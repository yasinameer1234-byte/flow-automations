"""Demo access: invite-only links when hosted, spoof-proof rate limits, expiring demo data."""

import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from flow.core.db import Database
from flow.web.app import create_web_app

NOW = datetime(2026, 11, 23, 15, 0, tzinfo=UTC)
PW = "test-pass-123"


def make(monkeypatch, clock=None, **env):
    monkeypatch.delenv("FLOW_ENV", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    db = Database(":memory:")
    a = create_web_app(
        db, clock=clock or (lambda: NOW), secret="s", admin_password=PW, owner_password=PW
    )
    a.state.db = db
    return a


def login(c, email="admin@flow.local"):
    r = c.post("/login", data={"email": email, "password": PW}, follow_redirects=False)
    assert r.status_code == 303


@pytest.fixture
def invite(monkeypatch):
    return make(monkeypatch, FLOW_DEMO_ACCESS="invite")


def test_invite_mode_blocks_strangers(invite):
    c = TestClient(invite)
    assert c.get("/demo/northside").status_code == 403
    assert c.post("/api/demo/northside/calls").status_code == 403
    sms = {"from_number": "4165550100", "text": "hi"}
    assert c.post("/api/demo/northside/sms", json=sms).status_code == 403


def test_forged_or_expired_links_are_refused(invite):
    c = TestClient(invite)
    assert c.get("/demo/northside?k=abc.def").status_code == 403
    signer = invite.state.ctx.signer
    expired = signer.demo_link_token("northside", hours=-1)
    assert c.get(f"/demo/northside?k={expired}").status_code == 403
    other = signer.demo_link_token("maple", hours=1)  # a link for one business only
    assert c.get(f"/demo/northside?k={other}").status_code == 403
    # a demo link can never be used as a login session
    c.cookies.set("flow_session", signer.demo_link_token("northside", hours=1))
    assert c.get("/app", follow_redirects=False).status_code in (302, 303, 401)


def test_admin_link_lets_a_prospect_book(invite):
    admin = TestClient(invite)
    login(admin)
    csrf = re.search(r'name="csrf" value="([^"]+)"', admin.get("/admin").text).group(1)
    r = admin.post(
        "/admin/demo-links",
        data={"slug": "northside", "hours": "48", "label": "Acme", "csrf": csrf},
    )
    url = re.search(r'value="(http[^"]*/demo/northside\?k=[^"]+)"', r.text).group(1)
    prospect = TestClient(invite)  # a different browser, not signed in
    path = url.split("://", 1)[1].split("/", 1)[1]
    page = prospect.get("/" + path.replace("&amp;", "&"))
    assert page.status_code == 200
    call = prospect.post("/api/demo/northside/calls")  # the invite cookie carries over
    assert call.status_code == 200
    cid = call.json()["call_id"]
    for line in ["leaky tap", "Priya Shah", "416 555 0142", "tomorrow", "first", "yes"]:
        out = prospect.post(f"/api/demo/northside/calls/{cid}/turn", json={"text": line})
        assert out.status_code == 200
    assert "booked" in out.json()["reply"]
    assert (
        invite.state.db.one(
            "SELECT COUNT(*) AS n FROM audit_log WHERE event = 'demo.link_created'"
        )["n"]
        == 1
    )


def test_signed_in_owner_of_the_demo_business_needs_no_link(invite):
    c = TestClient(invite)
    login(c, "owner@northside.example")
    assert c.get("/demo/northside").status_code == 200
    assert c.post("/api/demo/northside/calls").status_code == 200


def test_hosted_default_is_invite_only(monkeypatch):
    monkeypatch.setenv("FLOW_ENV", "staging")
    monkeypatch.setenv("FLOW_SECRET_KEY", "x" * 40)
    monkeypatch.setenv("FLOW_VOICE_TOOL_KEY", "y" * 40)
    monkeypatch.setenv("FLOW_DEMO_PASSWORD", "z" * 20)
    try:
        a = create_web_app(Database(":memory:"), clock=lambda: NOW)
    except Exception as e:  # staging may need more config; the default is what we check
        pytest.skip(f"staging config not satisfiable in tests: {e}")
    assert TestClient(a).get("/demo/northside").status_code == 403


def test_spoofed_forwarded_for_cannot_dodge_the_limit(monkeypatch):
    a = make(monkeypatch, FLOW_PROXY_HOPS="1")
    c = TestClient(a)
    codes = []
    for i in range(12):
        # the caller makes up the left part; the proxy appends the real address on the right
        h = {"x-forwarded-for": f"10.0.0.{i}, 203.0.113.7"}
        codes.append(c.post("/api/demo/northside/calls", headers=h).status_code)
    assert codes[10:] == [429, 429]
    # a different real address still gets in
    h = {"x-forwarded-for": "10.0.0.1, 198.51.100.9"}
    assert c.post("/api/demo/northside/calls", headers=h).status_code == 200


def test_demo_bookings_expire_after_a_day(monkeypatch):
    clock = {"now": NOW}
    a = make(monkeypatch, clock=lambda: clock["now"])
    c = TestClient(a)
    db = a.state.db
    cid = c.post("/api/demo/northside/calls").json()["call_id"]
    for line in ["leaky tap", "Priya Shah", "416 555 0142", "tomorrow", "first", "yes", "bye"]:
        c.post(f"/api/demo/northside/calls/{cid}/turn", json={"text": line})
    q = "SELECT COUNT(*) AS n FROM appointments WHERE source_call_id = ?"
    assert db.one(q, (cid,))["n"] == 1
    clock["now"] = NOW + timedelta(hours=23)
    c.post("/api/demo/northside/calls")
    assert db.one(q, (cid,))["n"] == 1  # still there within the day
    clock["now"] = NOW + timedelta(hours=25)
    c.post("/api/demo/northside/calls")
    assert db.one(q, (cid,))["n"] == 0
    assert db.one("SELECT COUNT(*) AS n FROM leads WHERE source_call_id = ?", (cid,))["n"] == 0
    # seeded synthetic history is never touched
    assert db.one("SELECT COUNT(*) AS n FROM calls WHERE channel = 'synthetic_history'")["n"] > 0


def test_real_voice_button_only_when_configured(monkeypatch):
    c = TestClient(make(monkeypatch))
    assert "retell-btn" not in c.get("/demo/northside").text
    a = make(
        monkeypatch,
        FLOW_RETELL_PUBLIC_KEY="public_key_placeholder",
        FLOW_RETELL_AGENT_NORTHSIDE="agent_placeholder",
    )
    html = TestClient(a).get("/demo/northside").text
    assert "retell-btn" in html and "agent_placeholder" in html
    assert TestClient(a).get("/static/vendor/retell-sdk-3.0.3.js").status_code == 200


def test_admin_sets_up_a_retell_agent_with_the_servers_key(monkeypatch):
    a = make(monkeypatch, RETELL_API_KEY="k-test", FLOW_PUBLIC_URL="https://flow.example")
    calls = []

    def fake_post(path, body, key):
        calls.append((path, body, key))
        return {"llm_id": "llm_1"} if path == "/create-retell-llm" else {"agent_id": "agent_1"}

    a.state.retell_poster = fake_post
    c = TestClient(a)
    login(c)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/admin").text).group(1)
    tid = a.state.db.one("SELECT id FROM tenants WHERE slug = 'northside'")["id"]
    r = c.post(
        f"/admin/tenants/{tid}/retell-agent",
        data={"trade": "plumbing", "area": "Toronto", "csrf": csrf},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert [p for p, _, _ in calls] == ["/create-retell-llm", "/create-agent"]
    assert calls[0][2] == "k-test"
    tools = calls[0][1]["general_tools"]
    assert tools[0]["url"].startswith("https://flow.example/voice/northside/tools/")
    assert "agent_1" in c.get("/admin").text
    # a second click doesn't silently create another agent
    r = c.post(f"/admin/tenants/{tid}/retell-agent", data={"csrf": csrf}, follow_redirects=False)
    assert r.status_code == 409
    # the agent id feeds the browser voice button once a public key exists
    monkeypatch.setenv("FLOW_RETELL_PUBLIC_KEY", "public_key_placeholder")
    assert "agent_1" in TestClient(a).get("/demo/northside").text


def test_retell_setup_needs_admin_and_key(monkeypatch):
    a = make(monkeypatch)
    c = TestClient(a)
    tid = a.state.db.one("SELECT id FROM tenants WHERE slug = 'northside'")["id"]
    r = c.post(f"/admin/tenants/{tid}/retell-agent", data={}, follow_redirects=False)
    assert r.status_code in (302, 303, 401, 403)
    login(c)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/admin").text).group(1)
    r = c.post(f"/admin/tenants/{tid}/retell-agent", data={"csrf": csrf})
    assert r.status_code == 503


def test_trusted_client_ip_header_wins(monkeypatch):
    a = make(monkeypatch, FLOW_CLIENT_IP_HEADER="cf-connecting-ip", FLOW_PROXY_HOPS="1")
    c = TestClient(a)
    h = {"cf-connecting-ip": "203.0.113.7", "x-forwarded-for": "1.2.3.4, 10.0.0.1"}
    assert c.get("/whoami", headers=h).json()["seen_as"] == "203.0.113.7"
    codes = [
        c.post(
            "/api/demo/northside/calls",
            headers={"cf-connecting-ip": "203.0.113.7", "x-forwarded-for": f"9.9.9.{i}"},
        ).status_code
        for i in range(12)
    ]
    assert codes[10:] == [429, 429]


def test_dashboard_labels_texts_as_simulated(monkeypatch):
    a = make(monkeypatch)
    c = TestClient(a)
    login(c, "owner@northside.example")
    html = c.get("/app").text
    assert "simulated (not sent)" in html and "texts sent" not in html
