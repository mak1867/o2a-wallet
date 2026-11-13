"""Configuration validation and the command line."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from o2a_protocol.testing import TEST_ISSUER

from o2a_wallet import cli
from o2a_wallet.config import ConfigError, load_config

from .helpers import make_block, pinned_jwks, write_settings


def test_defaults(tmp_path: Path) -> None:
    path = write_settings(tmp_path / "config.toml", issuers=("a.example", "b.example"))
    config = load_config({"O2A_CONFIG": str(path)})
    assert config.trusted_issuers == ("a.example", "b.example")
    assert config.db_path == Path("~/.o2a/wallet.db").expanduser()
    assert config.log_path == Path("~/.o2a/activity.jsonl").expanduser()
    assert config.fetch_keys is True and config.max_active == 200
    assert config.saving == "auto"
    assert config.notices == "every" and config.claiming == "with_purchase"
    assert config.buyer_email is None


def test_activity_log_can_be_turned_off(tmp_path: Path) -> None:
    path = write_settings(tmp_path / "config.toml")
    assert load_config({"O2A_CONFIG": str(path), "O2A_WALLET_LOG": "off"}).log_path is None


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"O2A_FETCH_KEYS": "0"}, "no key source"),
        ({"O2A_FETCH_KEYS": "2"}, "O2A_FETCH_KEYS"),
        ({"O2A_MAX_ACTIVE": "0"}, "O2A_MAX_ACTIVE"),
        ({"O2A_MAX_ACTIVE": "x"}, "O2A_MAX_ACTIVE"),
        ({"O2A_PINNED_KEYS": "/nope.json"}, "cannot read"),
    ],
)
def test_invalid_configuration(tmp_path: Path, env: dict[str, str], message: str) -> None:
    path = write_settings(tmp_path / "config.toml", issuers=("a.example",))
    with pytest.raises(ConfigError) as info:
        load_config({"O2A_CONFIG": str(path), **env})
    assert message in str(info.value)


def test_no_issuers_means_run_init(tmp_path: Path) -> None:
    with pytest.raises(
        ConfigError, match="no trusted issuers are configured: run `o2a-wallet init`"
    ):
        load_config({"O2A_CONFIG": str(tmp_path / "absent.toml")})


@pytest.mark.parametrize(
    "var", ["O2A_SAVING", "O2A_SAVE_NOTICES", "O2A_CLAIMING", "O2A_TRUSTED_ISSUERS",
            "O2A_BUYER_EMAIL"],
)  # fmt: skip
def test_the_environment_never_overrides_the_users_settings(tmp_path: Path, var: str) -> None:
    path = write_settings(tmp_path / "config.toml")
    config = load_config({"O2A_CONFIG": str(path), var: "off"})
    assert (config.saving, config.notices, config.claiming) == ("auto", "every", "with_purchase")
    assert config.trusted_issuers == (TEST_ISSUER,) and config.buyer_email is None


def test_pinned_keys_file(tmp_path: Path) -> None:
    good = tmp_path / "keys.json"
    good.write_text(json.dumps(pinned_jwks()))
    env = {
        "O2A_CONFIG": str(write_settings(tmp_path / "config.toml")),
        "O2A_PINNED_KEYS": str(good),
        "O2A_FETCH_KEYS": "0",
    }
    assert set(load_config(env).pinned_keys[TEST_ISSUER]) == {"test-1"}

    good.write_text('{"a.example": {"keys": "nope"}}')
    with pytest.raises(ConfigError, match=r"the entry for a\.example"):
        load_config(env)


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    keys = tmp_path / "keys.json"
    keys.write_text(json.dumps(pinned_jwks()))
    monkeypatch.setenv("O2A_WALLET_DB", str(tmp_path / "wallet.db"))
    monkeypatch.setenv("O2A_WALLET_LOG", str(tmp_path / "activity.jsonl"))
    monkeypatch.setenv("O2A_CONFIG", str(write_settings(tmp_path / "config.toml")))
    monkeypatch.setenv("O2A_PINNED_KEYS", str(keys))
    monkeypatch.setenv("O2A_FETCH_KEYS", "0")
    return tmp_path


def _fresh_block_file(directory: Path, name: str, campaign: str) -> tuple[Path, dict[str, object]]:
    from datetime import UTC, datetime

    block = make_block(campaign=campaign, now=datetime.now(UTC).replace(microsecond=0))
    path = directory / name
    path.write_text(json.dumps({"content": "x", "o2a_sponsored": block}))
    return path, block


def test_cli_flow(cli_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path, block = _fresh_block_file(cli_env, "offer.json", "cmp_a")
    assert cli.main(["add", str(path)]) == 0
    assert "valid: Saved: 20% off" in capsys.readouterr().out

    assert cli.main(["list"]) == 0
    listed = capsys.readouterr().out
    assert "Acme Mail" in listed and "active" in listed
    assert str(block["claim"]) not in listed
    offer_id = next(w for w in listed.split() if w.startswith("off_"))

    assert cli.main(["show", offer_id]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["claim"] == str(block["claim"])[:6] + "…" and shown["source"] == "cli"

    assert cli.main(["stats"]) == 0
    stats = json.loads(capsys.readouterr().out)
    assert stats["by_status"] == {"active": 1}
    recent = stats["recent_activity"]
    assert [line["tool"] for line in recent] == ["save_offer", "list", "show"]
    assert recent[0]["outcome"] == "valid" and recent[0]["offer_id"] == offer_id

    assert cli.main(["remove", offer_id]) == 0
    assert cli.main(["remove", offer_id]) == 1
    capsys.readouterr()
    assert cli.main(["list", "--all"]) == 0
    assert "No offers." in capsys.readouterr().out


def test_cli_add_reports_bad_input(cli_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = cli_env / "bad.json"
    bad.write_text("nope")
    assert cli.main(["add", str(bad)]) == 1
    assert "malformed" in capsys.readouterr().out
    assert cli.main(["add", str(cli_env / "missing.json")]) == 1
    assert "Could not read" in capsys.readouterr().err


def test_cli_reset(cli_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path, _ = _fresh_block_file(cli_env, "offer.json", "cmp_a")
    cli.main(["add", str(path)])
    capsys.readouterr()
    assert cli.main(["reset"]) == 2  # Needs --yes.
    assert cli.main(["list"]) == 0
    assert "Acme Mail" in capsys.readouterr().out
    assert cli.main(["reset", "--yes", "--keys"]) == 0
    assert "Removed 1 offers." in capsys.readouterr().out


def test_cli_read_commands_do_not_need_trust_config(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("O2A_CONFIG", str(cli_env / "absent.toml"))
    monkeypatch.delenv("O2A_PINNED_KEYS")
    assert cli.main(["list"]) == 0
    assert cli.main(["add", "x"]) == 2
    assert "no trusted issuers are configured" in capsys.readouterr().err
