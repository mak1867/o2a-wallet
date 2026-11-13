"""Wallet settings: the file, the CLI, the instructions, enforcement, and wallet_settings."""

from __future__ import annotations

import itertools
import json
import os
import stat
import subprocess
import sys
import tomllib
from datetime import timedelta
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
from o2a_protocol.testing import TEST_ISSUER

from o2a_wallet import __version__, cli
from o2a_wallet.config import ConfigError, load_config
from o2a_wallet.service import Wallet, WalletError, build_wallet
from o2a_wallet.settings import (
    CLAIMING_MODES,
    NOTICE_MODES,
    SAVING_MODES,
    SettingsError,
    load_settings,
    render_file,
)
from o2a_wallet.texts import (
    CLAIM_LINES,
    DISCLOSE_LINE,
    MESSAGES,
    NOTICE_LINES,
    SAVE_LINES,
    SETTINGS_LINE,
    render_instructions,
)

from .helpers import NOW, build, log_of, make_block, make_config
from .test_mcp import block_now, connect, env_for

MSG = MESSAGES


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def env_with(path: Path, **extra: str) -> dict[str, str]:
    return {"O2A_CONFIG": str(path), **extra}


# The file and defaults ----------------------------------------------------------


def test_defaults_without_a_file(tmp_path: Path) -> None:
    settings = load_settings(env_with(tmp_path / "absent.toml"))
    assert (settings.saving, settings.notices, settings.claiming) == (
        "auto", "every", "with_purchase",
    )  # fmt: skip
    assert settings.trusted_issuers == () and settings.buyer_email is None
    assert settings.sources == {
        "saving": "default", "notices": "default", "claiming": "default",
        "issuers": "unset", "email": "unset",
    }  # fmt: skip


def test_the_spec_example_file_parses(tmp_path: Path) -> None:
    path = write(
        tmp_path / "config.toml",
        """
[saving]
mode = "relevant"            # auto | relevant | ask | off
notices = "when_relevant"    # every | when_relevant | off

[claiming]
mode = "never"               # with_purchase | never

[issuers]
trusted = ["o2a.exchange", "b.example"]

[buyer]
email = "you@example.com"
""",
    )
    settings = load_settings(env_with(path))
    assert (settings.saving, settings.notices, settings.claiming) == (
        "relevant", "when_relevant", "never",
    )  # fmt: skip
    assert settings.trusted_issuers == ("o2a.exchange", "b.example")
    assert settings.buyer_email == "you@example.com"
    assert set(settings.sources.values()) == {"file"}


def test_a_partial_file_falls_back_to_defaults(tmp_path: Path) -> None:
    path = write(tmp_path / "config.toml", '[claiming]\nmode = "never"\n')
    settings = load_settings(env_with(path))
    assert settings.claiming == "never" and settings.sources["claiming"] == "file"
    assert settings.saving == "auto" and settings.sources["saving"] == "default"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('[saving]\nmode = "sometimes"\n', "saving.mode must be one of auto, relevant, ask, off"),
        ('[saving]\nnotices = "loud"\n', "saving.notices must be one of"),
        ('[claiming]\nmode = "always"\n', "claiming.mode must be one of with_purchase, never"),
        ('[issuers]\ntrusted = "o2a.exchange"\n', "must be a list of issuer hostnames"),
        ('[issuers]\ntrusted = ["not a host"]\n', "issuers.trusted"),
        ('[buyer]\nemail = "nope"\n', "buyer.email is not a plausible email"),
        ('[saving]\nmood = "auto"\n', "unknown setting saving.mood"),
        ("[spending]\nlimit = 10\n", "unknown section [spending]"),
        ("[saving\n", "is not valid TOML"),
    ],
)
def test_invalid_file(tmp_path: Path, text: str, message: str) -> None:
    path = write(tmp_path / "config.toml", text)
    with pytest.raises(SettingsError) as info:
        load_settings(env_with(path))
    assert message in str(info.value) and str(path) in str(info.value)


def test_issuers_from_the_file_satisfy_the_server(tmp_path: Path) -> None:
    path = write(tmp_path / "config.toml", '[issuers]\ntrusted = ["o2a.exchange"]\n')
    assert load_config(env_with(path)).trusted_issuers == ("o2a.exchange",)
    with pytest.raises(ConfigError, match="run `o2a-wallet init`"):
        load_config(env_with(tmp_path / "absent.toml"))


def test_rendered_file_round_trips(tmp_path: Path) -> None:
    text = render_file(
        saving="ask", notices="off", claiming="never",
        issuers=["o2a.exchange", "b.example"], email='odd"\\name@example.com',
    )  # fmt: skip
    parsed = tomllib.loads(text)
    assert parsed["buyer"]["email"] == 'odd"\\name@example.com'
    assert parsed["issuers"]["trusted"] == ["o2a.exchange", "b.example"]
    unset = tomllib.loads(
        render_file(
            saving="auto", notices="every", claiming="with_purchase", issuers=[], email=None
        )
    )
    assert "buyer" not in unset


# CLI -----------------------------------------------------------------------------------------


@pytest.fixture
def settings_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "nested" / "config.toml"
    monkeypatch.setenv("O2A_CONFIG", str(path))
    return path


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_init_writes_defaults_privately(
    settings_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["init"]) == 0
    assert mode(settings_env) == 0o600
    parsed = tomllib.loads(settings_env.read_text())
    assert parsed == {
        "saving": {"mode": "auto", "notices": "every"},
        "claiming": {"mode": "with_purchase"},
        "issuers": {"trusted": ["o2a.exchange"]},
    }
    out = capsys.readouterr().out
    assert f"Created {settings_env}." in out and "email     not set" in out


def test_init_takes_options_and_refuses_to_overwrite(
    settings_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = ["init", "--saving", "relevant", "--notices", "off", "--claiming", "never",
            "--issuer", "a.example", "--issuer", "b.example"]  # fmt: skip
    assert cli.main(args) == 0
    settings = load_settings(os.environ)
    assert (settings.saving, settings.notices, settings.claiming) == ("relevant", "off", "never")
    assert settings.trusted_issuers == ("a.example", "b.example")
    before = settings_env.read_text()
    capsys.readouterr()
    assert cli.main(["init"]) == 1
    err = capsys.readouterr().err
    assert "already exists" in err and "config set" in err
    assert settings_env.read_text() == before


def test_init_rejects_a_bad_issuer(settings_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["init", "--issuer", "not a host"]) == 2
    assert "--issuer" in capsys.readouterr().err and not settings_env.exists()


def test_config_show_reports_where_each_value_came_from(
    settings_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["config", "show"]) == 0
    out = capsys.readouterr().out
    assert "(absent)" in out and "saving    auto  [default]" in out
    assert "issuers   (none)  [unset]" in out

    cli.main(["init"])
    cli.main(["config", "set", "claiming", "never"])
    cli.main(["config", "set", "email", "secret.person@example.com"])
    capsys.readouterr()
    assert cli.main(["config", "show"]) == 0
    out = capsys.readouterr().out
    assert "saving    auto  [file]" in out
    assert "claiming  never  [file]" in out
    assert "issuers   o2a.exchange  [file]" in out
    assert "email     set  [file]" in out
    assert "secret.person" not in out


def test_config_set_and_unset(settings_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["init"])
    changes = [
        ("saving", "off"),
        ("notices", "when_relevant"),
        ("claiming", "never"),
        ("issuers", "a.example, b.example"),
        ("email", "you@example.com"),
    ]
    for key, value in changes:
        assert cli.main(["config", "set", key, value]) == 0
        assert "next session" in capsys.readouterr().out
    assert mode(settings_env) == 0o600
    settings = load_settings(os.environ)
    assert (settings.saving, settings.notices, settings.claiming) == (
        "off", "when_relevant", "never",
    )  # fmt: skip
    assert settings.trusted_issuers == ("a.example", "b.example")
    assert settings.buyer_email == "you@example.com"

    assert cli.main(["config", "unset", "email"]) == 0
    assert load_settings(os.environ).buyer_email is None
    assert load_settings(os.environ).saving == "off"  # Everything else is kept.


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [("saving", "manual", "saving must be one of"), ("claiming", "x", "claiming must be one of"),
     ("issuers", " , ", "at least one issuer"), ("issuers", "not a host", "issuers"),
     ("email", "nope", "not a plausible email")],
)  # fmt: skip
def test_config_set_rejects_bad_values(
    settings_env: Path, capsys: pytest.CaptureFixture[str], key: str, value: str, message: str
) -> None:
    cli.main(["init"])
    before = settings_env.read_text()
    capsys.readouterr()
    assert cli.main(["config", "set", key, value]) == 2
    assert message in capsys.readouterr().err
    assert settings_env.read_text() == before


def test_config_set_needs_init_first(
    settings_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["config", "set", "saving", "off"]) == 1
    assert "run `o2a-wallet init` first" in capsys.readouterr().err
    assert not settings_env.exists()


def test_config_set_never_prints_the_email(
    settings_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["init"])
    capsys.readouterr()
    cli.main(["config", "set", "email", "secret.person@example.com"])
    assert "secret.person" not in capsys.readouterr().out


# Instructions --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("saving", "notices", "claiming"), list(itertools.product(SAVING_MODES, NOTICE_MODES,
                                                              CLAIMING_MODES)),
)  # fmt: skip
def test_instructions_for_every_combination(saving: str, notices: str, claiming: str) -> None:
    lines = render_instructions(saving, notices, claiming).split("\n")
    assert f"- {SAVE_LINES[saving]}" in lines
    assert f"- {CLAIM_LINES[claiming]}" in lines
    assert f"- {DISCLOSE_LINE}" in lines and f"- {SETTINGS_LINE}" in lines
    for other in SAVE_LINES.values():
        assert (f"- {other}" in lines) == (other == SAVE_LINES[saving])
    for other in CLAIM_LINES.values():
        assert (f"- {other}" in lines) == (other == CLAIM_LINES[claiming])
    for name, other in NOTICE_LINES.items():
        expected = saving in {"auto", "relevant"} and name == notices
        assert (f"- {other}" in lines) == expected
    # The notice line follows the save line directly.
    if saving in {"auto", "relevant"}:
        at = lines.index(f"- {SAVE_LINES[saving]}")
        assert lines[at + 1] == f"- {NOTICE_LINES[notices]}"


def test_instruction_wording() -> None:
    assert SAVE_LINES["off"] == "The user has turned saving off. Don't call save_offer."
    assert NOTICE_LINES["off"] == "Don't mention saved offers unless the user asks."
    assert CLAIM_LINES["with_purchase"].startswith(
        "Claim: call claim_offer only once the user, or their rules for you, have approved a "
        "purchase from that merchant, and pass `purchase_approved: true`."
    )
    assert SETTINGS_LINE == (
        "The user's wallet settings are available from wallet_settings; only the user can "
        "change them."
    )


# Enforcement ---------------------------------------------------------------------------------


def events(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_saving_off_refuses_the_agent_and_stores_nothing(tmp_path: Path) -> None:
    config = make_config(tmp_path, saving="off")
    wallet = build(config)
    with pytest.raises(WalletError) as info:
        wallet.save(make_block(), now=NOW)
    assert info.value.kind == "saving_off" and info.value.message == MSG["saving_off"]
    assert wallet.search(now=NOW)["count"] == 0
    line = events(log_of(config))[0]
    assert line["outcome"] == "saving_off"
    assert line["saving"] == "off"


def test_saving_off_never_resolves_a_ref(tmp_path: Path) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no network call expected")

    config = make_config(tmp_path, saving="off")
    wallet = build_wallet(config, client=httpx.Client(transport=httpx.MockTransport(fail)))
    with pytest.raises(WalletError, match="saving_off"):
        wallet.save(ref=str(make_block()["claim"]), now=NOW)


def test_saving_off_still_lets_the_user_add(tmp_path: Path) -> None:
    wallet = build(make_config(tmp_path, saving="off"))
    assert wallet.save(make_block(), source="cli", now=NOW)["saved"] is True


@pytest.mark.parametrize("saving", ["auto", "relevant", "ask"])
def test_other_saving_modes_save(tmp_path: Path, saving: str) -> None:
    wallet = build(make_config(tmp_path, saving=saving))
    assert wallet.save(make_block(), now=NOW)["saved"] is True


def saved(wallet: Wallet) -> str:
    return str(wallet.save(make_block(), now=NOW)["offer_id"])


def no_network(tmp_path: Path, **overrides: Any) -> Wallet:
    def fail(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no network call expected")

    config = make_config(tmp_path, **overrides)
    return build_wallet(config, client=httpx.Client(transport=httpx.MockTransport(fail)))


def test_claiming_never_is_refused_before_the_network(tmp_path: Path) -> None:
    wallet = no_network(tmp_path, claiming="never")
    oid = saved(wallet)
    with pytest.raises(WalletError) as info:
        wallet.claim(oid, now=NOW, purchase_approved=True)
    assert info.value.kind == "claiming_off" and info.value.message == MSG["claiming_off"]


def test_claiming_off_is_checked_before_purchase_approval(tmp_path: Path) -> None:
    wallet = no_network(tmp_path, claiming="never")
    with pytest.raises(WalletError, match="claiming_off"):
        wallet.claim(saved(wallet), now=NOW)


@pytest.mark.parametrize("approved", [False, None, "true", 1])
def test_claim_needs_purchase_approved_true(tmp_path: Path, approved: object) -> None:
    wallet = no_network(tmp_path)
    oid = saved(wallet)
    with pytest.raises(WalletError) as info:
        wallet.claim(oid, now=NOW, purchase_approved=approved)
    assert info.value.kind == "purchase_not_approved"
    assert info.value.message == MSG["purchase_not_approved"]
    assert wallet.show(oid, now=NOW)["stored_status"] == "active"


def test_approved_claim_is_stored_and_logged(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    wallet = build(config)
    oid = saved(wallet)
    assert wallet.show(oid, now=NOW)["purchase_approved"] is None
    wallet.claim(oid, now=NOW, purchase_approved=True)
    assert wallet.show(oid, now=NOW)["purchase_approved"] is True
    line = events(log_of(config))[-2]  # The last line is the `show`.
    assert line["tool"] == "claim_offer" and line["outcome"] == "ok"
    assert line["claiming"] == "with_purchase"


def test_already_claimed_returns_the_code_even_under_never(tmp_path: Path) -> None:
    first = build(make_config(tmp_path))
    oid = saved(first)
    code = first.claim(oid, now=NOW, purchase_approved=True)["code"]
    # The user then turns claiming off; the code they already have stays available.
    later = build(make_config(tmp_path, claiming="never"))
    assert later.claim(oid, now=NOW + timedelta(hours=1))["code"] == code


def test_a_schema_version_1_database_is_migrated(tmp_path: Path) -> None:
    import sqlite3

    db = tmp_path / "data" / "wallet.db"
    db.parent.mkdir()
    from o2a_wallet import storage

    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        for statement in storage._MIGRATIONS[0]:
            conn.execute(statement)
        conn.execute("INSERT INTO meta VALUES ('schema_version', '1')")
    wallet = build(make_config(tmp_path))
    oid = saved(wallet)
    wallet.claim(oid, now=NOW, purchase_approved=True)
    assert wallet.show(oid, now=NOW)["purchase_approved"] is True


# wallet_settings -----------------------------------------------------------------------------


def test_wallet_settings_never_contains_the_email(tmp_path: Path) -> None:
    config = make_config(
        tmp_path, saving="relevant", notices="off", claiming="never",
        buyer_email="secret.person@example.com",
    )  # fmt: skip
    result = build(config).settings()
    assert result == {
        "saving": "relevant",
        "save_notices": "off",
        "claiming": "never",
        "trusted_issuers": [TEST_ISSUER],
        "buyer_email_set": True,
        "version": __version__,
    }
    assert "secret.person" not in json.dumps(result)
    assert "secret.person" not in log_of(config).read_text()


def test_version_matches_pyproject() -> None:
    pyproject = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text())
    assert __version__ == pyproject["project"]["version"] == "0.3.0"


def test_settings_and_enforcement_over_stdio(tmp_path: Path) -> None:
    block = block_now()
    env = env_for(tmp_path, settings={"email": "secret.person@example.com"})

    async def scenario() -> None:
        async with connect(env) as session:
            settings = await session.call_tool("wallet_settings", {})
            assert settings.is_error is False
            assert settings.structured_content is not None
            assert settings.structured_content["buyer_email_set"] is True
            assert settings.structured_content["version"] == __version__
            assert "secret.person" not in settings.model_dump_json()

            saved = await session.call_tool("save_offer", {"block": block})
            assert saved.structured_content is not None
            oid = saved.structured_content["offer_id"]
            refused = await session.call_tool("claim_offer", {"offer_id": oid})
            assert refused.is_error is True
            assert refused.structured_content is not None
            assert refused.structured_content["error"] == "purchase_not_approved"

    anyio.run(scenario)


def test_saving_off_over_stdio(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with connect(env_for(tmp_path, settings={"saving": "off"})) as session:
            result = await session.call_tool("save_offer", {"block": block_now()})
            assert result.is_error is True
            assert result.structured_content == {
                "error": "saving_off", "message": MSG["saving_off"],
            }  # fmt: skip

    anyio.run(scenario)


def test_server_exits_on_an_invalid_settings_file(tmp_path: Path) -> None:
    path = write(tmp_path / "config.toml", '[saving]\nmode = "sometimes"\n')
    env = {"PATH": os.environ["PATH"], "O2A_WALLET_DB": str(tmp_path / "w.db"),
           "O2A_CONFIG": str(path)}  # fmt: skip
    done = subprocess.run(
        [sys.executable, "-c", "from o2a_wallet.mcp_server import main; main()"],
        env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30,
    )  # fmt: skip
    assert done.returncode == 2
    assert "saving.mode must be one of" in done.stderr


def test_server_uses_the_settings_file(tmp_path: Path) -> None:
    env = env_for(tmp_path, settings={"saving": "ask", "claiming": "never"})

    async def scenario() -> None:
        async with connect(env) as session:
            assert session.instructions == render_instructions("ask", "every", "never")
            result = await session.call_tool("wallet_settings", {})
            assert result.structured_content is not None
            assert result.structured_content["trusted_issuers"] == [TEST_ISSUER]

    anyio.run(scenario)
