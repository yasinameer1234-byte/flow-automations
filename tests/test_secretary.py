"""AI Secretary: dialogue, tools, guardrails, failure handling. Fixed clock, synthetic data."""

from datetime import UTC, datetime, timedelta

import pytest

from flow.core.db import Database
from flow.secretary import nlu
from flow.secretary.availability import available_slots, parse_slot_id
from flow.secretary.calls import CallError, CallService
from flow.secretary.metrics import tenant_metrics
from flow.secretary.seed import MAPLE, NORTHSIDE, seed
from flow.secretary.store import Store
from flow.secretary.tools import SecretaryTools, ToolError

# Monday 2026-11-23 10:00 in Toronto (15:00 UTC): business is open.
OPEN_NOW = datetime(2026, 11, 23, 15, 0, tzinfo=UTC)
# Sunday 2026-11-22 12:00 Toronto: closed.
CLOSED_NOW = datetime(2026, 11, 22, 17, 0, tzinfo=UTC)


def fast_hash(pw):  # scrypt is slow; tests of auth use the real one
    return "plain$" + pw


@pytest.fixture
def env():
    db = Database(":memory:")
    seed(db, password_hasher=fast_hash, admin_password="a", owner_password="o", history=False)
    store = Store(db)
    clock = {"now": OPEN_NOW}
    calls = CallService(db, store, clock=lambda: clock["now"])
    return db, store, calls, clock


def talk(calls, lines, tenant=NORTHSIDE):
    r = calls.start(tenant)
    replies = [r["reply"]]
    for line in lines:
        r = calls.turn(tenant, r["call_id"], line)
        replies.append(r["reply"])
        if r["ended"]:
            break
    return r["call_id"], replies


BOOKING = [
    "My kitchen sink is leaking",
    "Sarah Chen",
    "416 555 0142",
    "tomorrow morning",
    "the second one",
    "yes",
    "no that's all",
]


def test_happy_path_books_and_records_everything(env):
    db, store, calls, _ = env
    call_id, replies = talk(calls, BOOKING)
    assert "AI assistant" in replies[0]  # disclosure first
    assert "plumbing repair" in replies[1].lower()
    assert "Tuesday, November 24 at 8:00 AM, 9:00 AM or 10:00 AM" in replies[4]
    assert "9:00 AM" in replies[5] and "Shall I book" in replies[5]
    assert replies[6].startswith("You're booked for Tuesday, November 24 at 9:00 AM")
    call = calls.get(NORTHSIDE, call_id)
    assert call["outcome"] == "booked" and call["billed_minutes"] >= 1
    appt = db.one("SELECT * FROM appointments WHERE id = ?", (call["appointment_id"],))
    assert appt["starts_at"] == "2026-11-24T14:00:00Z" and appt["customer_phone"] == "4165550142"
    lead = db.one("SELECT * FROM leads WHERE id = ?", (call["lead_id"],))
    assert lead["status"] == "booked" and lead["name"] == "Sarah Chen"
    assert lead["confirmed_value"] is None  # a lead is not revenue
    msg = db.one("SELECT * FROM messages WHERE related_appointment_id = ?", (appt["id"],))
    assert msg["status"] == "sent_simulated" and "STOP" in msg["body"]
    assert store.usage(NORTHSIDE, "2026-11")["voice_minutes"] >= 1
    assert len(calls.transcript(NORTHSIDE, call_id)) == 2 * len(BOOKING) + 1


def test_offered_times_always_come_from_availability(env):
    db, store, calls, _ = env
    tools = SecretaryTools(db, store, NORTHSIDE, clock=lambda: OPEN_NOW)
    first = tools.check_availability(limit=3)["slots"]
    tools.book_appointment(first[0]["slot_id"], "A B", "4165550100", tools.default_service()["id"])
    offered = tools.check_availability(limit=50)["slots"]
    assert first[0]["slot_id"] not in [o["slot_id"] for o in offered]  # booked slot gone
    profile = store.profile(NORTHSIDE)
    for o in offered:
        start = parse_slot_id(o["slot_id"])
        assert start >= OPEN_NOW + timedelta(minutes=profile.booking_lead_minutes)
        local = start.astimezone(__import__("zoneinfo").ZoneInfo("America/Toronto"))
        assert local.weekday() != 6  # closed Sundays
        assert 8 <= local.hour < 17


@pytest.mark.parametrize(
    "slot_id,code",
    [
        ("2026-11-22T15:00:00Z", "slot_unavailable"),  # Sunday: closed
        ("2026-11-24T03:00:00Z", "slot_unavailable"),  # 10 PM local
        ("2026-11-23T15:00:00Z", "slot_unavailable"),  # inside the 60-minute lead time
        ("not-a-slot", "bad_slot"),
    ],
)
def test_booking_tool_refuses_invented_times(env, slot_id, code):
    db, store, _, _ = env
    tools = SecretaryTools(db, store, NORTHSIDE, clock=lambda: OPEN_NOW)
    with pytest.raises(ToolError) as e:
        tools.book_appointment(slot_id, "A B", "4165550100", tools.default_service()["id"])
    assert e.value.code == code
    assert db.one("SELECT COUNT(*) AS n FROM appointments")["n"] == 0


def test_double_booking_is_impossible(env):
    db, store, _, _ = env
    tools = SecretaryTools(db, store, NORTHSIDE, clock=lambda: OPEN_NOW)
    slot = tools.check_availability()["slots"][0]["slot_id"]
    sid = tools.default_service()["id"]
    tools.book_appointment(slot, "First", "4165550100", sid)
    with pytest.raises(ToolError) as e:
        tools.book_appointment(slot, "Second", "4165550101", sid)
    assert e.value.code == "slot_unavailable"


def test_taken_slot_during_call_is_reoffered(env):
    db, store, calls, _ = env
    r = calls.start(NORTHSIDE)
    cid = r["call_id"]
    for line in BOOKING[:5]:
        calls.turn(NORTHSIDE, cid, line)
    tools = SecretaryTools(db, store, NORTHSIDE, clock=lambda: OPEN_NOW)
    tools.book_appointment(
        "2026-11-24T14:00:00Z", "Someone Else", "4165550111", tools.default_service()["id"]
    )
    reply = calls.turn(NORTHSIDE, cid, "yes")["reply"]
    assert reply.startswith("Sorry, that time was just taken.")
    assert "9:00 AM" not in reply.split("taken.")[1]


def test_after_hours_greeting(env):
    _, _, calls, clock = env
    clock["now"] = CLOSED_NOW
    assert "We're closed right now" in calls.start(NORTHSIDE)["reply"]


def test_only_approved_answers(env):
    _, _, calls, _ = env
    _, replies = talk(calls, ["How much is a service call?"])
    assert "$129" in replies[1]
    _, replies = talk(calls, ["Do you offer financing?"])
    assert "don't have an approved answer" in replies[1]
    assert "DRAFT" not in replies[1]


def test_emergency_flags_urgent_callback_and_never_fakes_a_transfer(env):
    db, _, calls, _ = env
    cid, replies = talk(
        calls, ["Help, a pipe burst and there's water everywhere", "Ana Ruiz", "4165550150", "no"]
    )
    assert "connecting you" not in replies[1].lower()
    assert "urgent for Mike Alvarez" in replies[1]
    assert calls.get(NORTHSIDE, cid)["outcome"] == "emergency"
    task = db.one("SELECT * FROM tasks WHERE tenant_id = ?", (NORTHSIDE,))
    assert task["kind"] == "emergency_callback"


def test_live_transfer_only_when_enabled(env, monkeypatch):
    _, _, calls, _ = env
    monkeypatch.setenv("FLOW_LIVE_TRANSFER", "1")
    cid, replies = talk(calls, ["Help, a pipe burst and there's water everywhere"])
    assert "connecting you to Mike Alvarez" in replies[1]


def test_no_promise_of_a_text_while_sms_is_simulated(env):
    _, _, calls, _ = env
    _, replies = talk(
        calls, ["I need my furnace repaired", "Lee Park", "4165550170", "tomorrow", "first", "yes"]
    )
    assert any("You're booked" in r for r in replies)
    assert not any("text" in r.lower() for r in replies)


def test_emergency_with_nobody_available_creates_urgent_callback(env):
    db, _, calls, _ = env
    db.execute("UPDATE staff_members SET available = 0")
    cid, replies = talk(
        calls, ["I smell gas in the basement", "Ana Ruiz", "4165550150", "no thanks"]
    )
    assert "leave the building" in replies[1]
    assert "urgent callback" in replies[3]
    task = db.one("SELECT * FROM tasks WHERE tenant_id = ?", (NORTHSIDE,))
    assert task["kind"] == "emergency_callback" and task["title"].startswith("URGENT")


def test_human_request_without_staff_takes_message(env):
    db, _, calls, _ = env
    db.execute("UPDATE staff_members SET available = 0")
    cid, replies = talk(
        calls, ["Can I speak to a real person?", "Tom Reed", "416-555-0160", "that's all"]
    )
    assert "can't put you through live" in replies[1]
    assert calls.get(NORTHSIDE, cid)["outcome"] == "callback"


def test_calendar_outage_never_promises_a_time(env):
    db, store, _, _ = env

    class Broken(SecretaryTools):
        def check_availability(self, *a, **k):
            raise TimeoutError("calendar API timed out")

    calls = CallService(
        db,
        store,
        clock=lambda: OPEN_NOW,
        tools_factory=lambda t: Broken(db, store, t, clock=lambda: OPEN_NOW),
    )
    cid, replies = talk(calls, ["My furnace is broken", "Lee Park", "4165550170", "tomorrow", "no"])
    assert "won't promise a time" in replies[4]
    assert not any("You're booked" in r for r in replies)
    assert calls.get(NORTHSIDE, cid)["outcome"] == "callback"
    errors = db.query("SELECT * FROM audit_log WHERE level = 'error'")
    assert errors and errors[0]["event"] == "call.integration_error"


def test_kill_switch_stops_the_assistant(env):
    db, store, calls, _ = env
    store.set_automation(NORTHSIDE, "ai_secretary", False, "test")
    r = calls.start(NORTHSIDE)
    assert r["ended"] and "can't take your call" in r["reply"]
    assert calls.get(NORTHSIDE, r["call_id"])["outcome"] == "disabled"
    assert store.usage(NORTHSIDE, "2026-11") == {}
    tools = SecretaryTools(db, store, NORTHSIDE, clock=lambda: OPEN_NOW)
    with pytest.raises(ToolError):
        tools.book_appointment("2026-11-24T14:00:00Z", "A", "4165550100", 1)


def test_cancel_and_reschedule(env):
    db, store, calls, _ = env
    talk(calls, BOOKING)
    _, replies = talk(
        calls,
        [
            "I need to reschedule my appointment",
            "416 555 0142",
            "wednesday afternoon",
            "the first one",
            "yes",
            "bye",
        ],
    )
    assert "Tuesday, November 24 at 9:00 AM" in replies[2]
    assert "Wednesday, November 25" in replies[3]
    assert "cancelled your previous time" in replies[5]
    rows = db.query("SELECT status, starts_at FROM appointments ORDER BY id")
    assert [r["status"] for r in rows] == ["cancelled", "booked"]
    _, replies = talk(calls, ["Please cancel my appointment", "4165550142", "no"])
    assert "is cancelled" in replies[2]
    assert db.one("SELECT COUNT(*) AS n FROM appointments WHERE status = 'booked'")["n"] == 0


def test_tenant_isolation(env):
    db, store, calls, _ = env
    cid, _ = talk(calls, BOOKING)
    with pytest.raises(CallError):
        calls.get(MAPLE, cid)
    maple = SecretaryTools(db, store, MAPLE, clock=lambda: OPEN_NOW)
    northside_service = store.services(NORTHSIDE)[0]["id"]
    with pytest.raises(ToolError):
        maple.book_appointment("2026-11-24T18:00:00Z", "A", "4165550100", northside_service)
    assert maple.answer_question("How much is a service call?")["answer"] is None


def test_abandoned_call_keeps_the_lead(env):
    db, _, calls, clock = env
    r = calls.start(NORTHSIDE)
    calls.turn(NORTHSIDE, r["call_id"], "My water heater is leaking")
    calls.turn(NORTHSIDE, r["call_id"], "Kim Ross")
    clock["now"] = OPEN_NOW + timedelta(hours=1)
    assert calls.expire_stale(NORTHSIDE) == 1
    call = calls.get(NORTHSIDE, r["call_id"])
    assert call["outcome"] == "abandoned"
    assert db.one("SELECT name FROM leads WHERE id = ?", (call["lead_id"],))["name"] == "Kim Ross"


def test_metrics_separate_verified_from_estimated(env):
    db, store, calls, _ = env
    talk(calls, BOOKING)
    m = tenant_metrics(db, store, NORTHSIDE, now=OPEN_NOW)
    assert m["appointments_booked"] == 1 and m["verified_revenue"] == 0
    assert m["estimated_revenue"] == pytest.approx(650 * 0.6)
    db.execute(
        "UPDATE leads SET status = 'won', confirmed_value = 900, updated_at = ?",
        (OPEN_NOW.isoformat(),),
    )
    m = tenant_metrics(db, store, NORTHSIDE, now=OPEN_NOW)
    assert m["verified_revenue"] == 900 and m["verified_roi"] == pytest.approx(0.9)


def test_availability_respects_part_of_day(env):
    db, store, _, _ = env
    p = store.profile(NORTHSIDE)
    slots = available_slots(db, p, 60, OPEN_NOW, part_of_day="afternoon", limit=5)
    assert slots and all(s.local().hour >= 12 for s in slots)


# ---------------------------------------------------------------- NLU
@pytest.mark.parametrize(
    "text,expected",
    [
        ("my name is sarah chen", "Sarah Chen"),
        ("This is Priya Raman calling", "Priya Raman"),
        ("I'm looking for help", None),
        # audit regression: a description is not a name when we didn't ask for one
        ("I need someone to look at my furnace, it's making a weird noise", None),
        ("I'm calling about my sink", None),
    ],
)
def test_extract_name(text, expected):
    assert nlu.extract_name(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("it's Dave", "Dave"),
        ("Priya Shah", "Priya Shah"),
        ("Do you guys do weekends", None),
        ("ok thanks bye", None),
        ("it's making a weird noise", None),
    ],
)
def test_extract_name_when_asked(text, expected):
    assert nlu.extract_name(text, expecting=True) == expected


def test_off_script_audit_calls(env):
    """Transcripts from the launch audit: wrong name, skipped price question, no way out."""
    db, store, calls, _ = env
    r = calls.start(NORTHSIDE)
    cid = r["call_id"]
    reply = calls.turn(
        NORTHSIDE, cid, "I need someone to look at my furnace, it's making a weird noise"
    )["reply"]
    assert "Making" not in reply and "name" in reply.lower()
    calls.turn(NORTHSIDE, cid, "Priya Shah")
    state = db.one("SELECT state_json FROM calls WHERE id = ?", (cid,))["state_json"]
    assert '"name": "Priya Shah"' in state

    r = calls.start(NORTHSIDE)
    reply = calls.turn(NORTHSIDE, r["call_id"], "How much do you charge to fix a leaky tap?")
    assert "$129" in reply["reply"] or "won't guess" in reply["reply"]

    r = calls.start(NORTHSIDE)
    cid = r["call_id"]
    calls.turn(NORTHSIDE, cid, "I need a plumber")
    reply = calls.turn(NORTHSIDE, cid, "Do you guys do weekends")["reply"]
    assert "name" in reply.lower() and reply != "Sorry, could I get your first and last name?"
    out = calls.turn(NORTHSIDE, cid, "ok thanks bye")
    assert out["ended"] is True


def test_extract_phone_and_day():
    assert nlu.extract_phone("call me at 1-416-555-0142") == "4165550142"
    assert nlu.extract_phone("four one six five five five zero one four two") == "4165550142"
    assert nlu.extract_phone("12345") is None
    monday = OPEN_NOW.date()
    assert nlu.extract_day("tomorrow", monday).isoformat() == "2026-11-24"
    assert nlu.extract_day("friday works", monday).isoformat() == "2026-11-27"
    assert nlu.extract_day("next monday", monday).isoformat() == "2026-11-30"


def test_yes_no():
    assert nlu.is_yes("yes please") and not nlu.is_yes("no not really")
    assert nlu.is_no("no") and not nlu.is_no("yes")
