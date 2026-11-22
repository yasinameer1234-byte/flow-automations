"""The AI Secretary's tools.

Both the browser demo's dialogue engine and a hosted voice agent (Retell custom
functions) call these. They are the only way the agent learns facts or changes
anything, which is how we guarantee it never invents availability, prices or
policies:

* times come only from `check_availability`
* a booking exists only if `book_appointment` wrote it
* answers come only from APPROVED knowledge-base entries
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from ..core.audit import AuditLog
from ..core.db import Database
from .availability import Slot, available_slots, parse_slot_id
from .store import Store

Clock = Callable[[], datetime]
_WORD = re.compile(r"[a-z0-9']+")
_STOP = {
    "the",
    "a",
    "an",
    "do",
    "you",
    "your",
    "is",
    "are",
    "what",
    "how",
    "i",
    "my",
    "to",
    "of",
    "for",
    "and",
    "can",
    "does",
    "it",
    "in",
    "on",
    "we",
    "me",
    "any",
}


def normalize_phone(raw: str | None) -> str | None:
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else None


def format_phone(digits: str) -> str:
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"


class ToolError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SecretaryTools:
    def __init__(self, db: Database, store: Store, tenant_id: str, clock: Clock | None = None):
        self.db = db
        self.store = store
        self.tenant_id = tenant_id
        self.clock = clock or (lambda: datetime.now(UTC))
        self.audit = AuditLog(db, tenant_id)
        self.profile = store.profile(tenant_id)

    # ------------------------------------------------------------------ lookup
    def match_service(self, text: str) -> dict | None:
        words = set(_WORD.findall(text.lower()))
        best, score = None, 0
        for s in self.store.services(self.tenant_id):
            hits = sum(
                1 for k in s["keywords"] if (k in words if " " not in k else k in text.lower())
            )
            if hits > score:
                best, score = s, hits
        return best

    def default_service(self) -> dict:
        return next(s for s in self.store.services(self.tenant_id) if s["bookable"])

    def answer_question(self, question: str, call_id: str | None = None) -> dict:
        q = set(_WORD.findall(question.lower())) - _STOP
        best, score = None, 0
        for e in self.store.knowledge(self.tenant_id, approved_only=True):
            s = len(q & set(e["keywords"]))
            if s > score:
                best, score = e, s
        self.audit.record(
            "tool.answer_question",
            run_id=call_id,
            matched=bool(best),
            entry_id=best["id"] if best else None,
        )
        return {
            "answer": best["answer"] if best else None,
            "entry_id": best["id"] if best else None,
        }

    # ------------------------------------------------------------------ availability
    def check_availability(
        self,
        service_id: int | None = None,
        on_date=None,
        part_of_day: str | None = None,
        limit: int = 3,
        call_id: str | None = None,
        exclude: set[str] | None = None,
    ) -> dict:
        service = (
            self.store.service(self.tenant_id, service_id) if service_id else self.default_service()
        )
        if not service or not service["bookable"]:
            raise ToolError("not_bookable", "that service can't be booked by phone")
        slots = available_slots(
            self.db,
            self.profile,
            service["duration_minutes"],
            self.clock(),
            on_date=on_date,
            part_of_day=part_of_day,
            limit=limit + 20,
        )
        slots = [s for s in slots if not exclude or s.id not in exclude][:limit]
        self.audit.record(
            "tool.check_availability",
            run_id=call_id,
            service=service["name"],
            date=str(on_date) if on_date else None,
            part=part_of_day,
            offered=[s.id for s in slots],
        )
        return {
            "service_id": service["id"],
            "service": service["name"],
            "slots": [{"slot_id": s.id, "label": s.label()} for s in slots],
        }

    def _slot_still_free(self, slot_id: str, duration: int) -> Slot | None:
        start = parse_slot_id(slot_id)
        day = start.astimezone(ZoneInfo(self.profile.timezone)).date()
        for s in available_slots(
            self.db, self.profile, duration, self.clock(), on_date=day, limit=1000
        ):
            if s.id == slot_id:
                return s
        return None

    # ------------------------------------------------------------------ booking
    def book_appointment(
        self,
        slot_id: str,
        name: str,
        phone: str,
        service_id: int,
        lead_id: int | None = None,
        call_id: str | None = None,
    ) -> dict:
        if not self.store.automation_enabled(self.tenant_id, "ai_secretary"):
            raise ToolError("disabled", "booking is switched off for this business")
        digits = normalize_phone(phone)
        if not digits:
            raise ToolError("bad_phone", "a 10-digit phone number is required")
        if not name or not name.strip():
            raise ToolError("bad_name", "a name is required")
        service = self.store.service(self.tenant_id, service_id)
        if not service or not service["bookable"]:
            raise ToolError("not_bookable", "unknown or unbookable service")
        try:
            slot = self._slot_still_free(slot_id, service["duration_minutes"])
        except ValueError as e:
            raise ToolError("bad_slot", "malformed slot id") from e
        if not slot:
            self.audit.record(
                "tool.book_rejected",
                run_id=call_id,
                level="warning",
                slot=slot_id,
                reason="not available",
            )
            raise ToolError("slot_unavailable", "that time is no longer available")
        try:
            cur = self.db.execute(
                "INSERT INTO appointments (tenant_id, lead_id, service_id, customer_name, "
                "customer_phone, starts_at, ends_at, source_call_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self.tenant_id,
                    lead_id,
                    service_id,
                    name.strip(),
                    digits,
                    slot.id,
                    slot.end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    call_id,
                    self.clock().isoformat(),
                ),
            )
        except sqlite3.IntegrityError as e:  # double booking race: the DB is the referee
            raise ToolError("slot_unavailable", "that time was just taken") from e
        appt_id = int(cur.lastrowid)
        self.audit.record(
            "tool.book_appointment",
            run_id=call_id,
            appointment_id=appt_id,
            slot=slot.id,
            service=service["name"],
        )
        return {"appointment_id": appt_id, "label": slot.label(), "service": service["name"]}

    def find_appointment(self, phone: str) -> dict:
        digits = normalize_phone(phone)
        appt = self.db.one(
            "SELECT * FROM appointments WHERE tenant_id = ? AND customer_phone = ? AND "
            "status = 'booked' AND starts_at > ? ORDER BY starts_at LIMIT 1",
            (self.tenant_id, digits, self.clock().strftime("%Y-%m-%dT%H:%M:%SZ")),
        )
        if not appt:
            raise ToolError("not_found", "no upcoming appointment for that number")
        slot = Slot(
            parse_slot_id(appt["starts_at"]), parse_slot_id(appt["ends_at"]), self.profile.timezone
        )
        return {
            "appointment_id": appt["id"],
            "label": slot.label(),
            "service_id": appt["service_id"],
            "name": appt["customer_name"],
        }

    def cancel_appointment(
        self, phone: str, appointment_id: int | None = None, call_id: str | None = None
    ) -> dict:
        if appointment_id:
            appt = self.db.one(
                "SELECT * FROM appointments WHERE id = ? AND tenant_id = ? AND status = 'booked'",
                (appointment_id, self.tenant_id),
            )
            if not appt:
                raise ToolError("not_found", "appointment not found")
        else:
            appt = self.db.one(
                "SELECT * FROM appointments WHERE id = ?",
                (self.find_appointment(phone)["appointment_id"],),
            )
        self.db.execute("UPDATE appointments SET status = 'cancelled' WHERE id = ?", (appt["id"],))
        self.audit.record("tool.cancel_appointment", run_id=call_id, appointment_id=appt["id"])
        slot = Slot(
            parse_slot_id(appt["starts_at"]), parse_slot_id(appt["ends_at"]), self.profile.timezone
        )
        return {
            "appointment_id": appt["id"],
            "label": slot.label(),
            "service_id": appt["service_id"],
        }

    # ------------------------------------------------------------------ people
    def transfer_call(
        self, reason: str, emergency: bool = False, call_id: str | None = None
    ) -> dict:
        staff = [
            s
            for s in self.store.staff(self.tenant_id)
            if s["available"] and (s["handles_emergencies"] or not emergency)
        ]
        if not staff:
            self.audit.record(
                "tool.transfer_failed",
                run_id=call_id,
                level="warning",
                reason=reason,
                emergency=emergency,
            )
            return {"transferred": False}
        target = staff[0]
        self.audit.record(
            "tool.transfer_call",
            run_id=call_id,
            to=target["name"],
            emergency=emergency,
            simulated=True,
        )
        return {"transferred": True, "to": target["name"]}

    def save_lead(
        self,
        *,
        name: str | None,
        phone: str | None,
        reason: str | None,
        service_id: int | None,
        status: str,
        qualified: bool,
        call_id: str | None = None,
        lead_id: int | None = None,
    ) -> int:
        now = self.clock().isoformat()
        est = self.profile.avg_job_value if qualified else None
        digits = normalize_phone(phone)
        if lead_id:
            self.db.execute(
                "UPDATE leads SET name = COALESCE(?, name), phone = COALESCE(?, phone), "
                "reason = COALESCE(?, reason), service_id = COALESCE(?, service_id), "
                "status = ?, qualified = ?, estimated_value = ?, updated_at = ? "
                "WHERE id = ? AND tenant_id = ?",
                (
                    name,
                    digits,
                    reason,
                    service_id,
                    status,
                    int(qualified),
                    est,
                    now,
                    lead_id,
                    self.tenant_id,
                ),
            )
            return lead_id
        cur = self.db.execute(
            "INSERT INTO leads (tenant_id, name, phone, reason, service_id, status, qualified, "
            "estimated_value, source, source_call_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ai_secretary', ?, ?, ?)",
            (
                self.tenant_id,
                name,
                digits,
                reason,
                service_id,
                status,
                int(qualified),
                est,
                call_id,
                now,
                now,
            ),
        )
        self.audit.record("tool.save_lead", run_id=call_id, lead_id=cur.lastrowid, status=status)
        return int(cur.lastrowid)

    def create_task(
        self, kind: str, title: str, lead_id: int | None, call_id: str | None = None
    ) -> int:
        cur = self.db.execute(
            "INSERT INTO tasks (tenant_id, kind, title, lead_id, created_at) VALUES (?,?,?,?,?)",
            (self.tenant_id, kind, title, lead_id, self.clock().isoformat()),
        )
        self.audit.record("tool.create_task", run_id=call_id, kind=kind, task_id=cur.lastrowid)
        return int(cur.lastrowid)

    def send_confirmation(self, appointment_id: int, call_id: str | None = None) -> str:
        """Transactional SMS confirming a booking the caller just asked for (simulated)."""
        appt = self.db.one(
            "SELECT * FROM appointments WHERE id = ? AND tenant_id = ?",
            (appointment_id, self.tenant_id),
        )
        tenant = self.store.tenant(self.tenant_id)
        slot = Slot(
            parse_slot_id(appt["starts_at"]), parse_slot_id(appt["ends_at"]), self.profile.timezone
        )
        body = (
            f"{tenant['name']}: your appointment is confirmed for {slot.label()}. "
            f"Reply C to cancel. Reply STOP to opt out."
        )
        status = (
            "sent_simulated"
            if self.store.automation_enabled(self.tenant_id, "booking_confirmations")
            else "suppressed"
        )
        self.db.execute(
            "INSERT INTO messages (tenant_id, channel, to_addr, body, purpose, status, "
            "related_appointment_id, created_at) VALUES (?, 'sms', ?, ?, 'transactional', ?, ?, ?)",
            (
                self.tenant_id,
                appt["customer_phone"],
                body,
                status,
                appointment_id,
                self.clock().isoformat(),
            ),
        )
        if status == "sent_simulated":
            self.store.add_usage(self.tenant_id, "sms", 1, ref=str(appointment_id))
        self.audit.record(
            "tool.send_confirmation", run_id=call_id, status=status, appointment_id=appointment_id
        )
        return status
