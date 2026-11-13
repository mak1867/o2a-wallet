"""Service behavior."""

from __future__ import annotations

import json
import stat
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from o2a_protocol import offer_id
from o2a_protocol.claims import ClaimErrorKind
from o2a_protocol.testing import TEST_ISSUER, TEST_KID, make_body, sign_unchecked, test_signer

from o2a_wallet.activitylog import ActivityLog
from o2a_wallet.config import Config
from o2a_wallet.service import Wallet, WalletError, build_wallet, terms_text
from o2a_wallet.storage import Storage
from o2a_wallet.texts import MESSAGES

from .helpers import NOW, build, fake_code, log_of, make_block, make_config, pinned_jwks, stamp

MSG = MESSAGES


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return make_config(tmp_path)


@pytest.fixture
def wallet(config: Config) -> Wallet:
    return build(config)


def events(config: Config) -> list[dict[str, Any]]:
    return [json.loads(line) for line in log_of(config).read_text().splitlines()]


def live_wallet(
    tmp_path: Path, handler: Callable[[httpx.Request], httpx.Response], **overrides: Any
) -> Wallet:
    return build(make_config(tmp_path, **overrides), handler)


def code_response(block: dict[str, Any], code: str = "CODE123") -> httpx.Response:
    from o2a_protocol import offer_id

    return httpx.Response(
        200,
        json={
            "offer_id": offer_id(block["claim"]),
            "code": code,
            "exp": stamp(NOW + timedelta(days=7)),
        },
    )


# Saving ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "wrap",
    [
        lambda b: b,
        lambda b: {"content": "x", "o2a_sponsored": b},
        json.dumps,
        lambda b: json.dumps({"content": "x", "o2a_sponsored": b}),
    ],
    ids=["block", "response", "block-string", "response-string"],
)
def test_save_accepts_every_input_form(
    wallet: Wallet, config: Config, wrap: Callable[[dict[str, Any]], object]
) -> None:
    block = make_block()
    result = wallet.save(wrap(block), now=NOW)
    assert result["outcome"] == "valid" and result["saved"] is True
    assert result["message"] == (
        "Saved: 20% off for the first 3 months from Acme Mail "
        f"(Transactional email API, paid plans), valid until {block['exp']}."
    )
    assert events(config)[-1]["offer_source"] == "block"


def test_save_bad_signature(wallet: Wallet, config: Config) -> None:
    block = make_block()
    sig = block["sig"]
    block["sig"] = ("A" if sig[0] != "A" else "B") + sig[1:]
    result = wallet.save(block, now=NOW)
    assert result["outcome"] == "bad_signature" and result["saved"] is False
    assert result["message"] == MSG["bad_signature"]
    assert result["offer_id"] is not None
    assert events(config)[-1]["outcome"] == "bad_signature"
    assert wallet.search(now=NOW)["count"] == 0


def test_save_malformed(wallet: Wallet, config: Config) -> None:
    result = wallet.save("not json", now=NOW)
    assert result["outcome"] == "malformed" and result["message"] == MSG["malformed"]
    assert events(config)[-1]["outcome"] == "malformed"
    assert wallet.save(7, now=NOW)["outcome"] == "unsupported"  # The core library's answer.


def test_string_input_rejects_duplicate_members(wallet: Wallet) -> None:
    text = json.dumps(make_block())
    assert wallet.save(text[:-1] + ', "typ": "x"}', now=NOW)["outcome"] == "malformed"


def test_save_untrusted_issuer(tmp_path: Path) -> None:
    wallet = build(make_config(tmp_path, trusted_issuers=("other.example",)))
    result = wallet.save(make_block(), now=NOW)
    assert result["outcome"] == "untrusted_issuer"
    assert result["message"] == MSG["untrusted_issuer"]


def test_save_unknown_key(tmp_path: Path) -> None:
    wallet = build(make_config(tmp_path, pinned_keys={}))
    assert wallet.save(make_block(), now=NOW)["outcome"] == "unknown_key"


def test_save_unsupported_kind(wallet: Wallet) -> None:
    for block in ({"nope": 1}, sign_unchecked(make_body(typ="o2a-sponsored-offer/v9"))):
        result = wallet.save(block, now=NOW)
        assert result["outcome"] == "unsupported" and result["message"] == MSG["unsupported"]


def test_save_expired(wallet: Wallet) -> None:
    block = make_block(days=1)
    result = wallet.save(block, now=NOW + timedelta(days=2))
    assert result["outcome"] == "expired" and result["message"] == MSG["expired"]
    assert wallet.list_offers(include_all=True, now=NOW) == []


def test_save_twice_changes_nothing(wallet: Wallet) -> None:
    block = make_block()
    first = wallet.save(block, now=NOW)
    second = wallet.save(block, now=NOW + timedelta(hours=1))
    assert second["outcome"] == "valid" and second["saved"] is False
    assert second["message"] == MSG["already_saved"]
    assert second["offer_id"] == first["offer_id"]
    assert wallet.show(str(first["offer_id"]), now=NOW)["saved_at"] == stamp(NOW)


def test_wallet_full(tmp_path: Path) -> None:
    wallet = build(make_config(tmp_path, max_active=2))
    wallet.save(make_block(campaign="cmp_a"), now=NOW)
    wallet.save(make_block(campaign="cmp_b"), now=NOW)
    with pytest.raises(WalletError) as info:
        wallet.save(make_block(campaign="cmp_c"), now=NOW)
    assert info.value.kind == "wallet_full" and info.value.message == MSG["wallet_full"]


def test_expired_offers_do_not_fill_the_wallet(tmp_path: Path) -> None:
    wallet = build(make_config(tmp_path, max_active=1))
    wallet.save(make_block(campaign="cmp_a", days=1), now=NOW)
    later = NOW + timedelta(days=2)
    assert wallet.save(make_block(campaign="cmp_b", now=later), now=later)["saved"] is True


# Same campaign -----------------------------------------------------------------------


def test_two_active_offers_later_expiry_wins_when_new_is_later(wallet: Wallet) -> None:
    old = wallet.save(make_block(days=5), now=NOW)
    new = wallet.save(make_block(days=9), now=NOW)
    offers = wallet.search(now=NOW)["offers"]
    assert [o["offer_id"] for o in offers] == [new["offer_id"]]  # type: ignore[attr-defined]
    shown = wallet.show(str(old["offer_id"]), now=NOW)
    assert shown["status"] == "superseded" and shown["superseded_by"] == new["offer_id"]


def test_two_active_offers_later_expiry_wins_when_existing_is_later(wallet: Wallet) -> None:
    old = wallet.save(make_block(days=9), now=NOW)
    new = wallet.save(make_block(days=5), now=NOW)
    assert new["saved"] is True
    assert new["message"] == MSG["superseded"].format(offer_id=old["offer_id"])
    offers = wallet.search(now=NOW)["offers"]
    assert [o["offer_id"] for o in offers] == [old["offer_id"]]  # type: ignore[attr-defined]


@pytest.mark.parametrize("use", [False, True], ids=["claimed", "used"])
def test_existing_claimed_or_used_supersedes_the_new_offer(wallet: Wallet, use: bool) -> None:
    old = str(wallet.save(make_block(days=5), now=NOW)["offer_id"])
    wallet.claim(old, now=NOW, purchase_approved=True)
    if use:
        wallet.mark_used(old, now=NOW)
    new = str(wallet.save(make_block(days=9), now=NOW)["offer_id"])
    shown = wallet.show(new, now=NOW)
    assert shown["status"] == "superseded" and shown["superseded_by"] == old


def test_different_campaigns_are_independent(wallet: Wallet) -> None:
    wallet.save(make_block(campaign="cmp_a"), now=NOW)
    wallet.save(make_block(campaign="cmp_b"), now=NOW)
    assert wallet.search(now=NOW)["count"] == 2


# Search ------------------------------------------------------------------------------


def test_search_filters_case_insensitively(wallet: Wallet) -> None:
    wallet.save(
        make_block(
            campaign="cmp_a",
            merchant={"name": "Postbird", "url": "https://postbird.example/p"},
            product="Mail API",
        ),
        now=NOW,
    )
    wallet.save(
        make_block(
            campaign="cmp_b",
            merchant={"name": "Hostly", "url": "https://hostly.example/p"},
            product="Web hosting",
            publisher={"name": "Docs Hub", "domain": "docs.example"},
        ),
        now=NOW,
    )
    for query, count in [("POSTBIRD", 1), ("hosting", 1), ("docs hub", 1), ("nope", 0), ("", 2)]:
        assert wallet.search(query, now=NOW)["count"] == count, query


def test_search_caps_at_20_sorted_by_expiry(wallet: Wallet) -> None:
    for i in range(25):
        wallet.save(make_block(campaign=f"cmp_{i}", days=25 - i), now=NOW)
    result = wallet.search(now=NOW)
    offers: list[dict[str, Any]] = result["offers"]  # type: ignore[assignment]
    assert result["count"] == 20 and len(offers) == 20
    assert [o["expires"] for o in offers] == sorted(o["expires"] for o in offers)
    assert offers[0]["expires"] == stamp(NOW + timedelta(days=1))


def test_search_output_shape(wallet: Wallet) -> None:
    block = make_block()
    wallet.save(block, now=NOW)
    offers: list[dict[str, Any]] = wallet.search(now=NOW)["offers"]  # type: ignore[assignment]
    assert set(offers[0]) == {
        "offer_id", "status", "merchant_name", "merchant_url", "product", "terms", "terms_text",
        "publisher_name", "expires",
    }  # fmt: skip
    assert offers[0]["terms"] == block["terms"]


def test_expiry_is_computed(tmp_path: Path) -> None:
    # The issuer's code expires with the offer.
    wallet = live_wallet(
        tmp_path,
        lambda request: httpx.Response(
            200,
            json={
                "offer_id": offer_id(json.loads(request.content)["claim"]),
                "code": "CODE",
                "exp": stamp(NOW + timedelta(days=1)),
            },
        ),
    )
    active = str(wallet.save(make_block(campaign="cmp_a", days=1), now=NOW)["offer_id"])
    claimed = str(wallet.save(make_block(campaign="cmp_b", days=1), now=NOW)["offer_id"])
    wallet.claim(claimed, now=NOW, purchase_approved=True)
    exactly = NOW + timedelta(days=1)
    assert wallet.search(now=exactly)["count"] == 0
    assert wallet.show(active, now=exactly)["status"] == "expired"
    assert wallet.show(claimed, now=exactly)["status"] == "expired"
    assert wallet.show(active, now=exactly)["stored_status"] == "active"
    assert wallet.search(now=exactly - timedelta(seconds=1))["count"] == 2


# Claiming ----------------------------------------------------------------------------


def test_claim_is_repeatable(wallet: Wallet, config: Config) -> None:
    block = make_block()
    offer_id = str(wallet.save(block, now=NOW)["offer_id"])
    first = wallet.claim(offer_id, now=NOW, purchase_approved=True)
    second = wallet.claim(offer_id, now=NOW, purchase_approved=True)
    assert first == second
    assert first["code"] == fake_code(block["claim"])
    assert first["code_expires"] == "2099-01-01T00:00:00Z"
    assert first["terms_text"] == "20% off for the first 3 months"
    assert first["merchant_url"] == block["merchant"]["url"]
    assert set(first) == {"offer_id", "code", "code_expires", "merchant_url", "terms_text"}


def test_live_claim_sends_the_expected_request_once(tmp_path: Path) -> None:
    block = make_block()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return code_response(block)

    wallet = live_wallet(tmp_path, handler, buyer_email="user@example.com")
    offer_id = str(wallet.save(block, now=NOW)["offer_id"])
    result = wallet.claim(offer_id, now=NOW, purchase_approved=True)
    assert result["code"] == "CODE123"
    assert result["code_expires"] == stamp(NOW + timedelta(days=7))
    assert len(requests) == 1
    assert str(requests[0].url) == f"https://{TEST_ISSUER}/v0/claims"
    assert json.loads(requests[0].content) == {
        "claim": block["claim"],
        "buyer": {"email": "user@example.com"},
    }
    assert wallet.claim(offer_id, now=NOW, purchase_approved=True) == result
    assert len(requests) == 1


def test_live_claim_omits_buyer_unless_configured(tmp_path: Path) -> None:
    block = make_block()
    bodies: list[object] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return code_response(block)

    wallet = live_wallet(tmp_path, handler)
    wallet.claim(str(wallet.save(block, now=NOW)["offer_id"]), now=NOW, purchase_approved=True)
    assert bodies == [{"claim": block["claim"]}]


def test_claim_again_after_the_code_expired(tmp_path: Path) -> None:
    block = make_block()
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return code_response(block)

    wallet = live_wallet(tmp_path, handler)
    offer_id = str(wallet.save(block, now=NOW)["offer_id"])
    wallet.claim(offer_id, now=NOW, purchase_approved=True)
    wallet.claim(offer_id, now=NOW + timedelta(days=8), purchase_approved=True)
    assert len(calls) == 2


def test_expired_offer_never_reaches_the_network(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("the transport must not be called")

    wallet = live_wallet(tmp_path, handler)
    offer_id = str(wallet.save(make_block(days=1), now=NOW)["offer_id"])
    # Exactly at exp, inside the verifier's clock skew, and well past it.
    for later in (NOW + timedelta(days=1), NOW + timedelta(days=2)):
        with pytest.raises(WalletError) as info:
            wallet.claim(offer_id, now=later, purchase_approved=True)
        assert info.value.kind == "expired" and info.value.message == MSG["expired"]


class MutableResolver:
    def __init__(self) -> None:
        self.keys: dict[tuple[str, str], Ed25519PublicKey] = {
            (TEST_ISSUER, TEST_KID): test_signer().public_key()
        }

    def get(self, iss: str, kid: str) -> Ed25519PublicKey | None:
        return self.keys.get((iss, kid))


def test_removed_key_means_error_and_no_network(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("the transport must not be called")

    from o2a_protocol.claims import ClaimClient

    config = make_config(tmp_path)
    resolver = MutableResolver()
    wallet = Wallet(
        config,
        Storage(config.db_path),
        ActivityLog(log_of(config)),
        keys=resolver,
        claim_client=ClaimClient(
            config.trusted_issuers, client=httpx.Client(transport=httpx.MockTransport(handler))
        ),
    )
    offer_id = str(wallet.save(make_block(), now=NOW)["offer_id"])
    resolver.keys.clear()
    with pytest.raises(WalletError) as info:
        wallet.claim(offer_id, now=NOW, purchase_approved=True)
    assert info.value.kind == "unknown_key" and info.value.message == MSG["unknown_key"]


@pytest.mark.parametrize(
    ("kind", "status"),
    [
        (ClaimErrorKind.MALFORMED_REQUEST, 400),
        (ClaimErrorKind.BUYER_REQUIRED, 400),
        (ClaimErrorKind.UNKNOWN_CLAIM, 404),
        (ClaimErrorKind.CAMPAIGN_LIMIT_REACHED, 409),
        (ClaimErrorKind.EXPIRED, 410),
        (ClaimErrorKind.ALREADY_CLAIMED, 410),
        (ClaimErrorKind.OFFER_UNAVAILABLE, 410),
        (ClaimErrorKind.RATE_LIMITED, 429),
    ],
)
def test_server_claim_errors_map_to_messages(
    tmp_path: Path, kind: ClaimErrorKind, status: int
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": kind.value, "message": "from the server"})

    wallet = live_wallet(tmp_path, handler)
    offer_id = str(wallet.save(make_block(), now=NOW)["offer_id"])
    with pytest.raises(WalletError) as info:
        wallet.claim(offer_id, now=NOW, purchase_approved=True)
    assert info.value.kind == kind.value and info.value.message == MSG[kind.value]
    assert "from the server" not in info.value.message
    assert wallet.show(offer_id, now=NOW)["stored_status"] == "active"


def test_network_error_message(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    wallet = live_wallet(tmp_path, handler)
    offer_id = str(wallet.save(make_block(), now=NOW)["offer_id"])
    with pytest.raises(WalletError) as info:
        wallet.claim(offer_id, now=NOW, purchase_approved=True)
    assert info.value.kind == "network_error" and info.value.message == MSG["network_error"]


def test_protocol_error_message(tmp_path: Path) -> None:
    wallet = live_wallet(tmp_path, lambda _: httpx.Response(200, json={"nope": 1}))
    offer_id = str(wallet.save(make_block(), now=NOW)["offer_id"])
    with pytest.raises(WalletError) as info:
        wallet.claim(offer_id, now=NOW, purchase_approved=True)
    assert info.value.kind == "protocol_error" and info.value.message == MSG["protocol_error"]


def test_every_claim_error_kind_has_a_message() -> None:
    assert all(kind.value in MSG for kind in ClaimErrorKind)


def test_claim_unknown_superseded_and_used(wallet: Wallet) -> None:
    with pytest.raises(WalletError) as info:
        wallet.claim("off_nope", now=NOW, purchase_approved=True)
    assert info.value.kind == "not_found"

    a = str(wallet.save(make_block(days=3), now=NOW)["offer_id"])
    b = str(wallet.save(make_block(days=5), now=NOW)["offer_id"])
    c = str(wallet.save(make_block(days=7), now=NOW)["offer_id"])
    for old in (a, b):  # The error names the current offer, even along a chain.
        with pytest.raises(WalletError) as info:
            wallet.claim(old, now=NOW, purchase_approved=True)
        assert info.value.kind == "superseded" and c in info.value.message

    wallet.claim(c, now=NOW, purchase_approved=True)
    wallet.mark_used(c, now=NOW)
    with pytest.raises(WalletError) as info:
        wallet.claim(c, now=NOW, purchase_approved=True)
    assert info.value.kind == "already_used" and info.value.message == MSG["already_used"]


# mark_used ---------------------------------------------------------------------------


def test_mark_used_transitions(wallet: Wallet) -> None:
    offer_id = str(wallet.save(make_block(), now=NOW)["offer_id"])
    with pytest.raises(WalletError) as info:
        wallet.mark_used(offer_id, now=NOW)
    assert info.value.kind == "not_claimed" and info.value.message == MSG["not_claimed"]

    wallet.claim(offer_id, now=NOW, purchase_approved=True)
    assert wallet.mark_used(offer_id, "email plan", now=NOW) == {
        "offer_id": offer_id,
        "status": "used",
    }
    later = NOW + timedelta(hours=1)
    assert wallet.mark_used(offer_id, now=later)["status"] == "used"  # Idempotent.
    shown = wallet.show(offer_id, now=later)
    assert shown["used_at"] == stamp(NOW) and shown["note"] == "email plan"


def test_mark_used_other_errors(wallet: Wallet) -> None:
    with pytest.raises(WalletError) as info:
        wallet.mark_used("off_nope", now=NOW)
    assert info.value.kind == "not_found"

    old = str(wallet.save(make_block(days=3), now=NOW)["offer_id"])
    wallet.save(make_block(days=5), now=NOW)
    with pytest.raises(WalletError) as info:
        wallet.mark_used(old, now=NOW)
    assert info.value.kind == "superseded"

    with pytest.raises(WalletError) as info:
        wallet.mark_used(old, "x" * 201, now=NOW)
    assert info.value.kind == "note_too_long"


# Secrets, permissions and robustness -------------------------------------------------


def test_secrets_stay_out_of_logs_and_search(tmp_path: Path) -> None:
    block = make_block()
    email = "buyer@example.com"
    wallet = live_wallet(tmp_path, lambda _: code_response(block, "SECRETCODE"), buyer_email=email)
    saved = wallet.save(block, now=NOW)
    offer_id = str(saved["offer_id"])
    before = json.dumps(wallet.search(now=NOW))
    claimed = wallet.claim(offer_id, now=NOW, purchase_approved=True)
    after = json.dumps(wallet.search(now=NOW))
    used = wallet.mark_used(offer_id, now=NOW)
    wallet.stats(now=NOW)

    log = log_of(wallet.config).read_text()
    assert log.count("\n") == 6
    for secret in (block["claim"], "SECRETCODE", email, block["sig"]):
        assert secret not in log
        assert secret not in before and secret not in after
        assert secret not in json.dumps(saved) and secret not in json.dumps(used)
    assert block["claim"] not in json.dumps(claimed)
    assert block["claim"] not in json.dumps(wallet.show(offer_id, now=NOW))
    assert wallet.show(offer_id, now=NOW)["claim"] == block["claim"][:6] + "…"


def test_database_is_private(config: Config, wallet: Wallet) -> None:
    assert stat.S_IMODE(config.db_path.stat().st_mode) == 0o600


def test_log_lines_have_exactly_the_documented_fields(wallet: Wallet, config: Config) -> None:
    wallet.save(make_block(), now=NOW)
    line = events(config)[-1]
    assert set(line) == {
        "ts", "tool", "source", "outcome", "offer_id", "offer_source", "ms",
        "version", "saving", "notices", "claiming",
    }  # fmt: skip
    assert line["source"] == "mcp" and line["tool"] == "save_offer" and line["outcome"] == "valid"
    assert str(line["offer_id"]).startswith("off_") and line["offer_source"] == "block"
    assert isinstance(line["ms"], int) and line["ts"]
    assert line["saving"] == "auto" and line["notices"] == "every"
    assert line["claiming"] == "with_purchase" and line["version"] == "0.3.0"


def test_every_operation_logs_the_settings_in_force(tmp_path: Path) -> None:
    config = make_config(tmp_path, saving="ask", claiming="never")
    wallet = build(config)
    wallet.save(make_block(), now=NOW)
    wallet.search(now=NOW)
    wallet.list_offers(now=NOW)
    wallet.stats(now=NOW)
    lines = events(config)
    assert [line["tool"] for line in lines] == ["save_offer", "search_offers", "list", "stats"]
    for line in lines:
        assert line["saving"] == "ask" and line["claiming"] == "never"


def test_activity_log_off_writes_nothing(tmp_path: Path) -> None:
    wallet = build(make_config(tmp_path, log_path=None))
    assert wallet.save(make_block(), now=NOW)["saved"] is True
    assert wallet.stats(now=NOW)["recent_activity"] == []
    assert not list((tmp_path / "data").glob("*.jsonl"))


def test_activity_log_is_private_and_capped(tmp_path: Path) -> None:
    path = tmp_path / "activity.jsonl"
    log = ActivityLog(path, max_bytes=2000)
    for number in range(100):
        log.write(source="mcp", tool="search_offers", outcome="ok", offer_id=f"off_{number}")
        assert path.stat().st_size <= 2000
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    kept = log.read()
    assert kept[-1]["offer_id"] == "off_99"  # The newest lines survive.
    assert 0 < len(kept) < 100


def test_activity_log_never_writes_unlisted_fields(tmp_path: Path) -> None:
    path = tmp_path / "activity.jsonl"
    ActivityLog(path).write(source="mcp", tool="claim_offer", code="SECRET", claim="clm_x")
    assert "SECRET" not in path.read_text() and "clm_x" not in path.read_text()


def test_log_write_failure_warns_once_and_never_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("")
    wallet = build(make_config(tmp_path, log_path=blocker / "activity.jsonl"))
    assert wallet.save(make_block(), now=NOW)["saved"] is True
    wallet.search(now=NOW)
    assert capsys.readouterr().err.count("cannot write the activity log") == 1


def test_concurrent_writers_share_the_database(config: Config) -> None:
    first, second = build(config), build(config)
    first.save(make_block(campaign="cmp_a"), now=NOW)
    second.save(make_block(campaign="cmp_b"), source="cli", now=NOW)
    assert first.search(now=NOW)["count"] == 2


# Key cache ---------------------------------------------------------------------------


def test_key_cache_survives_a_restart_offline(tmp_path: Path) -> None:
    fetches: list[int] = []

    def online(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/.well-known/o2a-keys.json"
        fetches.append(1)
        return httpx.Response(
            200, json=pinned_jwks()[TEST_ISSUER], headers={"Cache-Control": "max-age=300"}
        )

    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    config = make_config(tmp_path, pinned_keys={}, fetch_keys=True)
    block = make_block()
    first = build_wallet(config, client=httpx.Client(transport=httpx.MockTransport(online)))
    assert first.save(block, now=NOW)["saved"] is True
    assert fetches == [1]

    restarted = build_wallet(config, client=httpx.Client(transport=httpx.MockTransport(offline)))
    result = restarted.save(make_block(campaign="cmp_b"), now=NOW)
    assert result["outcome"] == "valid"


def test_failed_key_fetch_is_unknown_key(tmp_path: Path) -> None:
    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    config = make_config(tmp_path, pinned_keys={}, fetch_keys=True)
    wallet = build_wallet(config, client=httpx.Client(transport=httpx.MockTransport(offline)))
    assert wallet.save(make_block(), now=NOW)["outcome"] == "unknown_key"


# Terms -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("terms", "text"),
    [
        ({"kind": "percent_off", "percent": 20, "duration_months": 3},
         "20% off for the first 3 months"),
        ({"kind": "percent_off", "percent": 100, "duration_months": 1},
         "100% off for the first month"),
        ({"kind": "percent_off", "percent": 15}, "15% off on the first payment"),
        ({"kind": "amount_off", "amount_minor": 1000, "currency": "USD"},
         "10.00 USD off on the first payment"),
        ({"kind": "amount_off", "amount_minor": 500, "currency": "JPY", "duration_months": 2},
         "500 JPY off for the first 2 months"),
        ({"kind": "amount_off", "amount_minor": 1200, "currency": "KRW"},
         "1200 KRW off on the first payment"),
    ],
)  # fmt: skip
def test_terms_text(terms: dict[str, object], text: str) -> None:
    assert terms_text(terms) == text
