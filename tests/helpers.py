"""Shared test helpers: offers signed with the core library's public test key, a fake issuer, and
settings files."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from o2a_protocol import make_jwks, new_claim_token, offer_id
from o2a_protocol.testing import TEST_ISSUER, TEST_KID, make_body, test_signer

from o2a_wallet.config import Config
from o2a_wallet.service import Wallet, build_wallet
from o2a_wallet.settings import render_file, write_file

NOW = datetime(2026, 11, 2, 12, 0, 0, tzinfo=UTC)
FAKE_CODE_EXP = "2099-01-01T00:00:00Z"
CLAIMS_URL = f"https://{TEST_ISSUER}/v0/claims"

Handler = Callable[[httpx.Request], httpx.Response]


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def make_block(*, days: int = 14, now: datetime = NOW, **overrides: Any) -> dict[str, Any]:
    body = make_body(claim=new_claim_token(), exp=stamp(now + timedelta(days=days)), **overrides)
    return test_signer().sign(body, now=now)


def make_config(tmp_path: Path, **overrides: Any) -> Config:
    values: dict[str, Any] = {
        "db_path": tmp_path / "data" / "wallet.db",
        "log_path": tmp_path / "data" / "activity.jsonl",
        "trusted_issuers": (TEST_ISSUER,),
        "pinned_keys": {TEST_ISSUER: {TEST_KID: test_signer().public_key()}},
        "fetch_keys": False,
        "buyer_email": None,
        "saving": "auto",
        "notices": "every",
        "claiming": "with_purchase",
        "max_active": 200,
    }
    values.update(overrides)
    return Config(**values)


def fake_code(claim: str) -> str:
    return "FAKE-" + hashlib.sha256(claim.encode()).hexdigest()[:8].upper()


def fake_issuer(request: httpx.Request) -> httpx.Response:
    """Answers ``POST /v0/claims`` with a code derived from the claim token."""
    assert request.method == "POST" and str(request.url) == CLAIMS_URL, request.url
    claim = json.loads(request.content)["claim"]
    return httpx.Response(
        200, json={"offer_id": offer_id(claim), "code": fake_code(claim), "exp": FAKE_CODE_EXP}
    )


def build(config: Config, handler: Handler = fake_issuer) -> Wallet:
    """A wallet whose HTTP goes to ``handler`` (by default, a fake issuer that grants codes)."""
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return build_wallet(config, client=client, sleep=lambda _: None)


def pinned_jwks() -> dict[str, object]:
    return {TEST_ISSUER: make_jwks({TEST_KID: test_signer().public_key()})}


def write_settings(
    path: Path,
    *,
    saving: str = "auto",
    notices: str = "every",
    claiming: str = "with_purchase",
    issuers: tuple[str, ...] = (TEST_ISSUER,),
    email: str | None = None,
) -> Path:
    write_file(
        path,
        render_file(
            saving=saving, notices=notices, claiming=claiming, issuers=issuers, email=email
        ),
    )
    return path


def log_of(config: Config) -> Path:
    """The activity log's path, for a configuration that has one."""
    assert config.log_path is not None
    return config.log_path
