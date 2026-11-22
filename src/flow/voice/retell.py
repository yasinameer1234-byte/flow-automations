"""Retell phone integration: request signatures and call webhooks.

Retell signs every webhook and custom-function request with ``X-Retell-Signature:
v=<unix ms>,d=<hex>`` where ``d = HMAC-SHA256(key=RETELL_API_KEY, raw_body + timestamp)``
(docs.retellai.com/features/secure-webhook). We verify against the raw bytes, reject
anything older than five minutes, and never log the key.

The webhook turns Retell's ``call_started`` / ``call_ended`` events into rows in our own
``calls`` table, so phone calls show on the owner dashboard exactly like browser calls.
Leads and appointments made during the call are linked through ``source_call_id``, which
the tool endpoints stamp with Retell's ``call_id``.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import re
import time
from datetime import UTC, datetime

from ..core.db import Database
from ..core.models import utcnow
from ..secretary.availability import is_open
from ..secretary.store import Store
from ..secretary.tools import normalize_phone

_SIG = re.compile(r"^v=(\d+),d=([0-9a-fA-F]+)$")
MAX_SKEW_MS = 5 * 60 * 1000


def sign(raw_body: bytes, api_key: str, ts_ms: int | None = None) -> str:
    """Produce a Retell-style signature (used by tests and local tooling)."""
    ts = str(ts_ms if ts_ms is not None else int(time.time() * 1000))
    digest = hmac.new(api_key.encode(), raw_body + ts.encode(), hashlib.sha256).hexdigest()
    return f"v={ts},d={digest}"


def verify_signature(
    raw_body: bytes, header: str | None, api_key: str, now_ms: int | None = None
) -> bool:
    if not header or not api_key:
        return False
    m = _SIG.match(header.strip())
    if not m:
        return False
    ts, digest = m.group(1), m.group(2).lower()
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    if abs(now_ms - int(ts)) > MAX_SKEW_MS:
        return False
    expected = hmac.new(api_key.encode(), raw_body + ts.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, digest)


def _iso(ms: int | None, fallback: datetime) -> str:
    if not ms:
        return fallback.isoformat()
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat()


class RetellWebhook:
    """Applies Retell call events to one tenant. Safe to receive the same event twice."""

    def __init__(self, db: Database, store: Store, clock=None) -> None:
        self.db = db
        self.store = store
        self.clock = clock or (lambda: datetime.now(UTC))

    def handle(self, tenant_id: str, payload: dict) -> str:
        event = payload.get("event")
        call = payload.get("call") or {}
        call_id = call.get("call_id")
        if not call_id or event not in {"call_started", "call_ended", "call_analyzed"}:
            return "ignored"
        if call.get("direction", "inbound") != "inbound":
            return "ignored"
        self._ensure_call(tenant_id, call)
        if event == "call_ended":
            self._end(tenant_id, call)
            return "ended"
        if event == "call_analyzed":
            summary = (call.get("call_analysis") or {}).get("call_summary")
            if summary:
                self.db.execute(
                    "UPDATE calls SET summary = ? WHERE id = ? AND tenant_id = ?",
                    (summary[:1000], call_id, tenant_id),
                )
            return "analyzed"
        return "started"

    def _ensure_call(self, tenant_id: str, call: dict) -> None:
        existing = self.db.one("SELECT tenant_id FROM calls WHERE id = ?", (call["call_id"],))
        if existing:
            if existing["tenant_id"] != tenant_id:
                raise PermissionError("call belongs to another business")
            return
        started = datetime.fromisoformat(_iso(call.get("start_timestamp"), self.clock()))
        profile = self.store.profile(tenant_id)
        self.db.execute(
            "INSERT INTO calls (id, tenant_id, channel, caller_number, started_at, after_hours, "
            "state_json) VALUES (?, ?, 'phone', ?, ?, ?, '{}')",
            (
                call["call_id"],
                tenant_id,
                normalize_phone(call.get("from_number")) or None,
                started.isoformat(),
                int(not is_open(profile, started)),
            ),
        )

    def _end(self, tenant_id: str, call: dict) -> None:
        call_id = call["call_id"]
        row = self.db.one("SELECT * FROM calls WHERE id = ?", (call_id,))
        if row["ended_at"]:
            return  # duplicate delivery
        started = datetime.fromisoformat(row["started_at"])
        ended = datetime.fromisoformat(_iso(call.get("end_timestamp"), self.clock()))
        seconds = max(1, int((ended - started).total_seconds()))
        minutes = math.ceil(seconds / 60)
        for t in call.get("transcript_object") or []:
            speaker = "agent" if t.get("role") == "agent" else "caller"
            if t.get("content"):
                self.db.execute(
                    "INSERT INTO call_turns (call_id, tenant_id, speaker, text, ts) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (call_id, tenant_id, speaker, t["content"][:2000], utcnow()),
                )
        appt = self.db.one(
            "SELECT id, lead_id FROM appointments WHERE tenant_id = ? AND source_call_id = ? "
            "AND status = 'booked' ORDER BY id DESC",
            (tenant_id, call_id),
        )
        lead = self.db.one(
            "SELECT id, status, name, reason FROM leads WHERE tenant_id = ? AND "
            "source_call_id = ? ORDER BY id DESC",
            (tenant_id, call_id),
        )
        caller_turns = [t for t in call.get("transcript_object") or [] if t.get("role") == "user"]
        reason = call.get("disconnection_reason") or ""
        urgent = lead and self.db.one(
            "SELECT 1 FROM tasks WHERE tenant_id = ? AND lead_id = ? AND "
            "kind = 'emergency_callback'",
            (tenant_id, lead["id"]),
        )
        if appt:
            outcome = "booked"
        elif urgent:
            outcome = "emergency"
        elif "transfer" in reason:
            outcome = "transferred"
        elif lead and lead["status"] == "callback":
            outcome = "callback"
        elif caller_turns:
            outcome = "answered"
        else:
            outcome = "abandoned"
        who = (lead or {}).get("name") or "Caller"
        about = f" about: {lead['reason'][:120]}" if lead and lead.get("reason") else ""
        summary = f"{who} called{about}. Outcome: {outcome}."
        self.db.execute(
            "UPDATE calls SET ended_at = ?, duration_seconds = ?, billed_minutes = ?, "
            "outcome = ?, summary = COALESCE(summary, ?), lead_id = ?, appointment_id = ? "
            "WHERE id = ? AND tenant_id = ?",
            (
                ended.isoformat(),
                seconds,
                minutes,
                outcome,
                summary,
                (appt or {}).get("lead_id") or (lead or {}).get("id"),
                (appt or {}).get("id"),
                call_id,
                tenant_id,
            ),
        )
        self.store.add_usage(tenant_id, "voice_minutes", minutes, ref=call_id, when=ended)
