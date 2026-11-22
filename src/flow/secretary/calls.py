"""Call sessions: start, take turns, end. Records transcript, outcome, minutes and usage."""

from __future__ import annotations

import json
import math
import uuid
from datetime import UTC, datetime

from ..core.db import Database
from ..core.models import utcnow
from .availability import is_open
from .dialogue import CallState, Receptionist
from .store import Store
from .tools import Clock, SecretaryTools


class CallError(Exception):
    pass


class CallService:
    def __init__(
        self, db: Database, store: Store, clock: Clock | None = None, tools_factory=None
    ) -> None:
        self.db = db
        self.store = store
        self.clock = clock or (lambda: datetime.now(UTC))
        self.tools_factory = tools_factory or (
            lambda tenant_id: SecretaryTools(db, store, tenant_id, clock=self.clock)
        )

    def _turn(self, call: dict, speaker: str, text: str) -> None:
        self.db.execute(
            "INSERT INTO call_turns (call_id, tenant_id, speaker, text, ts) VALUES (?, ?, ?, ?, ?)",
            (call["id"], call["tenant_id"], speaker, text, utcnow()),
        )

    def get(self, tenant_id: str, call_id: str) -> dict:
        call = self.db.one(
            "SELECT * FROM calls WHERE id = ? AND tenant_id = ?", (call_id, tenant_id)
        )
        if not call:
            raise CallError("call not found")
        return call

    def transcript(self, tenant_id: str, call_id: str) -> list[dict]:
        return self.db.query(
            "SELECT speaker, text, ts FROM call_turns WHERE call_id = ? AND "
            "tenant_id = ? ORDER BY id",
            (call_id, tenant_id),
        )

    # ------------------------------------------------------------------ lifecycle
    def start(
        self, tenant_id: str, channel: str = "browser_demo", caller_number: str | None = None
    ) -> dict:
        tenant = self.store.tenant(tenant_id)
        if not tenant or tenant["status"] != "active":
            raise CallError("unknown or inactive business")
        profile = self.store.profile(tenant_id)
        now = self.clock()
        call_id = f"call_{uuid.uuid4().hex[:12]}"
        after_hours = not is_open(profile, now)
        self.db.execute(
            "INSERT INTO calls (id, tenant_id, channel, caller_number, started_at, after_hours, "
            "state_json) VALUES (?, ?, ?, ?, ?, ?, '{}')",
            (call_id, tenant_id, channel, caller_number, now.isoformat(), int(after_hours)),
        )
        call = {"id": call_id, "tenant_id": tenant_id}
        if not self.store.automation_enabled(tenant_id, "ai_secretary"):
            reply = (
                f"Thanks for calling {tenant['name']}. We can't take your call right now. "
                "Please call back during business hours."
            )
            self._turn(call, "agent", reply)
            self._finish(tenant_id, call_id, CallState(ended=True), outcome="disabled")
            return {"call_id": call_id, "reply": reply, "ended": True}
        hours_note = (
            " We're closed right now, but I can still book you in or take a message."
            if after_hours
            else ""
        )
        reply = (
            f"{profile.disclosure} Thanks for calling {tenant['name']}.{hours_note} "
            "How can I help you today?"
        )
        self._turn(call, "agent", reply)
        return {"call_id": call_id, "reply": reply, "ended": False}

    def turn(self, tenant_id: str, call_id: str, text: str) -> dict:
        call = self.get(tenant_id, call_id)
        if call["ended_at"]:
            raise CallError("call already ended")
        text = (text or "")[:1000]
        state = CallState.from_dict(json.loads(call["state_json"]))
        self._turn(call, "caller", text)
        tenant = self.store.tenant(tenant_id)
        agent = Receptionist(self.tools_factory(tenant_id), tenant["name"], call_id)
        reply = agent.step(state, text)
        self._turn(call, "agent", reply)
        self.db.execute(
            "UPDATE calls SET state_json = ? WHERE id = ?", (json.dumps(state.to_dict()), call_id)
        )
        if state.ended:
            self._finish(tenant_id, call_id, state)
        return {
            "call_id": call_id,
            "reply": reply,
            "ended": state.ended,
            "transferred_to": state.transferred_to,
        }

    def end(self, tenant_id: str, call_id: str) -> dict:
        call = self.get(tenant_id, call_id)
        if not call["ended_at"]:
            self._finish(tenant_id, call_id, CallState.from_dict(json.loads(call["state_json"])))
        return self.get(tenant_id, call_id)

    def expire_stale(self, tenant_id: str, max_minutes: int = 30) -> int:
        """Close calls left open (browser closed, dropped connection) as abandoned."""
        from datetime import timedelta

        cutoff = (self.clock() - timedelta(minutes=max_minutes)).isoformat()
        stale = self.db.query(
            "SELECT id, state_json FROM calls WHERE tenant_id = ? AND "
            "ended_at IS NULL AND started_at < ?",
            (tenant_id, cutoff),
        )
        for c in stale:
            self._finish(tenant_id, c["id"], CallState.from_dict(json.loads(c["state_json"])))
        return len(stale)

    def _finish(
        self, tenant_id: str, call_id: str, s: CallState, outcome: str | None = None
    ) -> None:
        call = self.get(tenant_id, call_id)
        if call["ended_at"]:
            return
        now = self.clock()
        started = datetime.fromisoformat(call["started_at"])
        seconds = max(1, int((now - started).total_seconds()))
        minutes = math.ceil(seconds / 60)
        outcome = outcome or self._outcome(s)
        summary = self._summary(s, outcome)
        self.db.execute(
            "UPDATE calls SET ended_at = ?, duration_seconds = ?, billed_minutes = ?, outcome = ?,"
            " summary = ?, lead_id = ?, appointment_id = ? WHERE id = ? AND tenant_id = ?",
            (
                now.isoformat(),
                seconds,
                minutes,
                outcome,
                summary,
                s.lead_id,
                s.appointment_id,
                call_id,
                tenant_id,
            ),
        )
        if outcome != "disabled":
            self.store.add_usage(tenant_id, "voice_minutes", minutes, ref=call_id, when=now)
        if s.lead_id is None and (s.name or s.phone) and outcome == "abandoned":
            tools = self.tools_factory(tenant_id)
            lead_id = tools.save_lead(
                name=s.name,
                phone=s.phone,
                reason=s.reason,
                service_id=s.service_id,
                status="new",
                qualified=False,
                call_id=call_id,
            )
            self.db.execute("UPDATE calls SET lead_id = ? WHERE id = ?", (lead_id, call_id))

    @staticmethod
    def _outcome(s: CallState) -> str:
        if s.appointment_id:
            return "booked"
        if s.goal == "emergency":
            return "emergency"
        if s.transferred_to:
            return "transferred"
        if s.task_id:
            return "callback"
        if s.goal == "cancel" and s.stage == "anything_else":
            return "cancelled"
        if s.questions_answered:
            return "answered"
        return "abandoned"

    @staticmethod
    def _summary(s: CallState, outcome: str) -> str:
        who = s.name or "Unknown caller"
        if s.phone:
            who += f" ({s.phone[:3]}-{s.phone[3:6]}-{s.phone[6:]})"
        about = f" about: {s.reason[:120]}" if s.reason else ""
        extra = f" Transferred to {s.transferred_to}." if s.transferred_to else ""
        err = " Booking system error during call." if s.integration_error else ""
        return f"{who} called{about}. Outcome: {outcome}.{extra}{err}"
