"""Simulated payments provider (stands in for Stripe refunds etc.).

Records refunds in a local table. Nothing leaves the machine.
"""

from __future__ import annotations

from ..core.db import Database
from ..core.models import utcnow
from ..core.retry import PermanentError

_TABLE = """
CREATE TABLE IF NOT EXISTS sim_refunds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    customer_email TEXT NOT NULL,
    amount REAL NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (tenant_id, idempotency_key)
)"""


class SimulatedPayments:
    name = "simulated-payments"
    max_refund = 5_000.0  # hard safety cap even after approval

    def __init__(self, db: Database, tenant_id: str) -> None:
        self.db = db
        self.tenant_id = tenant_id
        self.db.execute(_TABLE)

    def refund(self, customer_email: str, amount: float, idempotency_key: str) -> str:
        if amount <= 0 or amount > self.max_refund:
            raise PermanentError(f"refund amount {amount} outside allowed range")
        row = self.db.one(
            "SELECT id FROM sim_refunds WHERE tenant_id = ? AND idempotency_key = ?",
            (self.tenant_id, idempotency_key),
        )
        if row:
            return f"sim-re-{row['id']}"
        cur = self.db.execute(
            "INSERT INTO sim_refunds (tenant_id, idempotency_key, customer_email, amount, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            (self.tenant_id, idempotency_key, customer_email, amount, utcnow()),
        )
        return f"sim-re-{cur.lastrowid}"
