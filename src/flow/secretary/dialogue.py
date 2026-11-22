"""Simulated receptionist: a slot-filling dialogue that only speaks from tool results.

Every fact in a reply comes from the tenant's profile or a tool call. There is no
free-text generation, so it cannot invent availability, prices or policies. In
production, a hosted voice agent (Retell) replaces this class and calls the same
tools; this class keeps the browser demo and the tests free and deterministic.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from zoneinfo import ZoneInfo

from . import nlu
from .availability import parse_slot_id
from .tools import SecretaryTools, ToolError, format_phone, normalize_phone


@dataclass
class CallState:
    stage: str = "start"
    goal: str | None = None  # book | callback | emergency | transfer | reschedule | cancel
    service_id: int | None = None
    reason: str | None = None
    name: str | None = None
    phone: str | None = None
    day: str | None = None  # ISO date
    part: str | None = None
    offered: list[dict] = field(default_factory=list)
    shown: list[str] = field(default_factory=list)
    pending_slot: int | None = None
    lead_id: int | None = None
    appointment_id: int | None = None
    old_appointment_id: int | None = None
    transferred_to: str | None = None
    task_id: int | None = None
    questions_answered: int = 0
    misunderstood: int = 0
    integration_error: bool = False
    ended: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> CallState:
        return cls(**d) if d else cls()


class Receptionist:
    def __init__(self, tools: SecretaryTools, business_name: str, call_id: str) -> None:
        self.t = tools
        self.business = business_name
        self.call_id = call_id
        self.tz = ZoneInfo(tools.profile.timezone)

    # ------------------------------------------------------------------ entry
    def step(self, s: CallState, text: str) -> str:
        text = (text or "").strip()
        if not text:
            return "Sorry, I didn't catch that. Could you say it again?"
        if (
            s.stage not in ("collect_emergency",)
            and nlu.is_emergency(text, self.t.profile.emergency_keywords)
            and s.goal != "emergency"
        ):
            return self._emergency(s, text)
        handler = getattr(self, f"_stage_{s.stage}")
        try:
            return handler(s, text)
        except Exception as e:  # calendar/CRM outage: never leave the caller hanging
            return self._integration_failure(s, e)

    # ------------------------------------------------------------------ stages
    def _stage_start(self, s: CallState, text: str) -> str:
        self._absorb(s, text)
        if nlu.wants_human(text):
            return self._transfer(s, text)
        if nlu.wants_reschedule(text):
            s.goal = "reschedule"
            return self._ask_or_continue(s)
        if nlu.wants_cancel(text):
            s.goal = "cancel"
            return self._ask_or_continue(s)
        service = self.t.match_service(text)
        if nlu.is_question(text) and not (service and nlu.wants_booking(text)):
            res = self.t.answer_question(text, call_id=self.call_id)
            if res["answer"] or not nlu.wants_booking(text):
                return self._answer(s, text, res)
        if nlu.wants_booking(text) or (service and not nlu.is_question(text)):
            s.goal = "book"
            ack = ""
            if service:
                s.service_id, s.reason = service["id"], s.reason or text
                ack = f"I can book a {service['name'].lower()} visit for that. "
            return self._ask_or_continue(s, prefix=ack)
        if nlu.is_question(text):
            return self._answer(s, text)
        s.misunderstood += 1
        if s.misunderstood >= 2:
            s.goal = "callback"
            return self._ask_or_continue(
                s, prefix="Let me have someone from the team call you back. "
            )
        return (
            "I can book a visit, answer questions about our services, or connect you with "
            "the team. What can I help you with?"
        )

    def _stage_ask_reason(self, s, text):
        service = self.t.match_service(text)
        s.reason = text
        if service:
            s.service_id = service["id"]
        self._absorb(s, text)
        return self._ask_or_continue(s)

    def _stage_ask_name(self, s, text):
        s.name = nlu.extract_name(text, expecting=True) or s.name
        self._absorb(s, text, name=False)
        if not s.name:
            return "Sorry, could I get your first and last name?"
        return self._ask_or_continue(s)

    def _stage_ask_phone(self, s, text):
        phone = nlu.extract_phone(text)
        if not phone:
            return "Sorry, I need a 10-digit phone number. What's the best number to reach you?"
        s.phone = phone
        self._absorb(s, text, name=False)
        return self._ask_or_continue(s)

    def _stage_ask_time(self, s, text):
        self._absorb(s, text)
        return self._offer(s)

    def _stage_offer(self, s, text):
        starts = [parse_slot_id(o["slot_id"]).astimezone(self.tz) for o in s.offered]
        idx = nlu.choose_slot(text, s.offered, starts)
        if idx is not None:
            s.pending_slot = idx
            s.stage = "confirm"
            o = s.offered[idx]
            return (
                f"Just to confirm: {self._service_name(s)} for {s.name} on {o['label']}, "
                f"and we'll text {format_phone(s.phone)}. Shall I book it?"
            )
        if nlu.extract_day(text, self._today()) or nlu.extract_part(text):
            s.shown = []
            self._absorb(s, text)
            return self._offer(s)
        if nlu.wants_other_times(text) or nlu.is_no(text):
            return self._offer(s, more=True)
        return "Which of those times works for you? You can say the first, second or third."

    def _stage_confirm(self, s, text):
        if nlu.is_yes(text):
            return self._book(s)
        if nlu.is_no(text):
            s.stage = "offer"
            return "No problem. " + self._offer(s, more=True)
        return "Should I go ahead and book that time? Please say yes or no."

    def _stage_anything_else(self, s, text):
        if nlu.is_done(text) or (nlu.is_no(text) and not nlu.is_question(text)):
            s.ended = True
            return f"Thanks for calling {self.business}. Have a great day!"
        if nlu.wants_human(text):
            return self._transfer(s, text)
        if s.appointment_id is None and (nlu.wants_booking(text) or nlu.is_yes(text)):
            s.goal = "book"
            service = self.t.match_service(text)
            if service:
                s.service_id = service["id"]
            return self._ask_or_continue(s)
        if nlu.is_question(text):
            return self._answer(s, text)
        s.ended = True
        return f"Thanks for calling {self.business}. Have a great day!"

    def _stage_collect_emergency(self, s, text):
        self._absorb(s, text)
        if not s.name:
            s.name = nlu.extract_name(text, expecting=True)
        if not s.phone:
            s.phone = nlu.extract_phone(text)
        if not s.name:
            return "What's your name?"
        if not s.phone:
            return "And the best 10-digit number to reach you?"
        return self._finish_callback(s, urgent=True)

    def _stage_done(self, s, text):
        s.ended = True
        return f"Thanks for calling {self.business}. Goodbye!"

    # ------------------------------------------------------------------ flows
    def _ask_or_continue(self, s: CallState, prefix: str = "") -> str:
        if s.goal in ("cancel", "reschedule"):
            if not s.phone:
                s.stage = "ask_phone"
                return prefix + "Sure. What's the phone number the appointment is under?"
            return prefix + self._handle_existing(s)
        if s.goal == "book" and not s.service_id and not s.reason:
            s.stage = "ask_reason"
            return prefix + "Happy to help with that. What do you need help with?"
        if s.goal == "book" and not s.service_id:
            s.service_id = self.t.default_service()["id"]
        if not s.name:
            s.stage = "ask_name"
            return prefix + "Can I get your first and last name?"
        if not s.phone:
            s.stage = "ask_phone"
            return prefix + f"Thanks, {s.name.split()[0]}. What's the best number to reach you?"
        if s.goal == "callback":
            return prefix + self._finish_callback(s, urgent=False)
        if not s.day and not s.part and s.stage != "ask_time":
            s.stage = "ask_time"
            return prefix + "What day works best for you? I can also give you our earliest opening."
        return prefix + self._offer(s)

    def _offer(self, s: CallState, more: bool = False) -> str:
        from datetime import date

        on_date = date.fromisoformat(s.day) if s.day else None
        exclude = set(s.shown) if more else set()
        res = self.t.check_availability(
            s.service_id, on_date=on_date, part_of_day=s.part, call_id=self.call_id, exclude=exclude
        )
        if not res["slots"] and (on_date or s.part):
            s.day, s.part = None, None
            res = self.t.check_availability(s.service_id, call_id=self.call_id, exclude=exclude)
            lead = "I don't have anything then. "
        else:
            lead = ""
        if not res["slots"]:
            s.goal = "callback"
            return lead + (
                "I don't have any openings in the next week. "
                + self._finish_callback(s, urgent=False)
            )
        s.offered = res["slots"]
        s.shown = list(set(s.shown) | {o["slot_id"] for o in res["slots"]})
        s.stage = "offer"
        options = _spoken_options(res["slots"])
        return f"{lead}For {res['service'].lower()}, I have {options}. Which works best?"

    def _book(self, s: CallState) -> str:
        o = s.offered[s.pending_slot]
        s.lead_id = self.t.save_lead(
            name=s.name,
            phone=s.phone,
            reason=s.reason,
            service_id=s.service_id,
            status="new",
            qualified=True,
            call_id=self.call_id,
            lead_id=s.lead_id,
        )
        try:
            appt = self.t.book_appointment(
                o["slot_id"], s.name, s.phone, s.service_id, lead_id=s.lead_id, call_id=self.call_id
            )
        except ToolError as e:
            if e.code == "slot_unavailable":
                s.stage = "offer"
                return "Sorry, that time was just taken. " + self._offer(s, more=True)
            raise
        s.appointment_id = appt["appointment_id"]
        self.t.save_lead(
            name=None,
            phone=None,
            reason=None,
            service_id=None,
            status="booked",
            qualified=True,
            call_id=self.call_id,
            lead_id=s.lead_id,
        )
        self.t.db.execute(
            "UPDATE appointments SET lead_id = ? WHERE id = ?", (s.lead_id, s.appointment_id)
        )
        moved = ""
        if s.old_appointment_id:
            old = self.t.cancel_appointment(
                s.phone, appointment_id=s.old_appointment_id, call_id=self.call_id
            )
            moved = f" I've cancelled your previous time on {old['label']}."
        status = self.t.send_confirmation(s.appointment_id, call_id=self.call_id)
        texted = " You'll get a text confirmation shortly." if status == "sent_simulated" else ""
        s.stage = "anything_else"
        return (
            f"You're booked for {appt['label']}.{moved}{texted} "
            "Is there anything else I can help with?"
        )

    def _handle_existing(self, s: CallState) -> str:
        try:
            appt = self.t.find_appointment(s.phone)
        except ToolError:
            s.stage = "anything_else"
            s.goal = None
            return (
                "I couldn't find an upcoming appointment under that number. Would you like "
                "to book a new one?"
            )
        if s.goal == "cancel":
            self.t.cancel_appointment(
                s.phone, appointment_id=appt["appointment_id"], call_id=self.call_id
            )
            s.stage = "anything_else"
            return (
                f"Your appointment on {appt['label']} is cancelled. Would you like to book "
                "another time?"
            )
        s.old_appointment_id = appt["appointment_id"]
        s.service_id = appt["service_id"]
        s.name = s.name or appt["name"]
        s.goal = "book"
        s.stage = "ask_time"
        return f"I see your appointment on {appt['label']}. What day would you like to move it to?"

    def _answer(self, s: CallState, text: str, res: dict | None = None) -> str:
        res = res or self.t.answer_question(text, call_id=self.call_id)
        s.stage = "anything_else"
        if res["answer"]:
            s.questions_answered += 1
            return res["answer"] + " Would you like to book a visit, or is there anything else?"
        s.goal = s.goal or "callback"
        s.reason = s.reason or text
        return (
            "I don't have an approved answer for that, and I don't want to guess. I can "
            "have someone from the team call you back, or book you a visit. Which would "
            "you prefer?"
        )

    def _emergency(self, s: CallState, text: str) -> str:
        s.goal = "emergency"
        s.reason = text
        self._absorb(s, text)
        res = self.t.transfer_call(text, emergency=True, call_id=self.call_id)
        safety = self.t.profile.emergency_instructions
        if res["transferred"]:
            s.transferred_to = res["to"]
            s.ended = True
            s.stage = "done"
            self._save_contact(s, status="callback", qualified=True)
            return (
                f"That sounds urgent. {safety} I'm connecting you to {res['to']}, our "
                "on-call technician, right now."
            )
        s.stage = "collect_emergency"
        ask = "What's your name and the best number to reach you?"
        return (
            f"That sounds urgent. {safety} Our on-call technician isn't reachable right "
            f"this second, so I'm flagging this as urgent. {ask}"
        )

    def _transfer(self, s: CallState, text: str) -> str:
        res = self.t.transfer_call(text, call_id=self.call_id)
        if res["transferred"]:
            s.transferred_to = res["to"]
            s.ended = True
            s.stage = "done"
            self._save_contact(s, status="callback", qualified=False)
            return f"Sure, connecting you to {res['to']} now. Please hold."
        s.goal = "callback"
        s.reason = s.reason or "asked to speak with a person"
        return self._ask_or_continue(
            s, prefix="Everyone's busy right now. I'll have someone call you back. "
        )

    def _finish_callback(self, s: CallState, urgent: bool) -> str:
        self._save_contact(s, status="callback", qualified=bool(s.service_id))
        kind = "emergency_callback" if urgent else "callback"
        title = f"{'URGENT: ' if urgent else ''}Call back {s.name} at {format_phone(s.phone)}"
        if s.reason:
            title += f": {s.reason[:80]}"
        s.task_id = self.t.create_task(kind, title, s.lead_id, call_id=self.call_id)
        s.stage = "anything_else"
        when = "within minutes" if urgent else "as soon as possible"
        return (
            f"Got it. I've asked the team to call you back at {format_phone(s.phone)} "
            f"{when}. Is there anything else?"
        )

    def _integration_failure(self, s: CallState, err: Exception) -> str:
        s.integration_error = True
        self.t.audit.record(
            "call.integration_error",
            run_id=self.call_id,
            level="error",
            error=f"{type(err).__name__}: {err}",
            stage=s.stage,
        )
        s.goal = "callback"
        if s.name and s.phone:
            return (
                "I'm having trouble reaching our booking system, so I won't promise a time. "
                + self._finish_callback(s, urgent=False)
            )
        return self._ask_or_continue(
            s,
            prefix="I'm having trouble reaching our booking system, so I won't promise a "
            "time. Let me take your details for a callback. ",
        )

    # ------------------------------------------------------------------ helpers
    def _absorb(self, s: CallState, text: str, name: bool = True) -> None:
        """Pick up anything useful the caller volunteered."""
        if name and not s.name:
            s.name = nlu.extract_name(text)
        if not s.phone:
            p = nlu.extract_phone(text)
            if p and normalize_phone(p):
                s.phone = p
        d = nlu.extract_day(text, self._today())
        if d:
            s.day = d.isoformat()
        p = nlu.extract_part(text)
        if p:
            s.part = p
        if nlu.wants_earliest(text):
            s.day, s.part = None, None
            s.stage = s.stage if s.stage != "ask_time" else "ask_time"

    def _save_contact(self, s: CallState, status: str, qualified: bool) -> None:
        if s.name or s.phone:
            s.lead_id = self.t.save_lead(
                name=s.name,
                phone=s.phone,
                reason=s.reason,
                service_id=s.service_id,
                status=status,
                qualified=qualified,
                call_id=self.call_id,
                lead_id=s.lead_id,
            )

    def _service_name(self, s: CallState) -> str:
        svc = self.t.store.service(self.t.tenant_id, s.service_id)
        return (svc["name"] if svc else "a visit").lower()

    def _today(self):
        return self.t.clock().astimezone(self.tz).date()


def _spoken_options(slots: list[dict]) -> str:
    """'Monday, November 23 at 8:00 AM, 9:00 AM or 10:00 AM' (labels come from the tool)."""
    days: dict[str, list[str]] = {}
    for o in slots:
        day, _, time = o["label"].partition(" at ")
        days.setdefault(day, []).append(time)
    parts = []
    for day, times in days.items():
        joined = times[0] if len(times) == 1 else ", ".join(times[:-1]) + " or " + times[-1]
        parts.append(f"{day} at {joined}")
    return "; or ".join(parts)
