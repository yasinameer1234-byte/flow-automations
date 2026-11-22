"""Web app: login, roles, tenant isolation, CSRF, kill switch, browser call API, voice tools."""

import re
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from flow.core.db import Database
from flow.web.app import create_web_app

NOW = datetime(2026, 11, 23, 15, 0, tzinfo=UTC)
PW = "test-pass-123"


@pytest.fixture(scope="module")
def app():
    return create_web_app(
        Database(":memory:"),
        clock=lambda: NOW,
        secret="test-secret",
        admin_password=PW,
        owner_password=PW,
    )


def login(app, email):
    c = TestClient(app)
    r = c.post("/login", data={"email": email, "password": PW}, follow_redirects=False)
    assert r.status_code == 303
    return c


def csrf(html):
    return re.search(r'name="csrf" value="([0-9a-f]+)"', html).group(1)


def test_login_required_and_bad_password(app):
    c = TestClient(app)
    assert c.get("/app", follow_redirects=False).status_code == 303
    r = c.post("/login", data={"email": "owner@northside.example", "password": "nope"})
    assert "Wrong email or password" in r.text


def test_owner_sees_own_dashboard_only(app):
    c = login(app, "owner@northside.example")
    r = c.get("/app")
    assert r.status_code == 200 and "Northside Plumbing" in r.text
    assert "Verified revenue" in r.text and "Estimated revenue" in r.text
    assert c.get("/app?tenant=t_maple").status_code == 403
    assert c.get("/admin").status_code == 403


def test_tampered_cookie_is_rejected(app):
    c = login(app, "owner@northside.example")
    token = c.cookies.get("flow_session")
    c.cookies.set("flow_session", token[:-2] + ("00" if token[-2:] != "00" else "11"))
    assert c.get("/app", follow_redirects=False).status_code == 303


def test_browser_call_creates_lead_visible_on_dashboard(app):
    c = TestClient(app)
    r = c.post("/api/demo/northside/calls").json()
    cid = r["call_id"]
    for line in [
        "My toilet is clogged",
        "Nadia Okafor",
        "416 555 0190",
        "tomorrow afternoon",
        "the first one",
        "yes",
    ]:
        r = c.post(f"/api/demo/northside/calls/{cid}/turn", json={"text": line}).json()
    assert r["reply"].startswith("You're booked")
    end = c.post(f"/api/demo/northside/calls/{cid}/end").json()
    assert end["outcome"] == "booked"
    owner = login(app, "owner@northside.example")
    assert "Nadia Okafor" in owner.get("/app").text
    assert "Nadia Okafor" in owner.get("/app/leads").text
    assert "check_availability" in owner.get(f"/app/calls/{cid}").text


def test_marking_lead_won_needs_csrf_and_value(app):
    owner = login(app, "owner@northside.example")
    page = owner.get("/app/leads").text
    lead_id = re.search(r'data-lead="(\d+)"', page).group(1)
    url = f"/app/leads/{lead_id}/outcome"
    assert owner.post(url, data={"outcome": "won", "value": "500"}).status_code == 403
    token = csrf(page)
    assert owner.post(url, data={"outcome": "won", "csrf": token}).status_code == 400
    r = owner.post(
        url, data={"outcome": "won", "value": "500", "csrf": token}, follow_redirects=False
    )
    assert r.status_code == 303


def test_admin_kill_switch(app):
    admin = login(app, "admin@flow.local")
    page = admin.get("/admin").text
    assert "Maple Grove Dental" in page and "Northside" in page
    r = admin.post(
        "/admin/tenants/t_maple/automations/ai_secretary",
        data={"enabled": "0", "csrf": csrf(page)},
        follow_redirects=False,
    )
    assert r.status_code == 303
    call = TestClient(app).post("/api/demo/maple-dental/calls").json()
    assert call["ended"] and "can't take your call" in call["reply"]
    admin.post(
        "/admin/tenants/t_maple/automations/ai_secretary",
        data={"enabled": "1", "csrf": csrf(admin.get("/admin").text)},
    )
    assert not TestClient(app).post("/api/demo/maple-dental/calls").json()["ended"]


def test_voice_tools_need_tenant_key(app):
    c = TestClient(app)
    body = {"name": "check_availability", "args": {}, "call": {"call_id": "retell_1"}}
    assert c.post("/voice/tools/check_availability", json=body).status_code == 401
    h = {"Authorization": "Bearer demo-northside-key"}
    slots = c.post("/voice/tools/check_availability", json=body, headers=h).json()["slots"]
    assert slots
    bad = c.post(
        "/voice/tools/book_appointment",
        headers=h,
        json={
            "args": {
                "slot_id": "2026-11-29T15:00:00Z",
                "name": "X Y",
                "phone": "4165550100",
                "service_id": 1,
            }
        },
    )
    assert bad.status_code == 422 and bad.json()["error"] == "slot_unavailable"
    ok = c.post(
        "/voice/tools/book_appointment",
        headers=h,
        json={
            "args": {
                "slot_id": slots[0]["slot_id"],
                "name": "Voice Caller",
                "phone": "4165550199",
                "service_id": 1,
            }
        },
    )
    assert ok.status_code == 200 and ok.json()["appointment_id"]
    ans = c.post(
        "/voice/tools/answer_question",
        headers=h,
        json={"args": {"question": "Are you licensed and insured?"}},
    ).json()
    assert ans["answer"].startswith("Yes")
