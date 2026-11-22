"""Revenue Engine: turn missed calls, cold leads and past customers into booked jobs.

One ``tick(tenant_id)`` does three things, in order, and is safe to run every minute:
1. enrol people who match a playbook trigger (one active sequence per phone at a time);
2. stop sequences that have done their job (booked, won, lost, replied or opted out);
3. send the steps that are due, if every rule allows it.

Rules checked before every single message (see ``_blocked``):
- the business is on a package that includes the Revenue Engine, and its kill switch is on;
- the owner approved this playbook's current wording;
- the number has not opted out (STOP, UNSUBSCRIBE, ARRET...);
- implied consent is still valid (inquiry: 6 months, past customer: 2 years);
- local quiet hours (default 9 AM to 8 PM, Monday to Saturday) - otherwise it waits;
- at most one follow-up per number per 20 hours across all playbooks.

All sending is simulated: messages are written to the outbox with status ``sent_simulated``.
"""

from __future__ import annotations

import re
import threading
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from ..core.audit import AuditLog
from ..core.db import Database
from ..secretary.store import Store
from ..secretary.tools import format_phone, normalize_phone
from .playbooks import PLAYBOOKS, Playbook, render

REVENUE_SCHEMA = """
CREATE TABLE IF NOT EXISTS revenue_settings (
    tenant_id TEXT PRIMARY KEY,
    business_phone TEXT NOT NULL,
    review_url TEXT,
    send_start TEXT NOT NULL DEFAULT '09:00',
    send_end TEXT NOT NULL DEFAULT '20:00',
    send_sundays INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS playbook_approvals (
    tenant_id TEXT NOT NULL,
    playbook TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    approved_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, playbook)
);
CREATE TABLE IF NOT EXISTS suppressions (
    tenant_id TEXT NOT NULL,
    phone TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, phone)
);
CREATE TABLE IF NOT EXISTS enrollments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    playbook TEXT NOT NULL,
    subject TEXT NOT NULL,             -- what triggered it: call:<id> or lead:<id>
    phone TEXT NOT NULL,
    name TEXT,
    reason TEXT,
    lead_id INTEGER,
    consent_basis TEXT NOT NULL,
    consent_expires_at TEXT NOT NULL,
    step INTEGER NOT NULL DEFAULT 0,
    next_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',   -- active | converted | stopped | finished
    stop_reason TEXT,
    first_touch_at TEXT,
    converted_appointment_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (tenant_id, playbook, subject)
);
CREATE INDEX IF NOT EXISTS ix_enroll_due ON enrollments (tenant_id, status, next_at);
CREATE TABLE IF NOT EXISTS revenue_touches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    enrollment_id INTEGER NOT NULL REFERENCES enrollments(id),
    message_id INTEGER NOT NULL,
    step INTEGER NOT NULL,
    sent_at TEXT NOT NULL
);
"""

REVENUE_PACKAGES = {"growth", "scale"}
CONSENT_DAYS = {"inquiry": 183, "customer": 730}
FREQUENCY_HOURS = 20
_STOP_WORDS = {"stop", "unsubscribe", "cancel", "end", "quit", "stopall", "arret", "arrêt"}
_YES = re.compile(r"^\s*(yes|yep|yeah|sure|ok|okay|y|oui|book|please)\b", re.I)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def _dt(s: str) -> datetime:
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=UTC)


class RevenueEngine:
    def __init__(self, db: Database, store: Store, clock=None) -> None:
        self.db = db
        self.store = store
        self.clock = clock or (lambda: datetime.now(UTC))
        self._tick_lock = threading.Lock()  # one tick at a time: no double sends
        db._conn.executescript(REVENUE_SCHEMA)

    # ------------------------------------------------------------------ settings
    def settings(self, tenant_id: str) -> dict | None:
        return self.db.one("SELECT * FROM revenue_settings WHERE tenant_id = ?", (tenant_id,))

    def configure(self, tenant_id: str, business_phone: str, review_url: str | None) -> None:
        self.db.execute(
            "INSERT INTO revenue_settings (tenant_id, business_phone, review_url) VALUES (?,?,?) "
            "ON CONFLICT (tenant_id) DO UPDATE SET business_phone = excluded.business_phone, "
            "review_url = excluded.review_url",
            (tenant_id, business_phone, review_url),
        )

    def included(self, tenant_id: str) -> bool:
        t = self.store.tenant(tenant_id)
        return bool(t) and t["package"] in REVENUE_PACKAGES

    # ------------------------------------------------------------------ approvals
    def approve(self, tenant_id: str, key: str, by: str) -> None:
        pb = PLAYBOOKS[key]
        self.db.execute(
            "INSERT INTO playbook_approvals (tenant_id, playbook, fingerprint, approved_by, "
            "approved_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT (tenant_id, playbook) DO UPDATE SET "
            "fingerprint = excluded.fingerprint, approved_by = excluded.approved_by, "
            "approved_at = excluded.approved_at",
            (tenant_id, key, pb.fingerprint, by, _iso(self.clock())),
        )
        AuditLog(self.db, tenant_id).record(
            "revenue.playbook_approved", actor=by, playbook=key, fingerprint=pb.fingerprint
        )

    def revoke(self, tenant_id: str, key: str, by: str) -> None:
        self.db.execute(
            "DELETE FROM playbook_approvals WHERE tenant_id = ? AND playbook = ?", (tenant_id, key)
        )
        self.db.execute(
            "UPDATE enrollments SET status = 'stopped', stop_reason = 'playbook_paused', "
            "updated_at = ? WHERE tenant_id = ? AND playbook = ? AND status = 'active'",
            (_iso(self.clock()), tenant_id, key),
        )
        AuditLog(self.db, tenant_id).record("revenue.playbook_paused", actor=by, playbook=key)

    def approved(self, tenant_id: str, key: str) -> bool:
        r = self.db.one(
            "SELECT fingerprint FROM playbook_approvals WHERE tenant_id = ? AND playbook = ?",
            (tenant_id, key),
        )
        return bool(r) and r["fingerprint"] == PLAYBOOKS[key].fingerprint

    def playbook_status(self, tenant_id: str) -> list[dict]:
        out = []
        for pb in PLAYBOOKS.values():
            r = self.db.one(
                "SELECT * FROM playbook_approvals WHERE tenant_id = ? AND playbook = ?",
                (tenant_id, pb.key),
            )
            counts = {
                row["status"]: row["n"]
                for row in self.db.query(
                    "SELECT status, COUNT(*) n FROM enrollments WHERE tenant_id = ? AND "
                    "playbook = ? GROUP BY status",
                    (tenant_id, pb.key),
                )
            }
            out.append(
                {
                    "playbook": pb,
                    "approved": bool(r) and r["fingerprint"] == pb.fingerprint,
                    "stale": bool(r) and r["fingerprint"] != pb.fingerprint,
                    "approved_by": r["approved_by"] if r else None,
                    "approved_at": r["approved_at"] if r else None,
                    "preview": [self._preview(tenant_id, s.template) for s in pb.steps],
                    "counts": counts,
                }
            )
        return out

    def _preview(self, tenant_id: str, template: str) -> str:
        s = self.settings(tenant_id) or {}
        return render(
            template,
            business=self.store.tenant(tenant_id)["name"],
            business_phone=format_phone(s.get("business_phone") or "") or "(our number)",
            review_url=s.get("review_url") or "(your review link)",
            name="Alex Sample",
            reason="a leaking water heater",
        )

    # ------------------------------------------------------------------ opt-outs / replies
    def suppressed(self, tenant_id: str, phone: str) -> bool:
        return bool(
            self.db.one(
                "SELECT 1 FROM suppressions WHERE tenant_id = ? AND phone = ?", (tenant_id, phone)
            )
        )

    def inbound_sms(self, tenant_id: str, from_number: str, text: str) -> dict:
        """Handle a reply. STOP opts out everywhere; any other reply hands over to a person."""
        phone = normalize_phone(from_number)
        if not phone:
            return {"action": "ignored"}
        now = _iso(self.clock())
        word = re.sub(r"[^a-zàâçéèêëîïôûùüÿñæœ]", "", (text or "").strip().lower())
        audit = AuditLog(self.db, tenant_id)
        if word in _STOP_WORDS:
            self.db.execute(
                "INSERT OR IGNORE INTO suppressions (tenant_id, phone, reason, created_at) "
                "VALUES (?, ?, 'stop_reply', ?)",
                (tenant_id, phone, now),
            )
            self._stop_phone(tenant_id, phone, "opted_out")
            name = self.store.tenant(tenant_id)["name"]
            self._outbox(
                tenant_id,
                phone,
                f"{name}: you're unsubscribed and won't get more messages from us.",
                "transactional",
            )
            audit.record("revenue.opt_out", phone_last4=phone[-4:])
            return {"action": "opted_out"}
        active = self.db.one(
            "SELECT * FROM enrollments WHERE tenant_id = ? AND phone = ? AND status = 'active' "
            "ORDER BY id DESC",
            (tenant_id, phone),
        )
        self._stop_phone(tenant_id, phone, "replied")
        wants_booking = bool(_YES.match(text or ""))
        who = (active or {}).get("name") or format_phone(phone)
        title = (
            f"Book {who}: replied YES to a follow-up"
            if wants_booking
            else f"Reply from {who}: {text.strip()[:80]}"
        )
        self.db.execute(
            "INSERT INTO tasks (tenant_id, kind, title, lead_id, created_at) "
            "VALUES (?, 'callback', ?, ?, ?)",
            (tenant_id, title, (active or {}).get("lead_id"), now),
        )
        audit.record("revenue.reply", wants_booking=wants_booking, phone_last4=phone[-4:])
        return {"action": "handed_to_staff", "wants_booking": wants_booking}

    def _stop_phone(self, tenant_id: str, phone: str, reason: str) -> None:
        self.db.execute(
            "UPDATE enrollments SET status = 'stopped', stop_reason = ?, updated_at = ? "
            "WHERE tenant_id = ? AND phone = ? AND status = 'active'",
            (reason, _iso(self.clock()), tenant_id, phone),
        )

    # ------------------------------------------------------------------ the tick
    def tick(self, tenant_id: str) -> dict:
        if not self.included(tenant_id) or not self.settings(tenant_id):
            return {"enrolled": 0, "stopped": 0, "sent": 0, "waiting": 0}
        with self._tick_lock:
            enrolled = self._enroll(tenant_id)
            stopped = self._check_stops(tenant_id)
            sent, waiting = self._send_due(tenant_id)
        return {"enrolled": enrolled, "stopped": stopped, "sent": sent, "waiting": waiting}

    def tick_all(self) -> dict:
        return {
            t["id"]: self.tick(t["id"]) for t in self.store.tenants() if t["status"] == "active"
        }

    # enrolment ----------------------------------------------------------------
    def _candidates(self, tenant_id: str, now: datetime) -> list[tuple]:
        """(playbook, subject, phone, name, reason, lead_id, consent_from) per trigger."""
        out = []
        for c in self.db.query(
            "SELECT c.id, c.caller_number, c.ended_at, c.lead_id, l.name, l.reason "
            "FROM calls c LEFT JOIN leads l ON l.id = c.lead_id "
            "WHERE c.tenant_id = ? AND c.outcome IN ('abandoned', 'disabled') "
            "AND c.caller_number IS NOT NULL AND c.ended_at >= ?",
            (tenant_id, _iso(now - timedelta(hours=48))),
        ):
            out.append(
                (
                    "missed_call",
                    f"call:{c['id']}",
                    c["caller_number"],
                    c["name"],
                    c["reason"],
                    c["lead_id"],
                    c["ended_at"],
                )
            )
        for lead in self.db.query(
            "SELECT * FROM leads WHERE tenant_id = ? AND status IN ('new', 'callback') "
            "AND phone IS NOT NULL AND created_at >= ? AND created_at <= ?",
            (tenant_id, _iso(now - timedelta(days=14)), _iso(now - timedelta(minutes=60))),
        ):
            out.append(
                (
                    "lead_follow_up",
                    f"lead:{lead['id']}",
                    lead["phone"],
                    lead["name"],
                    lead["reason"],
                    lead["id"],
                    lead["created_at"],
                )
            )
        for lead in self.db.query(
            "SELECT * FROM leads WHERE tenant_id = ? AND status = 'won' AND phone IS NOT NULL "
            "AND updated_at >= ?",
            (tenant_id, _iso(now - timedelta(days=14))),
        ):
            out.append(
                (
                    "review_request",
                    f"lead:{lead['id']}",
                    lead["phone"],
                    lead["name"],
                    lead["reason"],
                    lead["id"],
                    lead["updated_at"],
                )
            )
        for lead in self.db.query(
            "SELECT l.* FROM leads l WHERE l.tenant_id = ? AND l.status = 'won' "
            "AND l.phone IS NOT NULL AND l.updated_at <= ? AND l.updated_at >= ? "
            "AND NOT EXISTS (SELECT 1 FROM leads n WHERE n.tenant_id = l.tenant_id "
            "AND n.phone = l.phone AND n.updated_at > ?)",
            (
                tenant_id,
                _iso(now - timedelta(days=335)),
                _iso(now - timedelta(days=700)),
                _iso(now - timedelta(days=335)),
            ),
        ):
            year = now.strftime("%Y")
            out.append(
                (
                    "reactivation",
                    f"lead:{lead['id']}:{year}",
                    lead["phone"],
                    lead["name"],
                    lead["reason"],
                    lead["id"],
                    lead["updated_at"],
                )
            )
        return out

    def _enroll(self, tenant_id: str) -> int:
        now = self.clock()
        n = 0
        for key, subject, phone, name, reason, lead_id, since in self._candidates(tenant_id, now):
            pb = PLAYBOOKS[key]
            if not self.approved(tenant_id, key):
                continue
            if self.suppressed(tenant_id, phone):
                continue
            expires = _dt(since) + timedelta(days=CONSENT_DAYS[pb.consent_basis])
            if expires <= now:
                continue
            busy = self.db.one(
                "SELECT 1 FROM enrollments WHERE tenant_id = ? AND phone = ? AND status = 'active'",
                (tenant_id, phone),
            )
            if busy or self._booked_since(tenant_id, phone, _dt(since)):
                continue
            cur = self.db.execute(
                "INSERT OR IGNORE INTO enrollments (tenant_id, playbook, subject, phone, name, "
                "reason, lead_id, consent_basis, consent_expires_at, next_at, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    tenant_id,
                    key,
                    subject,
                    phone,
                    name,
                    reason,
                    lead_id,
                    pb.consent_basis,
                    _iso(expires),
                    _iso(now + timedelta(minutes=pb.steps[0].delay_minutes)),
                    _iso(now),
                    _iso(now),
                ),
            )
            n += cur.rowcount
        return n

    def _booked_since(self, tenant_id: str, phone: str, since: datetime) -> dict | None:
        return self.db.one(
            "SELECT id, lead_id, created_at FROM appointments WHERE tenant_id = ? "
            "AND customer_phone = ? "
            "AND status IN ('booked', 'completed') AND created_at > ? ORDER BY id",
            (tenant_id, phone, _iso(since)),
        )

    # stopping -----------------------------------------------------------------
    def _check_stops(self, tenant_id: str) -> int:
        now = _iso(self.clock())
        n = 0
        for e in self.db.query(
            "SELECT * FROM enrollments WHERE tenant_id = ? AND status = 'active'", (tenant_id,)
        ):
            if e["playbook"] in ("missed_call", "lead_follow_up"):
                appt = self._booked_since(tenant_id, e["phone"], _dt(e["created_at"]))
                if appt:
                    # Credit the follow-up only if the booking came after our first message.
                    touched = e["first_touch_at"] and _dt(appt["created_at"]) > _dt(
                        e["first_touch_at"]
                    )
                    status = "converted" if touched else "stopped"
                    self.db.execute(
                        "UPDATE enrollments SET status = ?, stop_reason = 'booked', "
                        "converted_appointment_id = ?, updated_at = ? WHERE id = ?",
                        (status, appt["id"], now, e["id"]),
                    )
                    n += 1
                    continue
                if e["lead_id"]:
                    lead = self.db.one("SELECT status FROM leads WHERE id = ?", (e["lead_id"],))
                    if lead and lead["status"] in ("booked", "won", "lost"):
                        self.db.execute(
                            "UPDATE enrollments SET status = 'stopped', stop_reason = ?, "
                            "updated_at = ? WHERE id = ?",
                            (f"lead_{lead['status']}", now, e["id"]),
                        )
                        n += 1
        return n

    # sending ------------------------------------------------------------------
    def _blocked(self, tenant_id: str, e: dict, now: datetime) -> str | None:
        """Return why this message may not go out now, or None if it may."""
        if not self.store.automation_enabled(tenant_id, "revenue_engine"):
            return "switched_off"
        if not self.approved(tenant_id, e["playbook"]):
            return "not_approved"
        if self.suppressed(tenant_id, e["phone"]):
            return "opted_out"
        if _dt(e["consent_expires_at"]) <= now:
            return "consent_expired"
        recent = self.db.one(
            "SELECT 1 FROM revenue_touches t JOIN enrollments e ON e.id = t.enrollment_id "
            "WHERE t.tenant_id = ? AND e.phone = ? AND t.sent_at > ?",
            (tenant_id, e["phone"], _iso(now - timedelta(hours=FREQUENCY_HOURS))),
        )
        if recent:
            return "frequency_cap"
        return None

    def next_send_window(self, tenant_id: str, now: datetime) -> datetime:
        """The earliest moment at or after ``now`` that is inside local sending hours."""
        s = self.settings(tenant_id)
        tz = ZoneInfo(self.store.profile(tenant_id).timezone)
        start, end = time.fromisoformat(s["send_start"]), time.fromisoformat(s["send_end"])
        local = now.astimezone(tz)
        for _ in range(8):
            sunday_ok = s["send_sundays"] or local.weekday() != 6
            if sunday_ok and start <= local.time() < end:
                return local.astimezone(UTC)
            if sunday_ok and local.time() < start:
                return local.replace(
                    hour=start.hour, minute=start.minute, second=0, microsecond=0
                ).astimezone(UTC)
            local = (local + timedelta(days=1)).replace(
                hour=start.hour, minute=start.minute, second=0, microsecond=0
            )
        return local.astimezone(UTC)

    def _send_due(self, tenant_id: str) -> tuple[int, int]:
        now = self.clock()
        sent = waiting = 0
        tenant = self.store.tenant(tenant_id)
        s = self.settings(tenant_id)
        for e in self.db.query(
            "SELECT * FROM enrollments WHERE tenant_id = ? AND status = 'active' AND next_at <= ? "
            "ORDER BY next_at",
            (tenant_id, _iso(now)),
        ):
            pb: Playbook = PLAYBOOKS[e["playbook"]]
            why = self._blocked(tenant_id, e, now)
            if why in ("not_approved", "opted_out", "consent_expired"):
                self.db.execute(
                    "UPDATE enrollments SET status = 'stopped', stop_reason = ?, updated_at = ? "
                    "WHERE id = ?",
                    (why, _iso(now), e["id"]),
                )
                continue
            if why:  # switched off or capped: try again later, never drop silently
                self.db.execute(
                    "UPDATE enrollments SET next_at = ?, updated_at = ? WHERE id = ?",
                    (_iso(now + timedelta(hours=1)), _iso(now), e["id"]),
                )
                waiting += 1
                continue
            window = self.next_send_window(tenant_id, now)
            if window > now:
                self.db.execute(
                    "UPDATE enrollments SET next_at = ?, updated_at = ? WHERE id = ?",
                    (_iso(window), _iso(now), e["id"]),
                )
                waiting += 1
                continue
            if pb.key == "review_request" and not s.get("review_url"):
                self.db.execute(
                    "UPDATE enrollments SET status = 'stopped', stop_reason = 'no_review_link', "
                    "updated_at = ? WHERE id = ?",
                    (_iso(now), e["id"]),
                )
                continue
            body = render(
                pb.steps[e["step"]].template,
                business=tenant["name"],
                business_phone=format_phone(s["business_phone"]),
                review_url=s.get("review_url"),
                name=e["name"],
                reason=e["reason"],
            )
            msg_id = self._outbox(tenant_id, e["phone"], body, "follow_up")
            self.db.execute(
                "INSERT INTO revenue_touches (tenant_id, enrollment_id, message_id, step, sent_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (tenant_id, e["id"], msg_id, e["step"], _iso(now)),
            )
            nxt = e["step"] + 1
            if nxt < len(pb.steps):
                self.db.execute(
                    "UPDATE enrollments SET step = ?, next_at = ?, first_touch_at = "
                    "COALESCE(first_touch_at, ?), updated_at = ? WHERE id = ?",
                    (
                        nxt,
                        _iso(now + timedelta(minutes=pb.steps[nxt].delay_minutes)),
                        _iso(now),
                        _iso(now),
                        e["id"],
                    ),
                )
            else:
                self.db.execute(
                    "UPDATE enrollments SET step = ?, status = 'finished', first_touch_at = "
                    "COALESCE(first_touch_at, ?), updated_at = ? WHERE id = ?",
                    (nxt, _iso(now), _iso(now), e["id"]),
                )
            sent += 1
        return sent, waiting

    def _outbox(self, tenant_id: str, phone: str, body: str, purpose: str) -> int:
        cur = self.db.execute(
            "INSERT INTO messages (tenant_id, channel, to_addr, body, purpose, status, created_at)"
            " VALUES (?, 'sms', ?, ?, ?, 'sent_simulated', ?)",
            (tenant_id, phone, body, purpose, _iso(self.clock())),
        )
        self.store.add_usage(tenant_id, "sms", 1, ref=f"msg:{cur.lastrowid}", when=self.clock())
        return int(cur.lastrowid)

    # ------------------------------------------------------------------ results
    def results(self, tenant_id: str, days: int = 30) -> dict:
        """Recovered revenue. Verified = a job marked won with a value, booked after our text."""
        since = _iso(self.clock() - timedelta(days=days))
        p = self.store.profile(tenant_id)
        conv = self.db.query(
            "SELECT e.*, a.lead_id AS appt_lead FROM enrollments e "
            "JOIN appointments a ON a.id = e.converted_appointment_id "
            "WHERE e.tenant_id = ? AND e.status = 'converted' AND e.updated_at >= ?",
            (tenant_id, since),
        )
        verified = 0.0
        won = 0
        for c in conv:
            lead = (
                self.db.one(
                    "SELECT status, confirmed_value FROM leads WHERE id = ?", (c["appt_lead"],)
                )
                if c["appt_lead"]
                else None
            )
            if lead and lead["status"] == "won" and lead["confirmed_value"]:
                verified += lead["confirmed_value"]
                won += 1
        sent = self.db.one(
            "SELECT COUNT(*) n FROM revenue_touches WHERE tenant_id = ? AND sent_at >= ?",
            (tenant_id, since),
        )["n"]
        active = self.db.one(
            "SELECT COUNT(*) n FROM enrollments WHERE tenant_id = ? AND status = 'active'",
            (tenant_id,),
        )["n"]
        opted = self.db.one(
            "SELECT COUNT(*) n FROM suppressions WHERE tenant_id = ? AND created_at >= ?",
            (tenant_id, since),
        )["n"]
        recovered_bookings = len(conv)
        return {
            "days": days,
            "messages_sent": sent,
            "active_sequences": active,
            "recovered_bookings": recovered_bookings,
            "recovered_jobs_won": won,
            "verified_recovered_revenue": verified,
            "estimated_recovered_revenue": recovered_bookings * p.avg_job_value * p.est_close_rate,
            "opt_outs": opted,
        }
