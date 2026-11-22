"""Synthetic demo data: two fictional businesses, users, and three weeks of call history.

Everything here is invented. Phone numbers use the 555-01xx fictional range.
"""

from __future__ import annotations

import hashlib
import json
import random
from datetime import UTC, datetime, timedelta

from ..core.db import Database
from ..core.models import utcnow
from .store import Store

NORTHSIDE = "t_northside"
MAPLE = "t_maple"

HOURS_TRADES = {
    "mon": ["08:00", "17:00"],
    "tue": ["08:00", "17:00"],
    "wed": ["08:00", "17:00"],
    "thu": ["08:00", "17:00"],
    "fri": ["08:00", "17:00"],
    "sat": ["09:00", "13:00"],
}
HOURS_DENTAL = {
    "mon": ["09:00", "18:00"],
    "tue": ["09:00", "18:00"],
    "wed": ["09:00", "18:00"],
    "thu": ["09:00", "18:00"],
    "fri": ["09:00", "15:00"],
}


def api_key_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _tenant(db, tid, slug, name, package, api_key):
    db.execute(
        "INSERT INTO tenants (id, slug, name, package, api_key_hash, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (tid, slug, name, package, api_key_hash(api_key), utcnow()),
    )


def _profile(db, tid, tz, hours, disclosure, keywords, instructions, job_value, close_rate):
    db.execute(
        "INSERT INTO business_profiles (tenant_id, timezone, hours_json, disclosure, "
        "recording_enabled, emergency_keywords_json, emergency_instructions, avg_job_value, "
        "est_close_rate, minutes_saved_per_call) VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, 4)",
        (
            tid,
            tz,
            json.dumps(hours),
            disclosure,
            json.dumps(keywords),
            instructions,
            job_value,
            close_rate,
        ),
    )


def _services(db, tid, items):
    for name, kw, mins, bookable in items:
        db.execute(
            "INSERT INTO services (tenant_id, name, keywords_json, duration_minutes, "
            "bookable) VALUES (?, ?, ?, ?, ?)",
            (tid, name, json.dumps(kw), mins, int(bookable)),
        )


def _kb(db, tid, items):
    for q, kw, a, approved in items:
        db.execute(
            "INSERT INTO knowledge_entries (tenant_id, question, keywords_json, answer, "
            "approved) VALUES (?, ?, ?, ?, ?)",
            (tid, q, json.dumps(kw), a, int(approved)),
        )


def seed(
    db: Database,
    *,
    password_hasher,
    admin_password: str,
    owner_password: str,
    northside_api_key: str = "demo-northside-key",
    history: bool = True,
    now: datetime | None = None,
) -> None:
    store = Store(db)
    if store.tenant(NORTHSIDE):
        return
    _tenant(db, NORTHSIDE, "northside", "Northside Plumbing & Heating", "growth", northside_api_key)
    _profile(
        db,
        NORTHSIDE,
        "America/Toronto",
        HOURS_TRADES,
        "This call may be handled by an AI assistant.",
        [
            "burst",
            "flood",
            "flooding",
            "gas smell",
            "smell gas",
            "smell of gas",
            "carbon monoxide",
            "sewage backup",
            "water everywhere",
        ],
        "If you smell gas, leave the building now and call 911 from outside.",
        650,
        0.6,
    )
    _services(
        db,
        NORTHSIDE,
        [
            (
                "Plumbing repair",
                [
                    "leak",
                    "leaking",
                    "drip",
                    "dripping",
                    "tap",
                    "faucet",
                    "toilet",
                    "drain",
                    "clog",
                    "clogged",
                    "pipe",
                    "plumbing",
                    "sink",
                ],
                60,
                True,
            ),
            (
                "Heating repair",
                ["furnace", "boiler", "heat", "heating", "radiator", "thermostat", "cold"],
                60,
                True,
            ),
            ("Water heater service", ["water heater", "hot water", "tank", "tankless"], 60, True),
            (
                "Free estimate",
                [
                    "estimate",
                    "quote",
                    "renovation",
                    "install",
                    "installation",
                    "replace",
                    "upgrade",
                ],
                60,
                True,
            ),
        ],
    )
    _kb(
        db,
        NORTHSIDE,
        [
            (
                "What are your hours?",
                ["hours", "open", "close", "closing", "saturday", "weekend"],
                "We're open Monday to Friday 8 AM to 5 PM, and Saturday 9 AM to 1 PM.",
                True,
            ),
            (
                "What areas do you serve?",
                [
                    "area",
                    "areas",
                    "serve",
                    "come",
                    "located",
                    "location",
                    "travel",
                    "etobicoke",
                    "scarborough",
                ],
                "We serve Toronto, North York, Etobicoke and Scarborough.",
                True,
            ),
            (
                "How much is a service call?",
                ["price", "cost", "charge", "fee", "diagnostic", "call", "rate", "much"],
                "Our diagnostic visit is $129, and it's waived if you go ahead with the repair. "
                "Repair prices depend on the job, so the technician quotes on site before any "
                "work starts.",
                True,
            ),
            (
                "Are you licensed and insured?",
                ["licensed", "license", "insured", "insurance", "certified"],
                "Yes, all our technicians are licensed and we're fully insured.",
                True,
            ),
            (
                "Do you offer financing?",
                ["financing", "finance", "payment", "plan", "monthly"],
                "DRAFT: financing through a partner (not yet approved by the owner).",
                False,
            ),
        ],
    )
    db.execute(
        "INSERT INTO staff_members (tenant_id, name, phone, handles_emergencies, "
        "available) VALUES (?, 'Mike Alvarez', '4165550101', 1, 1)",
        (NORTHSIDE,),
    )
    db.execute(
        "INSERT INTO staff_members (tenant_id, name, phone, handles_emergencies, "
        "available) VALUES (?, 'Office (Dana)', '4165550102', 0, 0)",
        (NORTHSIDE,),
    )

    _tenant(db, MAPLE, "maple-dental", "Maple Grove Dental", "starter", "demo-maple-key")
    _profile(
        db,
        MAPLE,
        "America/Vancouver",
        HOURS_DENTAL,
        "This call may be handled by an AI assistant.",
        ["bleeding", "swelling", "knocked out"],
        "For severe bleeding or facial swelling with fever, go to the nearest emergency "
        "department.",
        300,
        0.7,
    )
    _services(
        db,
        MAPLE,
        [
            ("Cleaning and check-up", ["cleaning", "checkup", "check-up", "hygiene"], 60, True),
            ("New patient exam", ["new patient", "exam", "first visit"], 60, True),
        ],
    )
    _kb(
        db,
        MAPLE,
        [
            (
                "Do you accept new patients?",
                ["new", "patients", "accepting"],
                "Yes, we're accepting new patients.",
                True,
            )
        ],
    )

    users = [
        ("admin@flow.local", "Flow Admin", admin_password, "platform_admin", None),
        ("owner@northside.example", "Sam Northside", owner_password, "owner", NORTHSIDE),
        ("owner@maple.example", "Lee Maple", owner_password, "owner", MAPLE),
    ]
    for email, name, pw, role, tid in users:
        db.execute(
            "INSERT INTO users (email, name, password_hash, role, tenant_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (email, name, password_hasher(pw), role, tid, utcnow()),
        )
    if history:
        _history(db, store, now or datetime.now(UTC))


_FIRST = [
    "Olivia",
    "Liam",
    "Emma",
    "Noah",
    "Ava",
    "Ethan",
    "Sophia",
    "Lucas",
    "Mia",
    "Arjun",
    "Chloe",
    "Mateo",
    "Zara",
    "Owen",
    "Grace",
    "Wei",
    "Fatima",
    "Jack",
]
_LAST = [
    "Tremblay",
    "Singh",
    "Nguyen",
    "MacDonald",
    "Roy",
    "Patel",
    "Wilson",
    "Chen",
    "Ali",
    "Martin",
    "Brown",
    "Gagnon",
]
_REASONS = [
    ("Plumbing repair", "Kitchen sink is leaking"),
    ("Heating repair", "Furnace not heating"),
    ("Water heater service", "No hot water since yesterday"),
    ("Free estimate", "Quote to replace a water heater"),
    ("Plumbing repair", "Clogged basement drain"),
]


def _history(db: Database, store: Store, now: datetime) -> None:
    """Three weeks of past synthetic calls so the dashboard has something to show."""
    rnd = random.Random(42)  # noqa: S311 - synthetic data only
    services = {s["name"]: s["id"] for s in store.services(NORTHSIDE)}
    outcomes = (
        ["booked"] * 9
        + ["answered"] * 5
        + ["callback"] * 3
        + ["transferred"] * 2
        + ["abandoned"] * 3
        + ["disabled"] * 2
    )
    for i, outcome in enumerate(outcomes):
        start = (now - timedelta(days=rnd.randint(1, 21), hours=rnd.randint(0, 8))).replace(
            microsecond=0
        )
        secs = rnd.randint(70, 330)
        name = f"{rnd.choice(_FIRST)} {rnd.choice(_LAST)}"
        phone = f"416555{rnd.randint(110, 199):04d}"
        svc_name, reason = rnd.choice(_REASONS)
        call_id = f"call_hist{i:03d}"
        lead_id = appt_id = None
        if outcome in ("booked", "callback", "transferred"):
            won = outcome == "booked" and i % 3 == 0
            status = "won" if won else ("booked" if outcome == "booked" else "callback")
            cur = db.execute(
                "INSERT INTO leads (tenant_id, name, phone, reason, service_id, status, qualified,"
                " estimated_value, confirmed_value, source, source_call_id, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, 1, 650, ?, 'ai_secretary', ?, ?, ?)",
                (
                    NORTHSIDE,
                    name,
                    phone,
                    reason,
                    services[svc_name],
                    status,
                    rnd.choice([480, 720, 1150, 2400]) if won else None,
                    call_id,
                    start.isoformat(),
                    start.isoformat(),
                ),
            )
            lead_id = cur.lastrowid
        if outcome == "booked":
            slot = (start + timedelta(days=2)).replace(minute=0, second=0, hour=15)
            cur = db.execute(
                "INSERT INTO appointments (tenant_id, lead_id, service_id, customer_name, "
                "customer_phone, starts_at, ends_at, status, source_call_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    NORTHSIDE,
                    lead_id,
                    services[svc_name],
                    name,
                    phone,
                    slot.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    (slot + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "completed" if slot < now else "booked",
                    call_id,
                    start.isoformat(),
                ),
            )
            appt_id = cur.lastrowid
            db.execute(
                "INSERT INTO messages (tenant_id, channel, to_addr, body, purpose, status,"
                " related_appointment_id, created_at) VALUES (?, 'sms', ?, ?, "
                "'transactional', 'sent_simulated', ?, ?)",
                (NORTHSIDE, phone, "Booking confirmation (synthetic)", appt_id, start.isoformat()),
            )
        mins = 0 if outcome == "disabled" else -(-secs // 60)
        db.execute(
            "INSERT INTO calls (id, tenant_id, channel, caller_number, started_at, ended_at, "
            "duration_seconds, billed_minutes, after_hours, outcome, summary, lead_id, "
            "appointment_id, state_json) VALUES (?, ?, 'synthetic_history', ?, ?, ?, ?, ?, ?, ?,"
            " ?, ?, ?, '{}')",
            (
                call_id,
                NORTHSIDE,
                phone,
                start.isoformat(),
                (start + timedelta(seconds=secs)).isoformat(),
                secs,
                mins,
                int(i % 4 == 0),
                outcome,
                f"{name} called about: {reason}. Outcome: {outcome}. (synthetic)",
                lead_id,
                appt_id,
            ),
        )
        if mins:
            store.add_usage(NORTHSIDE, "voice_minutes", mins, ref=call_id, when=start)
