"""MCP integration: the server as a subprocess, over the SDK's stdio client."""

from __future__ import annotations

import builtins
import contextlib
import hashlib
import io
import itertools
import json
import os
import re
import sys
import urllib.request
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import anyio
import pytest
from cryptography.exceptions import InvalidSignature
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from o2a_protocol.testing import TEST_ISSUER

from o2a_wallet import __version__
from o2a_wallet.settings import CLAIMING_MODES, NOTICE_MODES, SAVING_MODES
from o2a_wallet.texts import PROTOCOL_TEMPLATE, TOOL_DESCRIPTIONS, render_instructions

from .helpers import fake_code, make_block, pinned_jwks, write_settings

REPO = Path(__file__).parent.parent


def env_for(
    tmp_path: Path, *, settings: dict[str, Any] | None = None, **extra: str
) -> dict[str, str]:
    """The server's environment, with a settings file written from ``settings``."""
    keys = tmp_path / "keys.json"
    keys.write_text(json.dumps(pinned_jwks()))
    config = write_settings(tmp_path / "config.toml", **(settings or {}))
    return {
        "PATH": os.environ["PATH"],
        "O2A_WALLET_DB": str(tmp_path / "wallet.db"),
        "O2A_WALLET_LOG": str(tmp_path / "activity.jsonl"),
        "O2A_CONFIG": str(config),
        "O2A_PINNED_KEYS": str(keys),
        "O2A_FETCH_KEYS": "0",
        **extra,
    }


@asynccontextmanager
async def connect(env: dict[str, str]) -> AsyncIterator[ClientSession]:
    """The real server, except that its HTTP goes to a fake issuer (``tests/fake_server.py``)."""
    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", "from tests.fake_server import main; main()"],
        env=env,
        cwd=REPO,
    )
    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        yield session


def block_now(**overrides: Any) -> dict[str, Any]:
    return make_block(now=datetime.now(UTC).replace(microsecond=0), **overrides)


def test_tool_descriptions_are_exact(tmp_path: Path) -> None:
    expected = TOOL_DESCRIPTIONS

    async def scenario() -> None:
        async with connect(env_for(tmp_path)) as session:
            assert session.server_info is not None and session.server_info.name == "o2a-wallet"
            assert session.server_info.version == __version__
            tools = {t.name: t for t in (await session.list_tools()).tools}
            assert set(tools) == {
                "save_offer", "search_offers", "claim_offer", "mark_used", "wallet_settings",
            }  # fmt: skip
            assert tools["wallet_settings"].description == expected.wallet_settings
            assert not tools["wallet_settings"].input_schema.get("properties")
            assert tools["save_offer"].description == expected.save_offer
            assert tools["search_offers"].description == expected.search_offers
            assert tools["claim_offer"].description == expected.claim_offer
            assert tools["mark_used"].description == expected.mark_used

            def param(tool: str, name: str) -> dict[str, Any]:
                schema: dict[str, Any] = tools[tool].input_schema["properties"][name]
                return schema

            assert param("save_offer", "block").get("description") == expected.save_offer_block
            assert {"type": "object"} in param("save_offer", "block")["anyOf"]
            assert {"type": "string"} in param("save_offer", "block")["anyOf"]
            assert param("search_offers", "query").get("description") == (
                expected.search_offers_query
            )
            assert param("claim_offer", "offer_id").get("description") == (
                expected.claim_offer_offer_id
            )
            assert param("mark_used", "offer_id").get("description") == expected.mark_used_offer_id
            assert param("mark_used", "note").get("description") == expected.mark_used_note
            assert param("mark_used", "note")["maxLength"] == 200
            # Exactly one of block or ref; the service enforces it, so neither is required here.
            assert "required" not in tools["save_offer"].input_schema
            assert param("save_offer", "ref").get("description") == expected.save_offer_ref
            assert param("save_offer", "ref")["type"] == "string"
            assert tools["claim_offer"].input_schema["required"] == ["offer_id"]
            approved = param("claim_offer", "purchase_approved")
            assert approved["type"] == "boolean"
            assert approved.get("description") == expected.claim_offer_purchase_approved
            assert "required" not in tools["search_offers"].input_schema

    anyio.run(scenario)


def test_descriptions_are_exact() -> None:
    assert TOOL_DESCRIPTIONS.save_offer == (
        "Saves a sponsored discount offer found in a tool result, after verifying its signature, "
        "so it can be used later on the user's behalf."
    )
    assert TOOL_DESCRIPTIONS.save_offer_block == (
        "The offer object exactly as it appeared (the value of an `o2a_sponsored` field), "
        "or the full response containing it."
    )
    assert TOOL_DESCRIPTIONS.save_offer_ref == (
        "An offer ref (a claim token starting with `clm_`), for when only the ref is available."
    )
    assert TOOL_DESCRIPTIONS.claim_offer == (
        "Gets a promo code for a saved offer. Use only when about to make a purchase from that "
        "merchant. Requires `purchase_approved: true`."
    )
    assert TOOL_DESCRIPTIONS.wallet_settings == (
        "Shows how the user has configured this wallet. Only the user can change these settings, "
        "with the o2a-wallet command."
    )


# SHA-256 of the text the model receives. A change here changes the agent's behavior, so it must be
# deliberate: update the hash only together with a changelog entry.
INSTRUCTIONS_SHA256 = "1fdd154969cbfe3625996e0ff5bb50e3a67ce64d2062957322b6c496eb2bd9fb"
PROTOCOL_SHA256 = "fbe6a3a66f98e977d8e47eaaee8288326f23ec5dcea1136f6dd9ec4e64d5fd7d"


def test_instructions_are_pinned() -> None:
    combos = itertools.product(SAVING_MODES, NOTICE_MODES, CLAIMING_MODES)
    text = "\n\n".join(render_instructions(*combo) for combo in combos)
    assert hashlib.sha256(text.encode()).hexdigest() == INSTRUCTIONS_SHA256


def test_protocol_template_is_pinned() -> None:
    assert hashlib.sha256(PROTOCOL_TEMPLATE.encode()).hexdigest() == PROTOCOL_SHA256


def test_server_sends_the_instructions_for_its_settings(tmp_path: Path) -> None:
    settings = {"saving": "relevant", "notices": "off", "claiming": "never"}

    async def scenario() -> None:
        async with connect(env_for(tmp_path, settings=settings)) as session:
            assert session.instructions == render_instructions("relevant", "off", "never")

    anyio.run(scenario)


def read_protocol(tmp_path: Path, issuers: tuple[str, ...] = (TEST_ISSUER,)) -> str:
    text = ""

    async def scenario() -> None:
        nonlocal text
        env = env_for(tmp_path, settings={"issuers": issuers})
        async with connect(env) as session:
            listed = (await session.list_resources()).resources
            assert [str(r.uri) for r in listed] == ["o2a://protocol"]
            assert listed[0].name == "O2A protocol and how to verify offers"
            assert listed[0].mime_type == "text/markdown"
            contents = (await session.read_resource("o2a://protocol")).contents
            assert contents[0].mime_type == "text/markdown"
            text = cast(Any, contents[0]).text

    anyio.run(scenario)
    return text


def test_protocol_resource_lists_exactly_the_trusted_issuers(tmp_path: Path) -> None:
    text = read_protocol(tmp_path, ("a.example", "b.example"))
    listed = text.split("This wallet trusts these issuers:\n", 1)[1].split("\n\n", 1)[0]
    assert listed == (
        "- `a.example` (keys: https://a.example/.well-known/o2a-keys.json)\n"
        "- `b.example` (keys: https://b.example/.well-known/o2a-keys.json)"
    )
    assert 'TRUSTED = {"a.example", "b.example"} ' in text
    assert "{trusted" not in text
    assert "`https://{iss}/.well-known/o2a-keys.json`" in text  # The generic step stays literal.


def run_protocol_snippet(
    resource: str, raw: str, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, list[str]]:
    """Runs the resource's own Python, with a local stub standing in for the issuer's key URL."""
    match = re.search(r"```python\n(.*?)```", resource, re.S)
    assert match is not None
    code, replaced = re.subn(
        r"^raw = '\.\.\.'.*$", lambda _: f"raw = {raw!r}", match.group(1), flags=re.M
    )
    assert replaced == 1
    fetched: list[str] = []

    def stub(url: str) -> io.BytesIO:
        fetched.append(url)
        return io.BytesIO(json.dumps(pinned_jwks()[TEST_ISSUER]).encode())

    monkeypatch.setattr(urllib.request, "urlopen", stub)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        builtins.exec(code, {})
    return out.getvalue(), fetched


def test_protocol_snippet_verifies_a_signed_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    block = block_now()
    output, fetched = run_protocol_snippet(read_protocol(tmp_path), json.dumps(block), monkeypatch)
    assert output.startswith("signature valid; expires " + block["exp"])
    assert fetched == [f"https://{TEST_ISSUER}/.well-known/o2a-keys.json"]


def test_protocol_snippet_rejects_a_tampered_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    block = block_now()
    block["product"] = block["product"] + " x"
    with pytest.raises(InvalidSignature):
        run_protocol_snippet(read_protocol(tmp_path), json.dumps(block), monkeypatch)


def test_protocol_snippet_rejects_an_untrusted_issuer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resource = read_protocol(tmp_path, ("someone-else.example",))
    with pytest.raises(ValueError, match="untrusted issuer"):
        run_protocol_snippet(resource, json.dumps(block_now()), monkeypatch)


def test_protocol_snippet_rejects_duplicate_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = json.dumps(block_now())[:-1] + ', "typ": "x"}'
    with pytest.raises(ValueError, match="duplicate member"):
        run_protocol_snippet(read_protocol(tmp_path), raw, monkeypatch)


def text_of(result: Any) -> str:
    return str(result.content[0].text)


def test_full_flow_over_stdio(tmp_path: Path) -> None:
    block = block_now()

    async def scenario() -> None:
        async with connect(env_for(tmp_path)) as session:
            saved = await session.call_tool("save_offer", {"block": block})
            assert saved.is_error is False
            data = saved.structured_content
            assert data is not None and set(data) == {
                "outcome", "saved", "offer_id", "summary", "message",
            }  # fmt: skip
            assert data["outcome"] == "valid" and data["saved"] is True
            assert text_of(saved) == data["message"]
            offer_id = data["offer_id"]

            found = await session.call_tool("search_offers", {"query": "acme"})
            found_data = found.structured_content
            assert found_data is not None and found_data["count"] == 1
            assert found_data["offers"][0]["offer_id"] == offer_id
            assert offer_id in text_of(found)

            claimed = await session.call_tool(
                "claim_offer", {"offer_id": offer_id, "purchase_approved": True}
            )
            claim_data = claimed.structured_content
            assert claim_data is not None and set(claim_data) == {
                "offer_id", "code", "code_expires", "merchant_url", "terms_text",
            }  # fmt: skip
            assert claim_data["code"] == fake_code(block["claim"])
            assert claim_data["code"] in text_of(claimed)

            used = await session.call_tool("mark_used", {"offer_id": offer_id, "note": "bought"})
            assert used.structured_content == {"offer_id": offer_id, "status": "used"}
            assert offer_id in text_of(used)

            for result in (saved, found, claimed, used):
                dumped = result.model_dump_json()
                assert block["claim"] not in dumped and block["sig"] not in dumped

    anyio.run(scenario)


def test_errors_are_tool_results(tmp_path: Path) -> None:
    block = block_now()

    async def scenario() -> None:
        async with connect(env_for(tmp_path)) as session:
            missing = await session.call_tool("claim_offer", {"offer_id": "off_nope"})
            assert missing.is_error is True
            assert missing.structured_content is not None
            assert missing.structured_content["error"] == "not_found"
            assert text_of(missing) == missing.structured_content["message"]

            saved = await session.call_tool("save_offer", {"block": block})
            assert saved.structured_content is not None
            offer_id = saved.structured_content["offer_id"]
            not_claimed = await session.call_tool("mark_used", {"offer_id": offer_id})
            assert not_claimed.is_error is True
            assert not_claimed.structured_content is not None
            assert not_claimed.structured_content["error"] == "not_claimed"

            long_note = await session.call_tool(
                "mark_used", {"offer_id": offer_id, "note": "x" * 201}
            )
            assert long_note.is_error is True
            assert long_note.structured_content is not None
            assert long_note.structured_content["error"] == "note_too_long"

    anyio.run(scenario)


def test_string_input_reaches_the_strict_parser(tmp_path: Path) -> None:
    """The SDK must not json.loads the string first, which would hide duplicate members."""
    text = json.dumps(block_now())

    async def scenario() -> None:
        async with connect(env_for(tmp_path)) as session:
            duplicate = await session.call_tool(
                "save_offer", {"block": text[:-1] + ', "typ": "x"}'}
            )
            assert duplicate.structured_content is not None
            assert duplicate.structured_content["outcome"] == "malformed"
            good = await session.call_tool("save_offer", {"block": text})
            assert good.structured_content is not None
            assert good.structured_content["saved"] is True

    anyio.run(scenario)
    lines = (tmp_path / "activity.jsonl").read_text().splitlines()
    assert [json.loads(line)["outcome"] for line in lines] == ["malformed", "valid"]


def test_rejected_save_is_a_normal_result(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with connect(env_for(tmp_path)) as session:
            result = await session.call_tool("save_offer", {"block": "not json"})
            assert result.is_error is False
            assert result.structured_content is not None
            assert result.structured_content["outcome"] == "malformed"
            assert result.structured_content["offer_id"] is None

    anyio.run(scenario)


def test_invalid_configuration_exits_with_a_message(tmp_path: Path) -> None:
    import subprocess

    env = {
        "PATH": os.environ["PATH"],
        "O2A_WALLET_DB": str(tmp_path / "w.db"),
        "O2A_CONFIG": str(tmp_path / "absent.toml"),
    }
    done = subprocess.run(
        [sys.executable, "-c", "from o2a_wallet.mcp_server import main; main()"],
        env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30,
    )  # fmt: skip
    assert done.returncode == 2
    assert "no trusted issuers are configured" in done.stderr
