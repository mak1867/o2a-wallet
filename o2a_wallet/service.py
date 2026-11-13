"""All business logic. The MCP server and the CLI are thin layers over this."""

from __future__ import annotations

import json
import sqlite3
import time
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import httpx
from o2a_protocol import (
    ChainedKeyResolver,
    KeyResolver,
    Offer,
    Outcome,
    StaticKeyResolver,
    VerificationResult,
    Verifier,
    offer_id,
    parse_strict_json,
)
from o2a_protocol.claims import ClaimClient, ClaimError
from o2a_protocol.ids import CLAIM_RE
from o2a_protocol.keys_http import HttpKeyResolver
from o2a_protocol.strictjson import StrictJSONError

from . import __version__
from .activitylog import ActivityLog
from .config import Config
from .resolve import RefResolver, ResolveError
from .storage import Storage
from .texts import MESSAGES

Result = dict[str, object]

MAX_SEARCH_RESULTS = 20
MAX_NOTE = 200
_TIMESTAMP = "%Y-%m-%dT%H:%M:%SZ"
_MINOR_UNIT_EXPONENT = {"JPY": 0, "KRW": 0}


class WalletError(Exception):
    """A request the wallet refused. ``kind`` and ``message`` are safe to show and log."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.message = message


def format_timestamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime(_TIMESTAMP)


def parse_timestamp(text: str) -> datetime:
    return datetime.strptime(text, _TIMESTAMP).replace(tzinfo=UTC)


def terms_text(terms: dict[str, object]) -> str:
    """For example "20% off for the first 3 months" or "10.00 USD off on the first payment"."""
    if terms["kind"] == "percent_off":
        text = f"{terms['percent']}% off"
    else:
        currency = str(terms["currency"])
        exponent = _MINOR_UNIT_EXPONENT.get(currency, 2)
        amount = cast(int, terms["amount_minor"]) / 10**exponent
        text = f"{amount:.{exponent}f} {currency} off"
    months = terms.get("duration_months")
    if months is None:
        return text + " on the first payment"
    return text + (" for the first month" if months == 1 else f" for the first {months} months")


def reported_status(row: sqlite3.Row, now: datetime) -> str:
    """Expiry is computed, never stored."""
    status = str(row["status"])
    if status == "active" and now >= parse_timestamp(row["exp"]):
        return "expired"
    if status == "claimed" and row["code_exp"] and now >= parse_timestamp(row["code_exp"]):
        return "expired"
    return status


class Wallet:
    def __init__(
        self,
        config: Config,
        storage: Storage,
        log: ActivityLog | None,
        *,
        keys: KeyResolver,
        http_keys: HttpKeyResolver | None = None,
        claim_client: ClaimClient,
        resolver: RefResolver | None = None,
        http: httpx.Client | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.config = config
        self._storage = storage
        self._log = log
        self._messages = MESSAGES
        self._verifier = Verifier(config.trusted_issuers, keys)
        self._http_keys = http_keys
        self._claim_client = claim_client
        self._resolver = resolver or RefResolver(config.trusted_issuers, client=http)
        self._http = http
        self._clock = clock
        self._last_snapshot: dict[str, object] = {}
        self._load_keys()

    # save ------------------------------------------------------------------------------

    def save(
        self,
        data: object = None,
        source: str = "mcp",
        now: datetime | None = None,
        *,
        ref: object = None,
    ) -> Result:
        """Save from a block (or a response containing one), or from a ref: exactly one.

        A ref is a claim token. The wallet looks up its signed block with a trusted issuer, then
        verifies and saves it exactly as if the block had been given. The ref itself is never
        logged or returned.
        """
        now = now or self._clock()
        with self._event("save_offer", source) as event:
            # Saving "off" is the one saving mode code can enforce. The CLI's `add` is the user
            # acting directly, so only the agent's saves are refused.
            if self.config.saving == "off" and source != "cli":
                raise WalletError("saving_off", self._messages["saving_off"])
            if (data is None) == (ref is None):
                event.update(offer_source=None, outcome="malformed")
                return self._not_saved("malformed", None, self._messages["block_or_ref"])
            if ref is not None:
                event["offer_source"] = "ref"
                if not isinstance(ref, str) or not CLAIM_RE.fullmatch(ref):
                    event["outcome"] = "malformed"
                    return self._not_saved("malformed", None, self._messages["malformed_ref"])
                event["offer_id"] = offer_id(ref)
                try:
                    data = self._resolver.resolve(ref)
                except ResolveError as exc:
                    event["outcome"] = exc.kind
                    return self._not_saved(exc.kind, offer_id(ref), self._messages[exc.kind])
            else:
                event["offer_source"] = "block"
            result = self._verify_input(data, now)
            outcome = result.outcome if result else Outcome.MALFORMED
            event["outcome"] = outcome.value
            if result and result.offer_id:
                event["offer_id"] = result.offer_id
            self._persist_keys(now)
            if result is None or result.offer is None:
                return {
                    "outcome": outcome.value,
                    "saved": False,
                    "offer_id": result.offer_id if result else None,
                    "summary": None,
                    "message": self._messages[outcome.value],
                }
            offer = result.offer
            saved, message = self._store(offer, source, now)
            return {
                "outcome": outcome.value,
                "saved": saved,
                "offer_id": offer.offer_id,
                "summary": self._summary(offer),
                "message": message,
            }
        raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    def _not_saved(outcome: str, oid: str | None, message: str) -> Result:
        return {"outcome": outcome, "saved": False, "offer_id": oid, "summary": None,
                "message": message}  # fmt: skip

    def _verify_input(self, data: object, now: datetime) -> VerificationResult | None:
        if isinstance(data, str):
            report = self._verifier.verify_response(data, mode="auto", now=now)
            # verify_response only finds an o2a_sponsored member, so a bare block reads as "none".
            if report.status == "none":
                return self._verifier.verify(data, now=now)
            return report.result
        if isinstance(data, dict) and "o2a_sponsored" in data:
            return self._verifier.verify_parsed(data["o2a_sponsored"], now=now)
        return self._verifier.verify_parsed(data, now=now)

    def _store(self, offer: Offer, source: str, now: datetime) -> tuple[bool, str]:
        exp = format_timestamp(offer.exp)
        stamp = format_timestamp(now)
        with self._storage.transaction() as conn:
            if self._row(conn, offer.offer_id) is not None:
                return False, self._messages["already_saved"]
            active = conn.execute(
                "SELECT COUNT(*) FROM offers WHERE status = 'active' AND exp > ?", (stamp,)
            ).fetchone()[0]
            if active >= self.config.max_active:
                raise WalletError("wallet_full", self._messages["wallet_full"])
            existing = conn.execute(
                "SELECT * FROM offers WHERE iss = ? AND campaign = ? AND status != 'superseded'",
                (offer.iss, offer.campaign),
            ).fetchone()
            status, superseded_by = "active", None
            if existing is not None:
                if existing["status"] == "active" and exp > existing["exp"]:
                    conn.execute(
                        "UPDATE offers SET status = 'superseded', superseded_by = ? "
                        "WHERE offer_id = ?",
                        (offer.offer_id, existing["offer_id"]),
                    )
                else:
                    status, superseded_by = "superseded", str(existing["offer_id"])
            block = offer.to_block()
            conn.execute(
                "INSERT INTO offers(offer_id, campaign, iss, kid, merchant_name, merchant_url, "
                "product, publisher_name, publisher_domain, terms_json, block_json, claim, exp, "
                "status, superseded_by, source, saved_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    offer.offer_id, offer.campaign, offer.iss, offer.kid,
                    offer.merchant.name, offer.merchant.url, offer.product,
                    offer.publisher.name, offer.publisher.domain,
                    json.dumps(block["terms"]), json.dumps(block), offer.claim, exp,
                    status, superseded_by, source, stamp,
                ),
            )  # fmt: skip
        if superseded_by:
            return True, self._messages["superseded"].format(offer_id=superseded_by)
        return True, self._messages["saved"].format(
            terms_text=terms_text(cast(dict[str, object], block["terms"])),
            merchant_name=offer.merchant.name,
            product=offer.product,
            exp=exp,
        )

    @staticmethod
    def _summary(offer: Offer) -> str:
        terms = cast(dict[str, object], offer.to_block()["terms"])
        return f"{terms_text(terms)} from {offer.merchant.name} ({offer.product})"

    # search ----------------------------------------------------------------------------

    def search(
        self, query: str | None = None, now: datetime | None = None, source: str = "mcp"
    ) -> Result:
        now = now or self._clock()
        with self._event("search_offers", source) as event:
            with self._storage.transaction() as conn:
                rows = conn.execute(
                    "SELECT * FROM offers WHERE status IN ('active', 'claimed') ORDER BY exp"
                ).fetchall()
            needle = query.casefold() if query else None
            offers = [
                self._public_offer(row, now)
                for row in rows
                if reported_status(row, now) in {"active", "claimed"}
                and (
                    needle is None
                    or needle in row["merchant_name"].casefold()
                    or needle in row["product"].casefold()
                    or needle in row["publisher_name"].casefold()
                )
            ][:MAX_SEARCH_RESULTS]
            event["outcome"] = "ok"
            return {"count": len(offers), "offers": offers}
        raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    def _public_offer(row: sqlite3.Row, now: datetime) -> Result:
        terms = json.loads(row["terms_json"])
        return {
            "offer_id": row["offer_id"],
            "status": reported_status(row, now),
            "merchant_name": row["merchant_name"],
            "merchant_url": row["merchant_url"],
            "product": row["product"],
            "terms": terms,
            "terms_text": terms_text(terms),
            "publisher_name": row["publisher_name"],
            "expires": row["exp"],
        }

    # claim -----------------------------------------------------------------------------

    def claim(
        self,
        offer_id: str,
        now: datetime | None = None,
        source: str = "mcp",
        *,
        purchase_approved: object = False,
    ) -> Result:
        """Checks, in order, before any network call: an already claimed offer
        returns its stored code; then claiming "never" is refused; then a claim without
        ``purchase_approved: true`` is refused. The argument is the model's own declaration, so
        it's a guardrail, not proof of consent."""
        now = now or self._clock()
        approved = purchase_approved is True
        with self._event("claim_offer", source) as event:
            event["offer_id"] = offer_id
            with self._storage.transaction() as conn:
                row = self._require(conn, offer_id)
                if row["status"] == "superseded":
                    raise self._superseded(conn, row)
            if row["status"] == "used":
                raise WalletError("already_used", self._messages["already_used"])
            if reported_status(row, now) == "claimed":
                event["outcome"] = "ok"
                return self._claim_result(row["offer_id"], row["code"], row["code_exp"], row)
            if self.config.claiming == "never":
                raise WalletError("claiming_off", self._messages["claiming_off"])
            if not approved:
                raise WalletError("purchase_not_approved", self._messages["purchase_not_approved"])

            offer = self._reverify(row, now)
            code, code_exp = self._obtain_code(offer, now)
            # The network call stays outside any transaction, so it never blocks other writers.
            with self._storage.transaction() as conn:
                conn.execute(
                    "UPDATE offers SET status = 'claimed', code = ?, code_exp = ?, claimed_at = ?, "
                    "purchase_approved = 1 WHERE offer_id = ? AND status IN ('active', 'claimed')",
                    (code, code_exp, format_timestamp(now), offer_id),
                )
            event["outcome"] = "ok"
            return self._claim_result(offer_id, code, code_exp, row)
        raise AssertionError("unreachable")  # pragma: no cover

    def _reverify(self, row: sqlite3.Row, now: datetime) -> Offer:
        try:
            block = parse_strict_json(row["block_json"])
        except StrictJSONError:
            raise WalletError("malformed", self._messages["malformed"]) from None
        result = self._verifier.verify_parsed(block, now=now)
        self._persist_keys(now)
        if result.offer is None:
            raise WalletError(result.outcome.value, self._messages[result.outcome.value])
        # The verifier allows clock skew, but the wallet already reports this offer as expired.
        if now >= result.offer.exp:
            raise WalletError("expired", self._messages["expired"])
        return result.offer

    def _obtain_code(self, offer: Offer, now: datetime) -> tuple[str, str]:
        try:
            claimed = self._claim_client.claim(offer, buyer_email=self.config.buyer_email, now=now)
        except ClaimError as exc:
            raise WalletError(exc.kind.value, self._messages[exc.kind.value]) from None
        return claimed.code, format_timestamp(claimed.exp)

    @staticmethod
    def _claim_result(offer_id: str, code: str, code_exp: str, row: sqlite3.Row) -> Result:
        return {
            "offer_id": offer_id,
            "code": code,
            "code_expires": code_exp,
            "merchant_url": row["merchant_url"],
            "terms_text": terms_text(json.loads(row["terms_json"])),
        }

    # settings --------------------------------------------------------------------------

    def settings(self, source: str = "mcp") -> Result:
        """The user's settings, read-only. Says whether a buyer email is set, never the email."""
        with self._event("wallet_settings", source) as event:
            event["outcome"] = "ok"
            return {
                "saving": self.config.saving,
                "save_notices": self.config.notices,
                "claiming": self.config.claiming,
                "trusted_issuers": list(self.config.trusted_issuers),
                "buyer_email_set": self.config.buyer_email is not None,
                "version": __version__,
            }
        raise AssertionError("unreachable")  # pragma: no cover

    # mark_used -------------------------------------------------------------------------

    def mark_used(
        self,
        offer_id: str,
        note: str | None = None,
        now: datetime | None = None,
        source: str = "mcp",
    ) -> Result:
        now = now or self._clock()
        with self._event("mark_used", source) as event:
            event["offer_id"] = offer_id
            if note is not None and len(note) > MAX_NOTE:
                raise WalletError("note_too_long", self._messages["note_too_long"])
            with self._storage.transaction() as conn:
                row = self._require(conn, offer_id)
                status = row["status"]
                if status == "superseded":
                    raise self._superseded(conn, row)
                if status == "active":
                    raise WalletError("not_claimed", self._messages["not_claimed"])
                if status == "claimed":
                    conn.execute(
                        "UPDATE offers SET status = 'used', used_at = ?, note = ? "
                        "WHERE offer_id = ?",
                        (format_timestamp(now), note, offer_id),
                    )
            event["outcome"] = "ok"
            return {"offer_id": offer_id, "status": "used"}
        raise AssertionError("unreachable")  # pragma: no cover

    # CLI -------------------------------------------------------------------------------

    def list_offers(self, include_all: bool = False, now: datetime | None = None) -> list[Result]:
        now = now or self._clock()
        with self._event("list", "cli") as event:
            with self._storage.transaction() as conn:
                rows = conn.execute("SELECT * FROM offers ORDER BY exp").fetchall()
            offers = [
                self._public_offer(row, now)
                for row in rows
                if include_all or reported_status(row, now) in {"active", "claimed"}
            ]
            event["outcome"] = "ok"
            return offers
        raise AssertionError("unreachable")  # pragma: no cover

    def show(self, offer_id: str, now: datetime | None = None) -> Result:
        """Full details for the human. The claim token is masked; the code is shown."""
        now = now or self._clock()
        with self._event("show", "cli") as event:
            event["offer_id"] = offer_id
            with self._storage.transaction() as conn:
                row = self._require(conn, offer_id)
            event["outcome"] = "ok"
            return {
                **self._public_offer(row, now),
                "stored_status": row["status"],
                "campaign": row["campaign"],
                "iss": row["iss"],
                "kid": row["kid"],
                "publisher_domain": row["publisher_domain"],
                "claim": str(row["claim"])[:6] + "…",
                "code": row["code"],
                "code_expires": row["code_exp"],
                "superseded_by": row["superseded_by"],
                "source": row["source"],
                "saved_at": row["saved_at"],
                "claimed_at": row["claimed_at"],
                "purchase_approved": (
                    None if row["purchase_approved"] is None else bool(row["purchase_approved"])
                ),
                "used_at": row["used_at"],
                "note": row["note"],
            }
        raise AssertionError("unreachable")  # pragma: no cover

    def remove(self, offer_id: str) -> None:
        with self._event("remove", "cli") as event:
            event["offer_id"] = offer_id
            with self._storage.transaction() as conn:
                self._require(conn, offer_id)
                conn.execute("DELETE FROM offers WHERE offer_id = ?", (offer_id,))
            event["outcome"] = "ok"

    def reset(self, keys: bool = False) -> int:
        with self._event("reset", "cli") as event:
            with self._storage.transaction() as conn:
                removed = conn.execute("DELETE FROM offers").rowcount
                if keys:
                    conn.execute("DELETE FROM key_cache")
            event["outcome"] = "ok"
            return removed
        raise AssertionError("unreachable")  # pragma: no cover

    def add(self, target: str, now: datetime | None = None) -> Result:
        """Save an offer from a page or file the user found themselves."""
        if target.startswith(("https://", "http://")):
            client = self._http or httpx.Client(timeout=10.0)
            try:
                response = client.get(target)
            except httpx.HTTPError:
                raise WalletError("add_failed", f"Could not fetch {target}.") from None
            if response.status_code != 200:
                raise WalletError(
                    "add_failed", f"Fetching {target} returned {response.status_code}."
                )
            text = response.text
        else:
            try:
                text = Path(target).expanduser().read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                raise WalletError("add_failed", f"Could not read {target}.") from None
        return self.save(text, source="cli", now=now)

    def stats(self, now: datetime | None = None, recent: int = 10) -> Result:
        """Counts of offers by status, and the most recent activity-log lines."""
        now = now or self._clock()
        with self._event("stats", "cli") as event:
            with self._storage.transaction() as conn:
                rows = conn.execute("SELECT * FROM offers").fetchall()
            event["outcome"] = "ok"
            return {
                "by_status": dict(Counter(reported_status(row, now) for row in rows)),
                "recent_activity": self._log.read()[-recent:] if self._log else [],
            }
        raise AssertionError("unreachable")  # pragma: no cover

    # Internals -------------------------------------------------------------------------

    @contextmanager
    def _event(self, tool: str, source: str) -> Iterator[dict[str, object]]:
        """Writes one log line per operation, with its duration, however it ends."""
        started = time.perf_counter()
        event: dict[str, object] = {}
        try:
            yield event
        except WalletError as exc:
            event["outcome"] = exc.kind
            raise
        except Exception:
            event["outcome"] = "internal_error"
            raise
        finally:
            ms = round((time.perf_counter() - started) * 1000)
            if self._log is not None:
                self._log.write(source=source, tool=tool, **event, ms=ms)

    @staticmethod
    def _row(conn: sqlite3.Connection, offer_id: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = conn.execute(
            "SELECT * FROM offers WHERE offer_id = ?", (offer_id,)
        ).fetchone()
        return row

    def _require(self, conn: sqlite3.Connection, offer_id: str) -> sqlite3.Row:
        row = self._row(conn, offer_id)
        if row is None:
            raise WalletError("not_found", self._messages["not_found"])
        return row

    def _superseded(self, conn: sqlite3.Connection, row: sqlite3.Row) -> WalletError:
        """Names the campaign's current offer, following the chain of replacements."""
        current = row
        seen = {row["offer_id"]}
        while current["status"] == "superseded" and current["superseded_by"] not in seen:
            following = self._row(conn, current["superseded_by"])
            if following is None:
                break
            seen.add(following["offer_id"])
            current = following
        message = self._messages["superseded"].format(offer_id=current["offer_id"])
        return WalletError("superseded", message)

    def _load_keys(self) -> None:
        if self._http_keys is None:
            return
        with self._storage.transaction() as conn:
            rows = conn.execute("SELECT iss, snapshot_json FROM key_cache").fetchall()
        issuers = {row["iss"]: json.loads(row["snapshot_json"]) for row in rows}
        self._http_keys.load_snapshot({"version": 1, "issuers": issuers})
        self._last_snapshot = issuers

    def _persist_keys(self, now: datetime) -> None:
        """Saves the key cache whenever a fetch changed it, for offline use after a restart."""
        if self._http_keys is None:
            return
        issuers = cast(dict[str, object], self._http_keys.snapshot()["issuers"])
        changed = {iss: e for iss, e in issuers.items() if self._last_snapshot.get(iss) != e}
        if not changed:
            return
        with self._storage.transaction() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO key_cache(iss, snapshot_json, updated_at) VALUES (?, ?, ?)",
                [(iss, json.dumps(e), format_timestamp(now)) for iss, e in changed.items()],
            )
        self._last_snapshot = {**self._last_snapshot, **changed}


def build_wallet(
    config: Config,
    *,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Wallet:
    """Wires up a wallet from its configuration. Tests pass a ``client`` with a fake issuer."""
    storage = Storage(config.db_path)
    log = None
    if config.log_path is not None:
        log = ActivityLog(config.log_path, settings=config.log_settings())
    pinned = StaticKeyResolver(config.pinned_keys)
    http_keys = None
    if config.fetch_keys:
        http_keys = HttpKeyResolver(config.trusted_issuers, client=client)
    keys: KeyResolver = ChainedKeyResolver(pinned, http_keys) if http_keys else pinned
    claim_client = ClaimClient(config.trusted_issuers, client=client, sleep=sleep)
    resolver = RefResolver(config.trusted_issuers, client=client, sleep=sleep)
    return Wallet(
        config, storage, log, keys=keys, http_keys=http_keys, claim_client=claim_client,
        resolver=resolver, http=client,
    )  # fmt: skip
