"""Data access for the platform and AI Secretary. Every query is scoped by tenant_id."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from ..core.db import Database
from ..core.models import utcnow
from .schema import PACKAGES, PLATFORM_SCHEMA


def init_platform_schema(db: Database) -> None:
    db._conn.executescript(PLATFORM_SCHEMA)
    # Additive migrations for databases created by earlier versions.
    cols = {r["name"] for r in db.query("PRAGMA table_info(tenants)")}
    if "is_demo" not in cols:
        db.execute("ALTER TABLE tenants ADD COLUMN is_demo INTEGER NOT NULL DEFAULT 0")


@dataclass(frozen=True)
class BusinessProfile:
    tenant_id: str
    timezone: str
    hours: dict[str, list[str]]
    disclosure: str
    recording_enabled: bool
    emergency_keywords: list[str]
    emergency_instructions: str
    avg_job_value: float
    est_close_rate: float
    minutes_saved_per_call: float
    booking_lead_minutes: int
    booking_horizon_days: int


class Store:
    def __init__(self, db: Database) -> None:
        self.db = db
        init_platform_schema(db)

    # ------------------------------------------------------------------ tenants
    def tenant(self, tenant_id: str) -> dict | None:
        return self.db.one("SELECT * FROM tenants WHERE id = ?", (tenant_id,))

    def tenant_by_slug(self, slug: str) -> dict | None:
        return self.db.one("SELECT * FROM tenants WHERE slug = ?", (slug,))

    def tenants(self) -> list[dict]:
        return self.db.query("SELECT * FROM tenants ORDER BY name")

    def package(self, tenant_id: str) -> dict:
        t = self.tenant(tenant_id)
        return {"key": t["package"], **PACKAGES[t["package"]]} if t else {}

    def profile(self, tenant_id: str) -> BusinessProfile:
        r = self.db.one("SELECT * FROM business_profiles WHERE tenant_id = ?", (tenant_id,))
        if not r:
            raise LookupError(f"no business profile for {tenant_id}")
        return BusinessProfile(
            tenant_id=tenant_id,
            timezone=r["timezone"],
            hours=json.loads(r["hours_json"]),
            disclosure=r["disclosure"],
            recording_enabled=bool(r["recording_enabled"]),
            emergency_keywords=json.loads(r["emergency_keywords_json"]),
            emergency_instructions=r["emergency_instructions"],
            avg_job_value=r["avg_job_value"],
            est_close_rate=r["est_close_rate"],
            minutes_saved_per_call=r["minutes_saved_per_call"],
            booking_lead_minutes=r["booking_lead_minutes"],
            booking_horizon_days=r["booking_horizon_days"],
        )

    def services(self, tenant_id: str) -> list[dict]:
        rows = self.db.query("SELECT * FROM services WHERE tenant_id = ? ORDER BY id", (tenant_id,))
        for r in rows:
            r["keywords"] = json.loads(r.pop("keywords_json"))
        return rows

    def service(self, tenant_id: str, service_id: int) -> dict | None:
        return next((s for s in self.services(tenant_id) if s["id"] == service_id), None)

    def knowledge(self, tenant_id: str, approved_only: bool = True) -> list[dict]:
        sql = "SELECT * FROM knowledge_entries WHERE tenant_id = ?"
        if approved_only:
            sql += " AND approved = 1"
        rows = self.db.query(sql + " ORDER BY id", (tenant_id,))
        for r in rows:
            r["keywords"] = json.loads(r.pop("keywords_json"))
        return rows

    def staff(self, tenant_id: str) -> list[dict]:
        return self.db.query(
            "SELECT * FROM staff_members WHERE tenant_id = ? ORDER BY id", (tenant_id,)
        )

    # ------------------------------------------------------------------ automations
    def automation_enabled(self, tenant_id: str, key: str) -> bool:
        r = self.db.one(
            "SELECT enabled FROM automations WHERE tenant_id = ? AND key = ?", (tenant_id, key)
        )
        return bool(r["enabled"]) if r else True

    def set_automation(self, tenant_id: str, key: str, enabled: bool, by: str) -> None:
        self.db.execute(
            "INSERT INTO automations (tenant_id, key, enabled, updated_by, updated_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (tenant_id, key) DO UPDATE SET "
            "enabled = excluded.enabled, updated_by = excluded.updated_by, "
            "updated_at = excluded.updated_at",
            (tenant_id, key, int(enabled), by, utcnow()),
        )

    def automations(self, tenant_id: str) -> list[dict]:
        keys = ["ai_secretary", "booking_confirmations", "revenue_engine"]
        return [{"key": k, "enabled": self.automation_enabled(tenant_id, k)} for k in keys]

    # ------------------------------------------------------------------ usage
    def add_usage(
        self,
        tenant_id: str,
        kind: str,
        qty: float,
        ref: str | None = None,
        when: datetime | None = None,
    ) -> None:
        period = (when or datetime.now(UTC)).strftime("%Y-%m")
        self.db.execute(
            "INSERT INTO usage_ledger (tenant_id, period, kind, quantity, ref, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (tenant_id, period, kind, qty, ref, utcnow()),
        )

    def usage(self, tenant_id: str, period: str | None = None) -> dict[str, float]:
        period = period or datetime.now(UTC).strftime("%Y-%m")
        rows = self.db.query(
            "SELECT kind, SUM(quantity) AS q FROM usage_ledger WHERE tenant_id = ? AND period = ? "
            "GROUP BY kind",
            (tenant_id, period),
        )
        return {r["kind"]: r["q"] for r in rows}
