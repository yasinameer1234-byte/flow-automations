"""Revenue Engine: approvals, consent, quiet hours, opt-outs, stopping, attribution."""

from datetime import UTC, datetime, timedelta

import pytest

from flow.core.db import Database
from flow.revenue import playbooks as pbmod
from flow.revenue.engine import RevenueEngine
from flow.secretary.calls import CallService
from flow.secretary.seed import MAPLE, NORTHSIDE, seed
from flow.secretary.store import Store
from flow.secretary.tools import SecretaryTools

TEST_PW = "test-only-" + "login"  # synthetic in-memory test apps only

MON_10AM = datetime(2026, 11, 23, 15, 0, tzinfo=UTC)  # Monday 10:00 Toronto


@pytest.fixture
def env():
    db = Database(":memory:")
    clock = {"now": MON_10AM}
    seed(
        db,
        password_hasher=lambda p: "plain$" + p,
        admin_password="a",
        owner_password="o",
        now=MON_10AM,
    )
    store = Store(db)
    eng = RevenueEngine(db, store, clock=lambda: clock["now"])
    return db, store, eng, clock


def advance(clock, **kw):
    clock["now"] = clock["now"] + timedelta(**kw)


def followups(db, phone=None):
    sql = "SELECT * FROM messages WHERE purpose = 'follow_up'"
    args = ()
    if phone:
        sql += " AND to_addr = ?"
        args = (phone,)
    return db.query(sql + " ORDER BY id", args)


def test_nothing_is_sent_without_owner_approval(env):
    db, _, eng, clock = env
    for _ in range(3):
        eng.tick(NORTHSIDE)
        advance(clock, hours=6)
    assert followups(db) == []
    assert db.one("SELECT COUNT(*) n FROM enrollments")["n"] == 0


def test_missed_call_text_back_after_two_minutes(env):
    db, _, eng, clock = env
    eng.approve(NORTHSIDE, "missed_call", "user:owner")
    assert eng.tick(NORTHSIDE)["enrolled"] == 2  # the two synthetic missed calls
    assert followups(db) == []  # first text waits two minutes
    advance(clock, minutes=3)
    assert eng.tick(NORTHSIDE)["sent"] == 2
    msg = followups(db, "4165550171")[0]
    assert "Northside Plumbing & Heating" in msg["body"]
    assert "(416) 555-0100" in msg["body"] and "Reply STOP" in msg["body"]
    assert msg["status"] == "sent_simulated"
    # second step only after a day, and the sequence then finishes
    advance(clock, hours=2)
    assert eng.tick(NORTHSIDE)["sent"] == 0
    advance(clock, days=1)
    assert eng.tick(NORTHSIDE)["sent"] == 2
    assert len(followups(db, "4165550171")) == 2
    st = db.one("SELECT status FROM enrollments WHERE phone = '4165550171'")["status"]
    assert st == "finished"


def test_quiet_hours_delay_instead_of_texting_at_night(env):
    db, _, eng, clock = env
    clock["now"] = datetime(2026, 11, 24, 3, 30, tzinfo=UTC)  # Mon 22:30 Toronto
    db.execute(
        "UPDATE calls SET ended_at = ? WHERE id LIKE 'call_missed%'",
        ((clock["now"] - timedelta(hours=1)).isoformat(),),
    )
    eng.approve(NORTHSIDE, "missed_call", "user:owner")
    eng.tick(NORTHSIDE)
    advance(clock, minutes=5)
    r = eng.tick(NORTHSIDE)
    assert r["sent"] == 0 and r["waiting"] == 2
    nxt = db.one("SELECT next_at FROM enrollments LIMIT 1")["next_at"]
    assert nxt.startswith("2026-11-24T14:00")  # Tuesday 09:00 Toronto


def test_no_texts_on_sunday(env):
    _, _, eng, _ = env
    sat_night = datetime(2026, 11, 29, 2, 0, tzinfo=UTC)  # Sat 21:00 Toronto
    nxt = eng.next_send_window(NORTHSIDE, sat_night)
    assert nxt == datetime(2026, 11, 30, 14, 0, tzinfo=UTC)  # Monday 09:00


def test_stop_reply_opts_out_everywhere(env):
    db, _, eng, clock = env
    eng.approve(NORTHSIDE, "missed_call", "user:owner")
    eng.tick(NORTHSIDE)
    advance(clock, minutes=3)
    eng.tick(NORTHSIDE)
    assert eng.inbound_sms(NORTHSIDE, "+1 416 555 0171", "STOP")["action"] == "opted_out"
    assert eng.suppressed(NORTHSIDE, "4165550171")
    e = db.one("SELECT * FROM enrollments WHERE phone = '4165550171'")
    assert e["status"] == "stopped" and e["stop_reason"] == "opted_out"
    confirm = db.one(
        "SELECT body FROM messages WHERE to_addr = '4165550171' AND purpose = 'transactional'"
    )
    assert "unsubscribed" in confirm["body"]
    # French opt-out works too
    assert eng.inbound_sms(NORTHSIDE, "6475550133", "Arrêt")["action"] == "opted_out"
    advance(clock, days=2)
    eng.approve(NORTHSIDE, "lead_follow_up", "user:owner")
    eng.tick(NORTHSIDE)
    later = [m for m in followups(db, "4165550171") if m["created_at"] > e["updated_at"]]
    assert later == []


def test_reply_yes_hands_over_to_staff(env):
    db, _, eng, clock = env
    eng.approve(NORTHSIDE, "missed_call", "user:owner")
    eng.tick(NORTHSIDE)
    advance(clock, minutes=3)
    eng.tick(NORTHSIDE)
    r = eng.inbound_sms(NORTHSIDE, "4165550171", "Yes please, tomorrow morning")
    assert r == {"action": "handed_to_staff", "wants_booking": True}
    task = db.one("SELECT title FROM tasks ORDER BY id DESC")
    assert task["title"].startswith("Book")
    advance(clock, days=2)
    eng.tick(NORTHSIDE)
    assert len(followups(db, "4165550171")) == 1  # no more automated texts


def test_booking_after_text_counts_as_recovered_and_won_is_verified(env):
    db, store, eng, clock = env
    eng.approve(NORTHSIDE, "missed_call", "user:owner")
    eng.tick(NORTHSIDE)
    advance(clock, minutes=3)
    eng.tick(NORTHSIDE)
    advance(clock, minutes=30)
    tools = SecretaryTools(db, store, NORTHSIDE, clock=lambda: clock["now"])
    svc = tools.match_service("furnace not heating")
    slot = tools.check_availability(svc["id"])["slots"][0]["slot_id"]
    appt = tools.book_appointment(slot, "Ravi Shah", "416-555-0171", svc["id"])
    lead = tools.save_lead(
        name="Ravi Shah",
        phone="4165550171",
        reason="furnace",
        service_id=svc["id"],
        status="booked",
        qualified=True,
    )
    db.execute("UPDATE appointments SET lead_id = ? WHERE id = ?", (lead, appt["appointment_id"]))
    eng.tick(NORTHSIDE)
    e = db.one("SELECT * FROM enrollments WHERE phone = '4165550171'")
    assert e["status"] == "converted" and e["converted_appointment_id"] == appt["appointment_id"]
    r = eng.results(NORTHSIDE)
    assert r["recovered_bookings"] == 1 and r["verified_recovered_revenue"] == 0
    assert r["estimated_recovered_revenue"] > 0
    db.execute("UPDATE leads SET status = 'won', confirmed_value = 640 WHERE id = ?", (lead,))
    assert eng.results(NORTHSIDE)["verified_recovered_revenue"] == 640


def test_booking_before_first_text_is_not_credited(env):
    db, store, eng, clock = env
    eng.approve(NORTHSIDE, "missed_call", "user:owner")
    eng.tick(NORTHSIDE)  # enrolled, first text due in 2 minutes
    tools = SecretaryTools(db, store, NORTHSIDE, clock=lambda: clock["now"])
    svc = tools.match_service("leaking sink")
    advance(clock, seconds=30)
    slot = tools.check_availability(svc["id"])["slots"][0]["slot_id"]
    tools.book_appointment(slot, "Ana Roy", "4165550171", svc["id"])
    advance(clock, minutes=3)
    eng.tick(NORTHSIDE)
    e = db.one("SELECT * FROM enrollments WHERE phone = '4165550171'")
    assert e["status"] == "stopped" and e["stop_reason"] == "booked"
    assert followups(db, "4165550171") == []
    assert eng.results(NORTHSIDE)["recovered_bookings"] == 0


def test_starter_package_does_not_get_the_engine(env):
    db, _, eng, _ = env
    assert not eng.included(MAPLE)
    assert eng.tick(MAPLE) == {"enrolled": 0, "stopped": 0, "sent": 0, "waiting": 0}


def test_kill_switch_holds_messages_without_dropping_them(env):
    db, store, eng, clock = env
    eng.approve(NORTHSIDE, "missed_call", "user:owner")
    store.set_automation(NORTHSIDE, "revenue_engine", False, "admin")
    eng.tick(NORTHSIDE)
    advance(clock, minutes=3)
    r = eng.tick(NORTHSIDE)
    assert r["sent"] == 0 and r["waiting"] == 2
    store.set_automation(NORTHSIDE, "revenue_engine", True, "admin")
    advance(clock, hours=1, minutes=1)
    assert eng.tick(NORTHSIDE)["sent"] == 2


def test_changed_wording_needs_new_approval(env, monkeypatch):
    db, _, eng, clock = env
    eng.approve(NORTHSIDE, "missed_call", "user:owner")
    eng.tick(NORTHSIDE)
    old = pbmod.PLAYBOOKS["missed_call"]
    new = pbmod.Playbook(**{**old.__dict__, "steps": (pbmod.Step(2, "Changed {business}"),)})
    monkeypatch.setitem(pbmod.PLAYBOOKS, "missed_call", new)
    advance(clock, minutes=3)
    eng.tick(NORTHSIDE)
    assert followups(db) == []
    assert db.one("SELECT stop_reason FROM enrollments LIMIT 1")["stop_reason"] == "not_approved"
    assert eng.playbook_status(NORTHSIDE)[0]["stale"] is True


def test_reactivation_reaches_last_years_customers_once(env):
    db, _, eng, clock = env
    eng.approve(NORTHSIDE, "reactivation", "user:owner")
    eng.tick(NORTHSIDE)
    sent = followups(db)
    phones = {m["to_addr"] for m in sent}
    assert phones == {"4165550191", "4165550192", "6475550193"}
    assert all("about a year" in m["body"] for m in sent)
    advance(clock, days=3)
    eng.tick(NORTHSIDE)
    assert len(followups(db)) == 3


def test_consent_window_is_enforced(env):
    db, _, eng, clock = env
    # a customer from 25 months ago is outside the 2-year implied-consent window
    old = (MON_10AM - timedelta(days=760)).isoformat()
    db.execute("UPDATE leads SET updated_at = ? WHERE phone = '4165550192'", (old,))
    eng.approve(NORTHSIDE, "reactivation", "user:owner")
    eng.tick(NORTHSIDE)
    assert "4165550192" not in {m["to_addr"] for m in followups(db)}


def test_review_request_day_after_a_won_job(env):
    db, _, eng, clock = env
    lead = db.one(
        "SELECT id FROM leads WHERE status = 'booked' AND tenant_id = ? LIMIT 1", (NORTHSIDE,)
    )
    db.execute(
        "UPDATE leads SET status = 'won', confirmed_value = 500, updated_at = ? WHERE id = ?",
        (MON_10AM.isoformat(), lead["id"]),
    )
    eng.approve(NORTHSIDE, "review_request", "user:owner")
    eng.tick(NORTHSIDE)
    assert followups(db) == []
    advance(clock, days=1, minutes=1)
    eng.tick(NORTHSIDE)
    msgs = followups(db)
    assert any("example.com/review" in m["body"] for m in msgs)


def test_one_sequence_per_phone_at_a_time(env):
    db, store, eng, clock = env
    calls = CallService(db, store, clock=lambda: clock["now"])
    r = calls.start(NORTHSIDE)
    for line in ["I need a quote", "It's Dana Wu", "416 555 0171"]:
        calls.turn(NORTHSIDE, r["call_id"], line)
    calls.end(NORTHSIDE, r["call_id"])
    eng.approve(NORTHSIDE, "missed_call", "user:owner")
    eng.approve(NORTHSIDE, "lead_follow_up", "user:owner")
    advance(clock, hours=2)
    eng.tick(NORTHSIDE)
    active = db.query(
        "SELECT playbook FROM enrollments WHERE phone = '4165550171' AND status = 'active'"
    )
    assert len(active) <= 1


def test_owner_approves_on_the_revenue_page(monkeypatch):
    import re

    from fastapi.testclient import TestClient

    from flow.web.app import create_web_app

    monkeypatch.delenv("FLOW_ENV", raising=False)
    clock = {"now": MON_10AM}
    app = create_web_app(
        Database(":memory:"),
        clock=lambda: clock["now"],
        secret="s",
        admin_password=TEST_PW,
        owner_password=TEST_PW,
    )
    c = TestClient(app)
    c.post("/login", data={"email": "owner@northside.example", "password": TEST_PW})
    page = c.get("/app/revenue")
    assert page.status_code == 200 and "waiting for your approval" in page.text
    token = re.search(r'name="csrf" value="([0-9a-f]+)"', page.text).group(1)
    assert (
        c.post(
            "/app/revenue/playbooks/missed_call", data={"action": "approve", "csrf": "bad"}
        ).status_code
        == 403
    )
    r = c.post(
        "/app/revenue/playbooks/missed_call",
        data={"action": "approve", "csrf": token},
        follow_redirects=False,
    )
    assert r.status_code == 303
    clock["now"] += timedelta(minutes=3)
    page = c.get("/app/revenue")
    assert "Sorry we missed your call" in page.text
    assert 'data-metric="followups_sent">2<' in page.text
    # a customer texts STOP through the simulated inbound endpoint
    out = c.post("/api/demo/northside/sms", json={"from_number": "4165550171", "text": "stop"})
    assert out.json()["action"] == "opted_out"

    # a Starter business owner can't turn it on
    m = TestClient(app)
    m.post("/login", data={"email": "owner@maple.example", "password": TEST_PW})
    page = m.get("/app/revenue")
    assert "Not in your package" in page.text
    tok = re.search(r'name="csrf" value="([0-9a-f]+)"', m.get("/app/leads").text)
    if tok:
        assert (
            m.post(
                "/app/revenue/playbooks/missed_call",
                data={"action": "approve", "csrf": tok.group(1)},
            ).status_code
            == 403
        )
