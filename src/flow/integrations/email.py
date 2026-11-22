"""Outbound email interface + a simulated provider.

SimulatedEmailSender writes to a local outbox table instead of sending. It can
inject failures so retries and dead-lettering are exercised in tests.
Real providers (Postmark, SES, Gmail API, Microsoft Graph) plug in behind the
same `EmailSender` protocol later.
"""

from __future__ import annotations

import json
from typing import Protocol

from ..core.db import Database
from ..core.models import utcnow
from ..core.retry import PermanentError, TransientError

_OUTBOX = """
CREATE TABLE IF NOT EXISTS sim_email_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    to_addr TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    UNIQUE (tenant_id, idempotency_key)
)"""


class EmailSender(Protocol):
    def send(self, to: str, subject: str, body: str, idempotency_key: str) -> str: ...


class SimulatedEmailSender:
    name = "simulated-email"

    def __init__(
        self, db: Database, tenant_id: str, fail_first_n: int = 0, always_fail: bool = False
    ) -> None:
        self.db = db
        self.tenant_id = tenant_id
        self.fail_first_n = fail_first_n
        self.always_fail = always_fail
        self.calls = 0
        self.db.execute(_OUTBOX)

    def send(self, to: str, subject: str, body: str, idempotency_key: str) -> str:
        self.calls += 1
        if "@" not in to:
            raise PermanentError(f"invalid recipient {to!r}")
        if self.always_fail or self.calls <= self.fail_first_n:
            raise TransientError("simulated SMTP 451 temporary failure")
        existing = self.db.one(
            "SELECT id FROM sim_email_outbox WHERE tenant_id = ? AND idempotency_key = ?",
            (self.tenant_id, idempotency_key),
        )
        if existing:  # provider-side idempotency: a retry never sends twice
            return f"sim-msg-{existing['id']}"
        cur = self.db.execute(
            "INSERT INTO sim_email_outbox (tenant_id, idempotency_key, to_addr, subject, body, "
            "sent_at) VALUES (?, ?, ?, ?, ?, ?)",
            (self.tenant_id, idempotency_key, to, subject, body, utcnow()),
        )
        return f"sim-msg-{cur.lastrowid}"

    def outbox(self) -> list[dict]:
        return self.db.query(
            "SELECT * FROM sim_email_outbox WHERE tenant_id = ? ORDER BY id", (self.tenant_id,)
        )


def dump(rows: list[dict]) -> str:
    return json.dumps(rows, indent=2)
