"""Retell phone path: signed tool calls and webhooks put phone calls on the dashboard."""

import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from flow.core.db import Database
from flow.voice.retell import sign, verify_signature
from flow.web.app import create_web_app

NOW = datetime(2026, 11, 23, 15, 0, tzinfo=UTC)  # Monday 10:00 Toronto
KEY = "retell-test-key"
AUTH = {"Authorization": "Bearer demo-northside-key"}
START_MS = int(NOW.timestamp() * 1000)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("RETELL_API_KEY", KEY)
    monkeypatch.delenv("FLOW_ENV", raising=False)
    db = Database(":memory:")
    app = create_web_app(
        db, clock=lambda: NOW, secret="s", admin_password="x" * 12, owner_password="x" * 12
    )
    return TestClient(app), db


def signed(c, path, body, headers=None, key=KEY):
    raw = json.dumps(body).encode()
    h = {"content-type": "application/json", "x-retell-signature": sign(raw, key)}
    h.update(headers or {})
    return c.post(path, content=raw, headers=h)


def test_signature_roundtrip_and_rejections():
    raw = b'{"a":1}'
    now = 1_800_000_000_000
    good = sign(raw, KEY, now)
    assert verify_signature(raw, good, KEY, now_ms=now)
    assert not verify_signature(raw + b" ", good, KEY, now_ms=now)  # body changed
    assert not verify_signature(raw, good, "other-key", now_ms=now)
    assert not verify_signature(raw, good, KEY, now_ms=now + 6 * 60 * 1000)  # replay
    assert not verify_signature(raw, "garbage", KEY, now_ms=now)
    assert not verify_signature(raw, None, KEY, now_ms=now)


def test_unsigned_tool_call_refused_when_retell_configured(env):
    c, _ = env
    r = c.post("/voice/tools/list_services", json={"args": {}}, headers=AUTH)
    assert r.status_code == 401


def test_phone_call_books_and_lands_on_dashboard(env):
    c, db = env
    call = {
        "call_id": "rc_1",
        "direction": "inbound",
        "from_number": "+14165550188",
        "to_number": "+14165550100",
        "start_timestamp": START_MS,
    }
    assert (
        signed(
            c, "/voice/webhooks/retell/northside", {"event": "call_started", "call": call}
        ).status_code
        == 204
    )

    info = signed(
        c, "/voice/tools/business_info", {"name": "business_info", "args": {}, "call": call}, AUTH
    ).json()
    assert info["open_now"] is True and "Monday" in info["local_now"]
    services = signed(c, "/voice/tools/list_services", {"args": {}, "call": call}, AUTH).json()
    heating = next(s for s in services["services"] if s["name"] == "Heating repair")
    slots = signed(
        c,
        "/voice/tools/check_availability",
        {"args": {"service_id": heating["service_id"]}, "call": call},
        AUTH,
    ).json()
    slot = slots["slots"][0]["slot_id"] if "slots" in slots else slots["options"][0]["slot_id"]
    booked = signed(
        c,
        "/voice/tools/book_appointment",
        {
            "args": {
                "slot_id": slot,
                "name": "Priya Shah",
                "phone": "416-555-0188",
                "service_id": heating["service_id"],
                "reason": "furnace not igniting",
            },
            "call": call,
        },
        AUTH,
    )
    assert booked.status_code == 200, booked.text
    lead_id = booked.json()["lead_id"]

    ended = {
        **call,
        "end_timestamp": START_MS + 125_000,
        "disconnection_reason": "user_hangup",
        "transcript_object": [
            {"role": "agent", "content": "Thanks for calling."},
            {"role": "user", "content": "My furnace won't start."},
        ],
    }
    assert (
        signed(
            c, "/voice/webhooks/retell/northside", {"event": "call_ended", "call": ended}
        ).status_code
        == 204
    )
    # duplicate delivery is harmless
    assert (
        signed(
            c, "/voice/webhooks/retell/northside", {"event": "call_ended", "call": ended}
        ).status_code
        == 204
    )

    row = db.one("SELECT * FROM calls WHERE id = 'rc_1'")
    assert row["channel"] == "phone" and row["outcome"] == "booked"
    assert row["billed_minutes"] == 3 and row["lead_id"] == lead_id
    assert row["caller_number"] == "4165550188"
    assert db.one("SELECT COUNT(*) n FROM call_turns WHERE call_id = 'rc_1'")["n"] == 2
    assert db.one("SELECT status FROM leads WHERE id = ?", (lead_id,))["status"] == "booked"
    usage = db.one("SELECT SUM(quantity) q FROM usage_ledger WHERE ref = 'rc_1'")["q"]
    assert usage == 3
    msg = db.one(
        "SELECT status FROM messages WHERE related_appointment_id = ?", (row["appointment_id"],)
    )
    assert msg["status"] == "sent_simulated"


def test_callback_request_and_emergency(env):
    c, db = env
    call = {"call_id": "rc_2", "direction": "inbound", "start_timestamp": START_MS}
    signed(c, "/voice/webhooks/retell/northside", {"event": "call_started", "call": call})
    r = signed(
        c,
        "/voice/tools/request_callback",
        {
            "args": {
                "name": "Tom",
                "phone": "4165550123",
                "reason": "water pouring from ceiling",
                "urgent": True,
            },
            "call": call,
        },
        AUTH,
    )
    assert r.status_code == 200 and r.json()["urgent"] is True
    signed(
        c,
        "/voice/webhooks/retell/northside",
        {
            "event": "call_ended",
            "call": {
                **call,
                "end_timestamp": START_MS + 30_000,
                "transcript_object": [{"role": "user", "content": "help"}],
            },
        },
    )
    assert db.one("SELECT outcome FROM calls WHERE id = 'rc_2'")["outcome"] == "emergency"


def test_webhook_rejects_bad_signature_and_cross_tenant(env):
    c, _ = env
    body = {"event": "call_started", "call": {"call_id": "rc_3", "start_timestamp": START_MS}}
    assert signed(c, "/voice/webhooks/retell/northside", body, key="wrong").status_code == 401
    assert signed(c, "/voice/webhooks/retell/northside", body).status_code == 204
    # the same Retell call id cannot be claimed by another business
    assert signed(c, "/voice/webhooks/retell/maple-dental", body).status_code == 403
    assert signed(c, "/voice/webhooks/retell/nope", body).status_code == 404


def test_webhook_disabled_without_key(monkeypatch):
    monkeypatch.delenv("RETELL_API_KEY", raising=False)
    app = create_web_app(
        Database(":memory:"),
        clock=lambda: NOW,
        secret="s",
        admin_password="x" * 12,
        owner_password="x" * 12,
    )
    r = TestClient(app).post("/voice/webhooks/retell/northside", json={})
    assert r.status_code == 503
