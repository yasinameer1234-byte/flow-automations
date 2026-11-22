"""CRM integration interface + a local SQLite implementation.

`CRM` is the contract every real connector (HubSpot, Pipedrive, Salesforce,
Airtable, a client's own DB) will implement. The demo uses SqliteCRM, a real
database with synthetic data, standing in for a hosted CRM.
"""

from __future__ import annotations

from typing import Protocol

from ..core.db import Database
from ..core.models import utcnow


class CRM(Protocol):
    def upsert_contact(
        self, email: str, name: str | None, company: str | None, stage: str | None = None
    ) -> int: ...
    def create_deal(self, contact_id: int, title: str, value: float | None, run_id: str) -> int: ...
    def create_ticket(
        self, contact_id: int | None, kind: str, subject: str, priority: str, run_id: str
    ) -> int: ...
    def add_note(self, contact_id: int | None, body: str) -> int: ...
    def get_contact(self, contact_id: int) -> dict | None: ...


class SqliteCRM:
    def __init__(self, db: Database, tenant_id: str) -> None:
        self.db = db
        self.tenant_id = tenant_id

    def upsert_contact(
        self, email: str, name: str | None, company: str | None, stage: str | None = None
    ) -> int:
        email = email.strip().lower()
        now = utcnow()
        existing = self.db.one(
            "SELECT id, lifecycle_stage FROM crm_contacts WHERE tenant_id = ? AND email = ?",
            (self.tenant_id, email),
        )
        if existing:
            # Never downgrade an existing customer back to 'lead'.
            new_stage = existing["lifecycle_stage"]
            if stage and not (existing["lifecycle_stage"] == "customer" and stage == "lead"):
                new_stage = stage
            self.db.execute(
                "UPDATE crm_contacts SET name = COALESCE(?, name), company = COALESCE(?, company),"
                " lifecycle_stage = ?, updated_at = ? WHERE id = ?",
                (name, company, new_stage, now, existing["id"]),
            )
            return int(existing["id"])
        cur = self.db.execute(
            "INSERT INTO crm_contacts (tenant_id, email, name, company, lifecycle_stage, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (self.tenant_id, email, name, company, stage or "lead", now, now),
        )
        return int(cur.lastrowid)

    def create_deal(self, contact_id: int, title: str, value: float | None, run_id: str) -> int:
        cur = self.db.execute(
            "INSERT INTO crm_deals (tenant_id, contact_id, title, value, source_run_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (self.tenant_id, contact_id, title, value, run_id, utcnow()),
        )
        return int(cur.lastrowid)

    def create_ticket(
        self, contact_id: int | None, kind: str, subject: str, priority: str, run_id: str
    ) -> int:
        cur = self.db.execute(
            "INSERT INTO crm_tickets (tenant_id, contact_id, kind, subject, priority, "
            "source_run_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (self.tenant_id, contact_id, kind, subject, priority, run_id, utcnow()),
        )
        return int(cur.lastrowid)

    def add_note(self, contact_id: int | None, body: str) -> int:
        cur = self.db.execute(
            "INSERT INTO crm_notes (tenant_id, contact_id, body, created_at) VALUES (?, ?, ?, ?)",
            (self.tenant_id, contact_id, body, utcnow()),
        )
        return int(cur.lastrowid)

    def get_contact(self, contact_id: int) -> dict | None:
        contact = self.db.one(
            "SELECT * FROM crm_contacts WHERE id = ? AND tenant_id = ?",
            (contact_id, self.tenant_id),
        )
        if not contact:
            return None
        contact["deals"] = self.db.query(
            "SELECT id, title, value, stage FROM crm_deals WHERE contact_id = ?", (contact_id,)
        )
        contact["tickets"] = self.db.query(
            "SELECT id, kind, subject, priority, status FROM crm_tickets WHERE contact_id = ?",
            (contact_id,),
        )
        return contact

    def summary(self) -> dict:
        def count(table: str) -> int:
            row = self.db.one(
                f"SELECT COUNT(*) AS n FROM {table} WHERE tenant_id = ?",  # noqa: S608
                (self.tenant_id,),
            )
            return int(row["n"]) if row else 0

        return {t: count(f"crm_{t}") for t in ("contacts", "deals", "tickets", "notes")}
