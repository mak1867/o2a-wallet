"""Saving an offer from a ref: a claim token resolved with a trusted issuer (format spec §9.5)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
from o2a_protocol import new_claim_token, offer_id
from o2a_protocol.testing import TEST_ISSUER

from o2a_wallet.config import Config
from o2a_wallet.mcp_server import build_server
from o2a_wallet.service import Wallet, build_wallet
from o2a_wallet.texts import MESSAGES

from .helpers import CLAIMS_URL, NOW, fake_code, fake_issuer, log_of, make_block, make_config

MSG = MESSAGES
RESOLVE_URL = f"https://{TEST_ISSUER}/v0/offers/resolve"


def ref_wallet(
    tmp_path: Path,
    handler: Callable[[httpx.Request], httpx.Response],
    **overrides: Any,
) -> tuple[Wallet, Config, list[httpx.Request]]:
    """A wallet whose HTTP goes to ``handler``, recording every request."""
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    config = make_config(tmp_path, **overrides)
    client = httpx.Client(transport=httpx.MockTransport(recording))
    return build_wallet(config, client=client, sleep=lambda _: None), config, seen


def issuer_with(*blocks: dict[str, Any]) -> Callable[[httpx.Request], httpx.Response]:
    """A fake issuer that knows these blocks, like POST /v0/offers/resolve."""
    known = {b["claim"]: b for b in blocks}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and str(request.url) == RESOLVE_URL
        claim = json.loads(request.content)["claim"]
        if claim not in known:
            return httpx.Response(404, json={"error": "unknown_claim", "message": "-"})
        return httpx.Response(200, json={"o2a_sponsored": known[claim]})

    return handler


def events(config: Config) -> list[dict[str, Any]]:
    return [json.loads(line) for line in log_of(config).read_text().splitlines()]


def everything_written(config: Config, result: object) -> str:
    """Tool output, event log and stderr-free state the model or a log reader could see."""
    return json.dumps(result) + log_of(config).read_text()


def test_save_from_ref_end_to_end(tmp_path: Path) -> None:
    block = make_block()
    wallet, config, seen = ref_wallet(tmp_path, issuer_with(block))
    result = wallet.save(ref=block["claim"], now=NOW)
    assert result["outcome"] == "valid" and result["saved"] is True
    assert result["offer_id"] == offer_id(block["claim"])
    assert str(result["message"]).startswith("Saved: 20% off")
    assert wallet.save(block, now=NOW)["message"] == MSG["already_saved"]  # same offer, as a block
    # The request: one POST, JSON body with exactly the claim, no redirects followed.
    [request] = seen
    assert json.loads(request.content) == {"claim": block["claim"]}
    assert request.headers["content-type"] == "application/json"
    # Saved like a block save: search finds it.
    offers = wallet.search(None, now=NOW)["offers"]
    assert isinstance(offers, list) and len(offers) == 1
    assert offers[0]["offer_id"] == offer_id(block["claim"])
    first = events(config)[0]
    assert first["offer_source"] == "ref" and first["outcome"] == "valid"
    assert first["offer_id"] == offer_id(block["claim"])
    assert events(config)[1]["offer_source"] == "block"
    assert block["claim"] not in everything_written(config, result)


def test_ref_message_matches_a_block_save(tmp_path: Path) -> None:
    block = make_block()
    via_ref, _, _ = ref_wallet(tmp_path / "a", issuer_with(block))
    via_block, _, _ = ref_wallet(tmp_path / "b", issuer_with())
    assert (
        via_ref.save(ref=block["claim"], now=NOW)["message"]
        == (via_block.save(block, now=NOW)["message"])
    )


def test_unknown_ref(tmp_path: Path) -> None:
    claim = new_claim_token()
    wallet, config, seen = ref_wallet(tmp_path, issuer_with())
    result = wallet.save(ref=claim, now=NOW)
    assert result == {
        "outcome": "unknown_ref", "saved": False, "offer_id": offer_id(claim), "summary": None,
        "message": MSG["unknown_ref"],
    }  # fmt: skip
    assert len(seen) == 1
    [event] = events(config)
    assert event["outcome"] == "unknown_ref" and event["offer_source"] == "ref"
    assert claim not in everything_written(config, result)


@pytest.mark.parametrize(
    "failure",
    [
        lambda r: (_ for _ in ()).throw(httpx.ConnectError("down", request=r)),
        lambda r: httpx.Response(503, json={"error": "unavailable", "message": "-"}),
    ],
    ids=["network", "503"],
)
def test_issuer_down(tmp_path: Path, failure: Callable[[httpx.Request], httpx.Response]) -> None:
    claim = new_claim_token()
    wallet, config, seen = ref_wallet(tmp_path, failure)
    result = wallet.save(ref=claim, now=NOW)
    assert result["outcome"] == "issuer_unreachable" and result["saved"] is False
    assert result["message"] == MSG["issuer_unreachable"]
    assert len(seen) == 3  # the first try and two retries, as for claims
    assert claim not in everything_written(config, result)


@pytest.mark.parametrize(
    ("response", "outcome"),
    [
        (httpx.Response(410, json={"error": "expired", "message": "-"}), "expired"),
        (httpx.Response(429, json={"error": "rate_limited", "message": "-"}), "rate_limited"),
        (httpx.Response(302, headers={"Location": "https://elsewhere.example/"}), "protocol_error"),
        (httpx.Response(200, content=b"not json"), "protocol_error"),
        (httpx.Response(200, json={"o2a_sponsored": {}, "extra": 1}), "protocol_error"),
    ],
)
def test_other_issuer_answers(tmp_path: Path, response: httpx.Response, outcome: str) -> None:
    wallet, _, _ = ref_wallet(tmp_path, lambda _: response)
    result = wallet.save(ref=new_claim_token(), now=NOW)
    assert result["outcome"] == outcome and result["saved"] is False
    assert result["message"] == MSG[outcome]


def test_an_issuer_may_only_answer_for_the_ref_it_was_asked_about(tmp_path: Path) -> None:
    other = make_block()
    answer = httpx.Response(200, json={"o2a_sponsored": other})
    wallet, _, _ = ref_wallet(tmp_path, lambda _: answer)
    result = wallet.save(ref=new_claim_token(), now=NOW)
    assert result["outcome"] == "protocol_error" and result["saved"] is False
    assert wallet.search(None, now=NOW)["offers"] == []


def test_a_resolved_block_still_has_to_verify(tmp_path: Path) -> None:
    block = make_block()
    tampered = {**block, "product": "Something else"}
    wallet, config, _ = ref_wallet(tmp_path, issuer_with(tampered))
    result = wallet.save(ref=block["claim"], now=NOW)
    assert result["outcome"] == "bad_signature" and result["saved"] is False
    assert events(config)[0]["outcome"] == "bad_signature"


@pytest.mark.parametrize(
    "ref",
    [
        "clm_short",
        "off_0123456789abcdef01234567",
        "CLM_0123456789ABCDEFGHJKMNPQRS",
        "clm_8123456789ABCDEFGHJKMNPQRS",
        " clm_0123456789ABCDEFGHJKMNPQRS",
        42,
    ],
)
def test_malformed_ref_makes_no_request(tmp_path: Path, ref: object) -> None:
    wallet, config, seen = ref_wallet(tmp_path, issuer_with())
    result = wallet.save(ref=ref, now=NOW)
    assert result["outcome"] == "malformed" and result["message"] == MSG["malformed_ref"]
    assert seen == []
    assert events(config)[0]["offer_source"] == "ref"


def test_both_or_neither(tmp_path: Path) -> None:
    block = make_block()
    wallet, config, seen = ref_wallet(tmp_path, issuer_with(block))
    for kwargs in ({"data": block, "ref": block["claim"]}, {}):
        result = wallet.save(now=NOW, **kwargs)
        assert result["outcome"] == "malformed" and result["saved"] is False
        assert result["message"] == MSG["block_or_ref"]
    assert seen == [] and wallet.search(None, now=NOW)["offers"] == []
    assert block["claim"] not in log_of(config).read_text()


def test_an_offer_saved_from_a_ref_can_be_claimed(tmp_path: Path) -> None:
    block = make_block()
    resolve = issuer_with(block)

    def issuer(request: httpx.Request) -> httpx.Response:
        return fake_issuer(request) if str(request.url) == CLAIMS_URL else resolve(request)

    wallet, _, seen = ref_wallet(tmp_path, issuer)
    assert wallet.save(ref=block["claim"], now=NOW)["saved"] is True and len(seen) == 1
    claimed = wallet.claim(offer_id(block["claim"]), now=NOW, purchase_approved=True)
    assert claimed["code"] == fake_code(block["claim"])
    assert [str(request.url) for request in seen] == [RESOLVE_URL, CLAIMS_URL]


def test_mcp_tool_takes_ref_and_never_echoes_it(tmp_path: Path) -> None:
    block = make_block(now=NOW)
    wallet, _, _ = ref_wallet(tmp_path, issuer_with(block))
    wallet._clock = lambda: NOW
    server = build_server(wallet)

    async def scenario() -> list[Any]:
        return [
            await server.call_tool("save_offer", {"ref": block["claim"]}),
            await server.call_tool("save_offer", {"ref": new_claim_token()}),
            await server.call_tool("save_offer", {}),
            await server.call_tool("save_offer", {"block": block, "ref": block["claim"]}),
        ]

    saved, unknown, neither, both = anyio.run(scenario)
    assert saved.structured_content["saved"] is True
    assert saved.content[0].text.startswith("Saved: ")
    assert unknown.content[0].text == MSG["unknown_ref"]
    assert neither.content[0].text == both.content[0].text == MSG["block_or_ref"]
    for result in (saved, unknown, neither, both):
        assert "clm_" not in result.model_dump_json()
