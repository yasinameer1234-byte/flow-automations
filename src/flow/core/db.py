"""SQLite storage for the engine (runs, audit, approvals) and the sample CRM.

SQLite is the local/dev store. The schema is plain SQL so it ports to Postgres
(see docs/02-platform-architecture.md). Every table carries tenant_id from day one.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    workflow TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    status TEXT NOT NULL,
    input_json TEXT NOT NULL,
    result_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (tenant_id, workflow, dedupe_key)
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    run_id TEXT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    event TEXT NOT NULL,
    level TEXT NOT NULL,
    detail_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_run ON audit_log (run_id);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    action TEXT NOT NULL,
    params_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL,          -- pending | approved | rejected | executed | failed
    decided_by TEXT,
    decision_note TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT
);
CREATE TABLE IF NOT EXISTS dead_letters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    run_id TEXT,
    operation TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    error TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- Sample CRM (stands in for HubSpot/Pipedrive/etc. in the demo)
CREATE TABLE IF NOT EXISTS crm_contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    email TEXT NOT NULL,
    name TEXT,
    company TEXT,
    lifecycle_stage TEXT NOT NULL DEFAULT 'lead',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (tenant_id, email)
);
CREATE TABLE IF NOT EXISTS crm_deals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    contact_id INTEGER NOT NULL REFERENCES crm_contacts(id),
    title TEXT NOT NULL,
    value REAL,
    stage TEXT NOT NULL DEFAULT 'new',
    source_run_id TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS crm_tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    contact_id INTEGER REFERENCES crm_contacts(id),
    kind TEXT NOT NULL,            -- support | billing
    subject TEXT NOT NULL,
    priority TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    source_run_id TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS crm_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    contact_id INTEGER REFERENCES crm_contacts(id),
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class Database:
    """Thin, thread-safe wrapper. One connection, serialized writes (fine for SQLite/dev)."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._lock = threading.RLock()
        self._conn.executescript(SCHEMA)

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def query(self, sql: str, params: tuple | dict = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple | dict = ()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def transaction(self):
        return _Tx(self)

    def close(self) -> None:
        self._conn.close()


class _Tx:
    def __init__(self, db: Database) -> None:
        self.db = db

    def __enter__(self) -> Database:
        self.db._lock.acquire()
        self.db._conn.execute("BEGIN")
        return self.db

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                self.db._conn.execute("COMMIT")
            else:
                self.db._conn.execute("ROLLBACK")
        finally:
            self.db._lock.release()
