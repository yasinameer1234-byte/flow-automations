"""SMS: simulated by default; Twilio only when configured, allow-listed, and honest statuses."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from flow.core.db import Database
from flow.integrations.sms import (
    SimulatedSms,
    TwilioSms,
    e164,
    sms_from_env,
    valid_twilio_signature,
)
from flow.secretary.seed import NORTHSIDE, seed
from flow.secretary.store import Store
from flow.web.app import create_web_app

NOW = datetime(2026, 11, 23, 15, 0, tzinfo=UTC)
TOKEN = "test-auth-token"  # not a real credential
MINE = "6475550123"  # stands in for the tester's own phone on the allow-list


class FakeTwilio:
    def __init__(self, code=201, data=None, boom=None):
        self.sent = []
        self.code, self.data, self.boom = code, data, boom

    def __call__(self, method, url, form, user, pw):
        if self.boom:
            raise self.boom
        self.sent.append(form)
        return self.code, self.data if self.data is not None else {"sid": f"SM{len(self.sent)}"}


def twilio(fake, allow=(MINE,)):
    return TwilioSms(
        "ACtest",
        TOKEN,
        "+16475550000",
        allowlist={e164(n) for n in allow} if allow is not None else None,
        status_callback="https://flow.example/sms/twilio/status",
        transport=fake,
    )


def test_default_is_simulated_and_never_sends():
    assert isinstance(sms_from_env({}), SimulatedSms)
    # half-configured is still simulated
    assert isinstance(sms_from_env({"FLOW_SMS_PROVIDER": "twilio"}), SimulatedSms)


def test_outside_production_real_texts_need_the_allowlist():
    env = {
        "FLOW_SMS_PROVIDER": "twilio",
        "TWILIO_ACCOUNT_SID": "ACx",
        "TWILIO_AUTH_TOKEN": "t",
        "TWILIO_FROM_NUMBER": "+16475550000",
        "FLOW_ENV": "staging",
    }
    s = sms_from_env(env)
    assert isinstance(s, TwilioSms) and s.allowlist == set()
    assert not s.allowed("4165550142", demo=False)
    s = sms_from_env({**env, "FLOW_SMS_ALLOWLIST": "647-555-0123"})
    assert s.allowed(MINE, demo=False) and not s.allowed("4165550142", demo=False)
    prod = sms_from_env({**env, "FLOW_ENV": "production"})
    assert prod.allowlist is None and prod.allowed("4165550142", demo=False)
    assert not prod.allowed("4165550142", demo=True)  # demo businesses: allow-list only


def test_twilio_send_and_failures():
    fake = FakeTwilio()
    s = twilio(fake)
    r = s.send(MINE, "hello")
    assert r.status == "sent" and r.provider_id == "SM1"
    assert fake.sent[0]["To"] == "+16475550123"
    assert fake.sent[0]["StatusCallback"].endswith("/sms/twilio/status")
    assert s.send("4165550142", "hi").status == "sent_simulated"  # not on the list
    assert len(fake.sent) == 1
    bad = twilio(FakeTwilio(code=400, data={"code": 21211, "message": "Invalid 'To'"}))
    r = bad.send(MINE, "x")
    assert r.status == "failed" and "21211" in r.error
    down = twilio(FakeTwilio(boom=OSError("network unreachable")))
    assert down.send(MINE, "x").status == "failed"


def test_signature_check():
    import base64
    import hashlib
    import hmac

    url = "https://example.com/myapp?foo=1&bar=2"
    params = {"To": "+18005551212", "From": "+12349013030", "Body": "C"}
    # Twilio's scheme: URL + params sorted by name, each name followed by its value.
    payload = url + "Body" + "C" + "From" + "+12349013030" + "To" + "+18005551212"
    good = base64.b64encode(hmac.new(b"12345", payload.encode(), hashlib.sha1).digest()).decode()
    assert valid_twilio_signature("12345", url, params, good)
    assert not valid_twilio_signature("12345", url, {**params, "Body": "STOP"}, good)
    assert not valid_twilio_signature("other", url, params, good)
    assert not valid_twilio_signature("12345", url, params, None)


@pytest.fixture
def env():
    db = Database(":memory:")
    seed(
        db,
        password_hasher=lambda p: "plain$" + p,
        admin_password="a",
        owner_password="o",
        history=False,
    )
    return db, Store(db)


def book(db, store, fake, phone):
    from flow.secretary.tools import SecretaryTools

    tools = SecretaryTools(db, store, NORTHSIDE, clock=lambda: NOW, sms=twilio(fake))
    slot = tools.check_availability()["slots"][0]["slot_id"]
    appt = tools.book_appointment(slot, "Priya Shah", phone, tools.default_service()["id"])
    return tools, tools.send_confirmation(appt["appointment_id"])


def test_booking_confirmation_goes_out_for_real_only_to_allowed_numbers(env):
    db, store = env
    fake = FakeTwilio()
    _, status = book(db, store, fake, MINE)
    assert status == "sent" and len(fake.sent) == 1
    msg = db.one("SELECT * FROM messages ORDER BY id DESC LIMIT 1")
    assert msg["provider"] == "twilio" and msg["provider_id"] == "SM1"
    _, status = book(db, store, fake, "4165550142")
    assert status == "sent_simulated" and len(fake.sent) == 1


def test_failed_text_is_recorded_and_never_promised(env):
    db, store = env
    _, status = book(db, store, FakeTwilio(code=500, data={"message": "boom"}), MINE)
    assert status == "failed"
    assert db.one("SELECT COUNT(*) AS n FROM audit_log WHERE level = 'error'")["n"] >= 1


def test_delivery_receipt_webhook(monkeypatch):
    monkeypatch.delenv("FLOW_ENV", raising=False)
    monkeypatch.setenv("FLOW_PUBLIC_URL", "https://flow.example")
    db = Database(":memory:")
    app = create_web_app(db, clock=lambda: NOW, secret="s", admin_password="p", owner_password="p")
    app.state.revenue.sms = twilio(FakeTwilio())
    db.execute(
        "INSERT INTO messages (tenant_id, channel, to_addr, body, purpose, status, created_at,"
        " provider, provider_id) VALUES (?, 'sms', ?, 'x', 'transactional', 'sent', ?, 'twilio',"
        " 'SM42')",
        (NORTHSIDE, MINE, NOW.isoformat()),
    )
    c = TestClient(app)
    form = {"MessageSid": "SM42", "MessageStatus": "delivered"}
    assert c.post("/sms/twilio/status", data=form).status_code == 403  # unsigned
    import base64
    import hashlib
    import hmac

    url = "https://flow.example/sms/twilio/status"
    payload = url + "".join(f"{k}{form[k]}" for k in sorted(form))
    sig = base64.b64encode(hmac.new(TOKEN.encode(), payload.encode(), hashlib.sha1).digest())
    r = c.post("/sms/twilio/status", data=form, headers={"X-Twilio-Signature": sig.decode()})
    assert r.status_code == 204
    assert db.one("SELECT status FROM messages WHERE provider_id = 'SM42'")["status"] == "delivered"


def test_webhooks_are_off_without_twilio(monkeypatch):
    monkeypatch.delenv("FLOW_ENV", raising=False)
    monkeypatch.delenv("FLOW_SMS_PROVIDER", raising=False)
    app = create_web_app(
        Database(":memory:"), clock=lambda: NOW, secret="s", admin_password="p", owner_password="p"
    )
    assert TestClient(app).post("/sms/twilio/status", data={}).status_code == 503
