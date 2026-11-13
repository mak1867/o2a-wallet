"""SQLite storage: WAL mode, schema and migrations."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

# One list of statements per schema version.
_MIGRATIONS: list[list[str]] = [
    [
        """
        CREATE TABLE offers(
          offer_id TEXT PRIMARY KEY,
          campaign TEXT NOT NULL, iss TEXT NOT NULL, kid TEXT NOT NULL,
          merchant_name TEXT NOT NULL, merchant_url TEXT NOT NULL,
          product TEXT NOT NULL,
          publisher_name TEXT NOT NULL, publisher_domain TEXT NOT NULL,
          terms_json TEXT NOT NULL,
          block_json TEXT NOT NULL,
          claim TEXT NOT NULL,
          exp TEXT NOT NULL,
          status TEXT NOT NULL CHECK (status IN ('active','claimed','used','superseded')),
          superseded_by TEXT,
          source TEXT NOT NULL CHECK (source IN ('mcp','cli')),
          saved_at TEXT NOT NULL, claimed_at TEXT, used_at TEXT,
          code TEXT, code_exp TEXT,
          note TEXT
        )
        """,
        "CREATE INDEX offers_campaign ON offers(iss, campaign)",
        """
        CREATE TABLE key_cache(
          iss TEXT PRIMARY KEY, snapshot_json TEXT NOT NULL, updated_at TEXT NOT NULL
        )
        """,
    ],
    # Whether the claim was declared as for a purchase the user approved.
    ["ALTER TABLE offers ADD COLUMN purchase_approved INTEGER"],
]


class Storage:
    """Opens a short-lived connection per transaction, so any thread or process can write."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        # The database holds claim tokens and codes, so it's private from its first moment.
        os.close(os.open(path, os.O_CREAT | os.O_RDWR, 0o600))
        path.chmod(0o600)
        conn = sqlite3.connect(path, timeout=10)  # WAL can't be set inside a transaction.
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        finally:
            conn.close()
        self._migrate()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """One write transaction. It commits on success and rolls back on any exception."""
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()

    def _migrate(self) -> None:
        with self.transaction() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
            version = int(row["value"]) if row else 0
            for statements in _MIGRATIONS[version:]:
                for statement in statements:
                    conn.execute(statement)
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
                (str(len(_MIGRATIONS)),),
            )
