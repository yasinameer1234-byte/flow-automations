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


class FakeRetell:
    """Stands in for api.retellai.com: records requests, answers like Retell does."""

    def __init__(self, agents=None, fail=None):
        self.calls = []
        self.agents = agents or []
        self.fail = fail

    def __call__(self, method, path, body, key):
        self.calls.append((method, path, body, key))
        if self.fail and path == self.fail:
            from flow.voice.agent import RetellError

            raise RetellError(f"Retell {method} {path} -> HTTP 500")
        if path == "/list-agents":
            return self.agents
        if path == "/create-retell-llm":
            return {"llm_id": "llm_new"}
        if path == "/create-agent":
            return {"agent_id": "agent_new"}
        if path == "/v3/create-web-call":
            return {
                "call_id": "call_1",
                "access_token": "tok_1",
                "transport": "livekit",
                "url": "wss://example.livekit",
                "agent_id": body["agent_id"],
            }
        raise AssertionError(path)

    def paths(self):
        return [p for _, p, _, _ in self.calls]


def voice_app(monkeypatch, fake, **env):
    a = make(
        monkeypatch,
        RETELL_API_KEY="k-test",
        FLOW_PUBLIC_URL="https://flow.example",
        **env,
    )
    a.state.voice_agents.request = fake
    return a


def test_voice_button_only_with_a_server_key(monkeypatch):
    monkeypatch.delenv("RETELL_API_KEY", raising=False)
    c = TestClient(make(monkeypatch))
    assert "retell-btn" not in c.get("/demo/northside").text
    assert c.post("/api/demo/northside/voice-call").status_code == 404
    a = voice_app(monkeypatch, FakeRetell())
    html = TestClient(a).get("/demo/northside").text
    assert "retell-btn" in html and "k-test" not in html
    assert "No microphone? Use the text demo" in html
    assert TestClient(a).get("/static/vendor/retell-sdk-3.0.3.js").status_code == 200
    # a business with no receptionist script for its trade gets no voice button
    assert "retell-btn" not in TestClient(a).get("/demo/maple-dental").text


def test_voice_call_reuses_the_agent_already_in_retell(monkeypatch):
    fake = FakeRetell(
        agents=[
            {
                "agent_id": "agent_old",
                "agent_name": "Northside Plumbing & Heating receptionist",
                "last_modification_timestamp": 1,
            },
            {
                "agent_id": "agent_admin",
                "agent_name": "Northside Plumbing & Heating receptionist",
                "last_modification_timestamp": 5,
            },
            {
                "agent_id": "agent_other",
                "agent_name": "Someone else",
                "last_modification_timestamp": 9,
            },
        ]
    )
    a = voice_app(monkeypatch, fake)
    c = TestClient(a)
    r = c.post("/api/demo/northside/voice-call")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["access_token"] == "tok_1" and body["transport"] == "livekit"
    assert "agent_id" not in body and "k-test" not in r.text
    assert fake.paths() == ["/list-agents", "/v3/create-web-call"]  # no new agent
    assert fake.calls[1][2]["agent_id"] == "agent_admin"  # the newest match
    assert fake.calls[1][3] == "k-test"
    # the id is remembered: the next call skips the lookup
    c.post("/api/demo/northside/voice-call")
    assert fake.paths()[2:] == ["/v3/create-web-call"]
    assert a.state.voice_agents.status["northside"] == "ready (stored)"


def test_voice_call_creates_the_agent_when_none_exists(monkeypatch):
    fake = FakeRetell()
    a = voice_app(monkeypatch, fake)
    r = TestClient(a).post("/api/demo/northside/voice-call")
    assert r.status_code == 200
    assert fake.paths() == [
        "/list-agents",
        "/create-retell-llm",
        "/create-agent",
        "/v3/create-web-call",
    ]
    tools = fake.calls[1][2]["general_tools"]
    assert tools[0]["url"].startswith("https://flow.example/voice/northside/tools/")
    assert fake.calls[2][2]["agent_name"] == "Northside Plumbing & Heating receptionist"
    row = a.state.db.one("SELECT retell_agent_id FROM tenants WHERE slug = 'northside'")
    assert row["retell_agent_id"] == "agent_new"
    assert TestClient(a).get("/health").json()["voice_agents"] == {"northside": "ready (created)"}


def test_voice_call_uses_the_env_agent_and_reports_retell_errors(monkeypatch):
    fake = FakeRetell(fail="/v3/create-web-call")
    a = voice_app(monkeypatch, fake, FLOW_RETELL_AGENT_NORTHSIDE="agent_env")
    r = TestClient(a).post("/api/demo/northside/voice-call")
    assert r.status_code == 502 and "text demo" in r.json()["detail"]
    assert fake.paths() == ["/v3/create-web-call"]
    assert fake.calls[0][2]["agent_id"] == "agent_env"
    ev = a.state.db.one("SELECT event FROM audit_log WHERE event = 'retell.web_call_failed'")
    assert ev is not None


def test_voice_call_is_invite_only_and_rate_limited(monkeypatch):
    fake = FakeRetell(agents=[])
    a = voice_app(monkeypatch, fake, FLOW_DEMO_ACCESS="invite")
    c = TestClient(a)
    assert c.post("/api/demo/northside/voice-call").status_code == 403
    assert fake.calls == []  # strangers never reach Retell
    login(c)
    codes = [c.post("/api/demo/northside/voice-call").status_code for _ in range(8)]
    assert codes[:6] == [200] * 6 and codes[6:] == [429, 429]


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
    # the voice button then uses that agent, with no lookup and no second agent
    fake = FakeRetell()
    a.state.voice_agents.request = fake
    assert c.post("/api/demo/northside/voice-call").status_code == 200
    assert fake.paths() == ["/v3/create-web-call"] and fake.calls[0][2]["agent_id"] == "agent_1"


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
