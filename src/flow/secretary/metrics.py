"""Financial impact metrics for the customer dashboard.

Verified figures count only money a person confirmed (a lead marked won with a
value). Estimated figures are labelled as estimates and show their assumptions.
Captured lead values are never reported as revenue.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ..core.db import Database
from .store import Store


def tenant_metrics(
    db: Database, store: Store, tenant_id: str, days: int = 30, now: datetime | None = None
) -> dict:
    now = now or datetime.now(UTC)
    since = (now - timedelta(days=days)).isoformat()
    p = store.profile(tenant_id)
    pkg = store.package(tenant_id)

    def scalar(sql: str, *args) -> float:
        r = db.one(sql, (tenant_id, since, *args))
        return float(list(r.values())[0] or 0) if r else 0.0

    total = scalar("SELECT COUNT(*) FROM calls WHERE tenant_id = ? AND started_at >= ?")
    missed = scalar(
        "SELECT COUNT(*) FROM calls WHERE tenant_id = ? AND started_at >= ? "
        "AND outcome IN ('disabled', 'abandoned')"
    )
    after_hours = scalar(
        "SELECT COUNT(*) FROM calls WHERE tenant_id = ? AND started_at >= ? "
        "AND after_hours = 1 AND outcome != 'disabled'"
    )
    leads = scalar(
        "SELECT COUNT(*) FROM leads WHERE tenant_id = ? AND created_at >= ? AND qualified = 1"
    )
    booked = scalar(
        "SELECT COUNT(*) FROM appointments WHERE tenant_id = ? AND created_at >= ? "
        "AND source_call_id IS NOT NULL"
    )
    messages = scalar(
        "SELECT COUNT(*) FROM messages WHERE tenant_id = ? AND created_at >= ? "
        "AND status IN ('sent_simulated', 'sent', 'delivered')"
    )
    callbacks = scalar(
        "SELECT COUNT(*) FROM calls WHERE tenant_id = ? AND started_at >= ? "
        "AND outcome IN ('callback', 'emergency', 'transferred')"
    )
    verified = scalar(
        "SELECT SUM(confirmed_value) FROM leads WHERE tenant_id = ? AND "
        "updated_at >= ? AND status = 'won'"
    )
    won = scalar(
        "SELECT COUNT(*) FROM leads WHERE tenant_id = ? AND updated_at >= ? AND status = 'won'"
    )
    answered = total - missed
    estimated = booked * p.avg_job_value * p.est_close_rate
    cost = pkg["monthly_cad"] * days / 30
    usage = store.usage(tenant_id, now.strftime("%Y-%m"))
    minutes = usage.get("voice_minutes", 0)
    return {
        "days": days,
        "calls_total": int(total),
        "calls_answered": int(answered),
        "calls_missed": int(missed),
        "after_hours_answered": int(after_hours),
        "qualified_leads": int(leads),
        "appointments_booked": int(booked),
        "messages_sent": int(messages),
        "messages_real": int(
            scalar(
                "SELECT COUNT(*) FROM messages WHERE tenant_id = ? AND created_at >= ? "
                "AND status IN ('sent', 'delivered')"
            )
        ),
        "handoffs": int(callbacks),
        "jobs_won": int(won),
        "verified_revenue": verified,
        "estimated_revenue": estimated,
        "assumptions": {
            "avg_job_value": p.avg_job_value,
            "close_rate": p.est_close_rate,
            "minutes_saved_per_call": p.minutes_saved_per_call,
        },
        "hours_saved_estimate": round(answered * p.minutes_saved_per_call / 60, 1),
        "subscription_cost": round(cost, 2),
        "package": pkg,
        "verified_roi": round(verified / cost, 1) if cost else None,
        "estimated_roi": round(estimated / cost, 1) if cost else None,
        "minutes_used": minutes,
        "minutes_allowance": pkg["minutes"],
        "minutes_pct": round(100 * minutes / pkg["minutes"]) if pkg["minutes"] else 0,
    }
