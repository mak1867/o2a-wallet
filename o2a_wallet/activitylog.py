"""The local activity log: one JSON line per tool call, so the user can see what their agent did
with offers.

Only the fields in ``FIELDS`` are ever written. Claim tokens, promo codes, emails and raw offers
never reach it. The file is private (0600) and capped: once it passes ``MAX_BYTES``, the oldest
lines are dropped so that the newest half remains. A failed write never fails the tool call; it
warns once on stderr. ``O2A_WALLET_LOG=off`` turns it off.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

FIELDS = (
    "ts", "tool", "source", "outcome", "offer_id", "offer_source", "ms",
    "version", "saving", "notices", "claiming",
)  # fmt: skip
MAX_BYTES = 5 * 1024 * 1024


class ActivityLog:
    def __init__(
        self,
        path: Path,
        *,
        settings: Mapping[str, object] | None = None,
        max_bytes: int = MAX_BYTES,
    ) -> None:
        self.path = path
        self._settings = dict(settings or {})
        self._max_bytes = max_bytes
        self._warned = False

    def write(self, *, source: str, tool: str, **fields: object) -> None:
        values = {
            "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "tool": tool,
            "source": source,
            **fields,
            **self._settings,
        }
        line = {name: values[name] for name in FIELDS if name in values}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as out:
                out.write(json.dumps(line) + "\n")
            if self.path.stat().st_size > self._max_bytes:
                self._trim()
        except OSError as exc:
            if not self._warned:
                self._warned = True
                print(f"o2a-wallet: cannot write the activity log: {exc.strerror}", file=sys.stderr)

    def _trim(self) -> None:
        """Keeps the newest lines that fit in half the cap."""
        lines = self.path.read_bytes().splitlines(keepends=True)
        kept: list[bytes] = []
        size = 0
        for line in reversed(lines):
            if size + len(line) > self._max_bytes // 2:
                break
            kept.append(line)
            size += len(line)
        temporary = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(temporary, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.writelines(reversed(kept))
        os.replace(temporary, self.path)

    def read(self) -> list[dict[str, object]]:
        """Every parseable line, oldest first."""
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        events: list[dict[str, object]] = []
        for text in lines:
            try:
                event = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
        return events
