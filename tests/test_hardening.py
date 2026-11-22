"""Launch-audit fixes: demo gating, rate limits, real-client onboarding, honest metrics."""

import tomllib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from flow.core.db import Database
from flow.onboarding import ClientProfile, OnboardingError, create_tenant, load_profile
from flow.web.app import create_web_app
from flow.web.auth import hash_password
from flow.web.ratelimit import RateLimiter

NOW = datetime(2026, 11, 23, 15, 0, tzinfo=UTC)
PW = "test-pass-123"
EXAMPLE = Path(__file__).parent.parent / "config" / "clients" / "example-plumbing.toml"


@pytest.fixture
def app(monkeypatch):
    monkeypatch.delenv("FLOW_ENV", raising=False)
    db = Database(":memory:")
    a = create_web_app(db, clock=lambda: NOW, secret="s", admin_password=PW, owner_password=PW)
    out = create_tenant(db, load_profile(EXAMPLE), owner_password_hash=hash_password(PW))
    a.state.real = out
    a.state.db = db
    return a


def test_example_profile_is_valid():
    p = load_profile(EXAMPLE)
    assert p.business.slug == "example-plumbing" and p.services


@pytest.mark.parametrize(
    "path,value,msg",
    [
        (("business", "timezone"), "Mars/Base", "unknown timezone"),
        (("business", "package"), "platinum", "package must be one of"),
        (("business", "business_phone"), "12345", "10-digit"),
        (("hours",), {"mon": ["17:00", "08:00"]}, "open before close"),
        (("hours",), {"funday": ["08:00", "17:00"]}, "unknown day"),
    ],
)
def test_profile_validation_rejects_bad_facts(path, value, msg):
    data = tomllib.loads(EXAMPLE.read_text())
    target = data
    for k in path[:-1]:
        target = target[k]
    target[path[-1]] = value
    with pytest.raises(ValidationError, match=msg):
        ClientProfile.model_validate(data)


def test_real_client_can_log_in_and_sees_only_its_business(app):
    c = TestClient(app)
    r = c.post(
        "/login", data={"email": "owner@example.com", "password": PW}, follow_redirects=False
    )
    assert r.status_code == 303
    page = c.get("/app")
    assert "Example Plumbing Ltd." in page.text
    assert c.get("/app?tenant=t_northside").status_code == 403


def test_duplicate_client_is_refused(app):
    with pytest.raises(OnboardingError, match="already exists"):
        create_tenant(app.state.db, load_profile(EXAMPLE), owner_password_hash="x")


def test_real_clients_have_no_public_demo(app):
    c = TestClient(app)
    assert c.get("/demo/example-plumbing").status_code == 404
    assert c.post("/api/demo/example-plumbing/calls").status_code == 404
    assert (
        c.post(
            "/api/demo/example-plumbing/sms", json={"from_number": "4165550100", "text": "hi"}
        ).status_code
        == 404
    )
    assert c.get("/demo/northside").status_code == 200  # synthetic demo business still works


def test_demo_calls_are_rate_limited(app):
    c = TestClient(app)
    codes = [c.post("/api/demo/northside/calls").status_code for _ in range(12)]
    assert codes[:10] == [200] * 10 and codes[10:] == [429, 429]


def test_login_locks_after_five_failures(app):
    c = TestClient(app)
    for _ in range(5):
        r = c.post("/login", data={"email": "owner@northside.example", "password": "wrong"})
        assert r.status_code == 200 and "Wrong email or password" in r.text
    r = c.post(
        "/login", data={"email": "owner@northside.example", "password": PW}, follow_redirects=False
    )
    assert r.status_code == 429  # even the right password waits out the lockout
    # other accounts are unaffected
    r = c.post("/login", data={"email": "admin@flow.local", "password": PW}, follow_redirects=False)
    assert r.status_code == 303


def test_rate_limiter_window_slides():
    t = {"now": 0.0}
    rl = RateLimiter(2, 10, clock=lambda: t["now"])
    assert rl.hit("k") and rl.hit("k") and not rl.hit("k")
    t["now"] = 10.5
    assert rl.hit("k")


def test_missed_calls_count_hang_ups(app):
    from flow.secretary.metrics import tenant_metrics

    m = tenant_metrics(app.state.db, app.state.ctx.store, "t_northside", now=NOW)
    # seeded history: 2 assistant-off calls + 3 hang-ups + 2 fresh missed calls
    assert m["calls_missed"] == 7
