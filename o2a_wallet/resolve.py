"""Looks up the signed block for an offer ref (a claim token) with a trusted issuer.

Uses the issuer's ``POST /v0/offers/resolve`` (format spec §9.5,
https://github.com/mak1867/o2a/blob/main/docs/o2a-format-v0.md#95-resolving-a-claim-token). The
token goes only in a POST body, over HTTPS, with no redirects. It is never logged or returned:
callers get the block, or a ``ResolveError`` whose kind is safe to log. With several trusted
issuers, the token is sent to each in turn until one knows it.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable

import httpx
from o2a_protocol import parse_strict_json
from o2a_protocol.strictjson import StrictJSONError

TIMEOUT_S = 10.0
_BACKOFF = (0.5, 1.0)
_MAX_RETRIES = 2
_MAX_RETRY_AFTER_S = 30.0

# Outcomes, from most to least useful when issuers disagree.
_PRIORITY = ("expired", "rate_limited", "issuer_unreachable", "protocol_error", "unknown_ref")


class ResolveError(Exception):
    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


class RefResolver:
    def __init__(
        self,
        trusted_issuers: Iterable[str],
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._issuers = tuple(trusted_issuers)  # configuration order
        self._client = client or httpx.Client(timeout=TIMEOUT_S, follow_redirects=False)
        self._sleep = sleep

    def resolve(self, ref: str) -> dict[str, object]:
        """The issuer's response, ``{"o2a_sponsored": <block>}``, from the first trusted issuer
        that knows the ref. Raises ``ResolveError``: ``unknown_ref`` if every issuer answered 404,
        otherwise the most useful failure seen."""
        failures: list[str] = []
        for iss in self._issuers:
            try:
                return self._ask(iss, ref)
            except ResolveError as exc:
                failures.append(exc.kind)
        kind = min(failures, key=_PRIORITY.index) if failures else "unknown_ref"
        raise ResolveError(kind)

    def _ask(self, iss: str, ref: str) -> dict[str, object]:
        url = f"https://{iss}/v0/offers/resolve"
        content = json.dumps({"claim": ref}).encode()
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        attempt = 0
        while True:
            try:
                response = self._client.post(
                    url, content=content, headers=headers, follow_redirects=False,
                    timeout=TIMEOUT_S,
                )  # fmt: skip
            except httpx.TransportError:
                if attempt < _MAX_RETRIES:
                    self._sleep(_BACKOFF[min(attempt, len(_BACKOFF) - 1)])
                    attempt += 1
                    continue
                raise ResolveError("issuer_unreachable") from None
            status = response.status_code
            if status >= 500:
                if attempt < _MAX_RETRIES:
                    self._sleep(_BACKOFF[min(attempt, len(_BACKOFF) - 1)])
                    attempt += 1
                    continue
                raise ResolveError("issuer_unreachable")
            if status == 429:
                wait = _retry_after(response.headers.get("retry-after"))
                if wait is not None and wait <= _MAX_RETRY_AFTER_S and attempt < _MAX_RETRIES:
                    self._sleep(wait)
                    attempt += 1
                    continue
                raise ResolveError("rate_limited")
            if status == 404:
                raise ResolveError("unknown_ref")
            if status == 410:
                raise ResolveError("expired")
            if status != 200:  # redirects and anything unexpected
                raise ResolveError("protocol_error")
            return _parse(response.content, ref)


def _parse(content: bytes, ref: str) -> dict[str, object]:
    try:
        document = parse_strict_json(content)
    except (StrictJSONError, UnicodeDecodeError, ValueError):
        raise ResolveError("protocol_error") from None
    if not isinstance(document, dict) or set(document) != {"o2a_sponsored"}:
        raise ResolveError("protocol_error")
    block = document["o2a_sponsored"]
    # An issuer may only answer for the ref it was asked about.
    if not isinstance(block, dict) or block.get("claim") != ref:
        raise ResolveError("protocol_error")
    return document


def _retry_after(value: str | None) -> float | None:
    if value is None or not value.strip().isascii() or not value.strip().isdigit():
        return None
    return float(value.strip())
