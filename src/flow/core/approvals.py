"""Human-in-the-loop approvals.

A workflow never performs a consequential action (sending an email to a customer,
issuing a refund) directly. It files a ProposedAction here. A person approves or
rejects it via API/CLI; only an approval releases the action to the executor.
"""

from __future__ import annotations

import json
import uuid

from .db import Database
from .models import ProposedAction, utcnow


class ApprovalError(Exception):
    pass


class ApprovalStore:
    def __init__(self, db: Database, tenant_id: str) -> None:
        self.db = db
        self.tenant_id = tenant_id

    def request(self, run_id: str, proposal: ProposedAction) -> str:
        approval_id = f"apr_{uuid.uuid4().hex[:12]}"
        self.db.execute(
            "INSERT INTO approvals (id, tenant_id, run_id, action, params_json, reason, status, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
            (
                approval_id,
                self.tenant_id,
                run_id,
                proposal.action,
                json.dumps(proposal.params),
                proposal.reason,
                utcnow(),
            ),
        )
        return approval_id

    def get(self, approval_id: str) -> dict:
        row = self.db.one(
            "SELECT * FROM approvals WHERE id = ? AND tenant_id = ?", (approval_id, self.tenant_id)
        )
        if not row:
            raise ApprovalError(f"approval {approval_id} not found")
        row["params"] = json.loads(row.pop("params_json"))
        return row

    def pending(self) -> list[dict]:
        rows = self.db.query(
            "SELECT * FROM approvals WHERE tenant_id = ? AND status = 'pending' "
            "ORDER BY created_at",
            (self.tenant_id,),
        )
        for r in rows:
            r["params"] = json.loads(r.pop("params_json"))
        return rows

    def decide(self, approval_id: str, *, approve: bool, decided_by: str, note: str = "") -> dict:
        if not decided_by:
            raise ApprovalError("decided_by is required")
        # Conditional update = only one decision can ever win (no double-approve races).
        cur = self.db.execute(
            "UPDATE approvals SET status = ?, decided_by = ?, decision_note = ?, decided_at = ? "
            "WHERE id = ? AND tenant_id = ? AND status = 'pending'",
            (
                "approved" if approve else "rejected",
                decided_by,
                note,
                utcnow(),
                approval_id,
                self.tenant_id,
            ),
        )
        if cur.rowcount != 1:
            current = self.get(approval_id)
            raise ApprovalError(f"approval {approval_id} is already {current['status']}")
        return self.get(approval_id)

    def mark(self, approval_id: str, status: str, note: str | None = None) -> None:
        self.db.execute(
            "UPDATE approvals SET status = ?, decision_note = COALESCE(?, decision_note) "
            "WHERE id = ? AND tenant_id = ?",
            (status, note, approval_id, self.tenant_id),
        )
