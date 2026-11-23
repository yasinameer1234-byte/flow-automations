"""SMS sending: simulated by default, Twilio when it is configured.

Nothing is texted for real unless ALL of these are set in the environment (never in code):
  FLOW_SMS_PROVIDER=twilio, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER.

Extra safety while testing:
  FLOW_SMS_ALLOWLIST=4165550100,6475550101  only these numbers get real texts; every other
                                            message stays simulated. Required outside
                                            production, so staging can't text strangers.
Demo (synthetic) businesses only ever text numbers on the allowlist.

Statuses written to the outbox:
  sent_simulated  nothing left the building
  sent            the provider accepted it (not yet proof it arrived)
  delivered       the carrier confirmed delivery (from the status webhook)
  undelivered / failed  the provider or carrier gave up; ``error`` says why
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

TWILIO_API = "https://api.twilio.com/2010-04-01"
REAL_STATUSES = ("sent", "delivered")


@dataclass(frozen=True)
class SmsResult:
    status: str  # sent_simulated | sent | failed
    provider_id: str | None = None
    error: str | None = None


def e164(phone: str) -> str | None:
    digits = "".join(c for c in phone or "" if c.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return f"+1{digits}" if len(digits) == 10 else None


class SimulatedSms:
    name = "simulated"
    real = False

    def send(self, to: str, body: str, *, demo: bool = False) -> SmsResult:
        return SmsResult("sent_simulated")


# (method, url, form fields, basic-auth user, password) -> (http status, parsed json)
Transport = Callable[[str, str, dict, str, str], tuple[int, dict]]


def _urllib_transport(method: str, url: str, form: dict, user: str, pw: str) -> tuple[int, dict]:
    data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(url, data=data, method=method)  # noqa: S310 (fixed https URL)
    token = base64.b64encode(f"{user}:{pw}".encode()).decode()
    req.add_header("Authorization", f"Basic {token}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


class TwilioSms:
    name = "twilio"
    real = True

    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        from_number: str,
        *,
        allowlist: set[str] | None = None,
        status_callback: str | None = None,
        transport: Transport | None = None,
    ) -> None:
        self.sid = account_sid
        self.token = auth_token
        self.from_number = from_number
        self.allowlist = allowlist
        self.status_callback = status_callback
        self.transport = transport or _urllib_transport

    def allowed(self, to: str, demo: bool) -> bool:
        number = e164(to)
        if not number:
            return False
        if self.allowlist is not None or demo:
            return number in (self.allowlist or set())
        return True

    def send(self, to: str, body: str, *, demo: bool = False) -> SmsResult:
        if not self.allowed(to, demo):
            return SmsResult("sent_simulated")
        form = {"To": e164(to), "From": self.from_number, "Body": body}
        if self.status_callback:
            form["StatusCallback"] = self.status_callback
        try:
            code, data = self.transport(
                "POST",
                f"{TWILIO_API}/Accounts/{self.sid}/Messages.json",
                form,
                self.sid,
                self.token,
            )
        except (OSError, ValueError) as e:  # network down, DNS, timeout
            return SmsResult("failed", error=f"{type(e).__name__}: {e}"[:200])
        if 200 <= code < 300 and data.get("sid"):
            return SmsResult("sent", provider_id=data["sid"])
        err = data.get("message") or f"HTTP {code}"
        return SmsResult("failed", error=f"{data.get('code', '')} {err}".strip()[:200])

    # Twilio signs webhooks: base64(HMAC-SHA1(auth token, full URL + sorted key+value pairs)).
    def valid_signature(self, url: str, params: dict, signature: str | None) -> bool:
        return valid_twilio_signature(self.token, url, params, signature)


def valid_twilio_signature(token: str, url: str, params: dict, signature: str | None) -> bool:
    if not signature:
        return False
    payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    digest = hmac.new(token.encode(), payload.encode(), hashlib.sha1).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), signature)


def sms_from_env(env: dict | None = None) -> SimulatedSms | TwilioSms:
    env = os.environ if env is None else env
    if env.get("FLOW_SMS_PROVIDER", "").lower() != "twilio":
        return SimulatedSms()
    sid, token, sender = (
        env.get("TWILIO_ACCOUNT_SID"),
        env.get("TWILIO_AUTH_TOKEN"),
        env.get("TWILIO_FROM_NUMBER"),
    )
    if not (sid and token and sender):
        return SimulatedSms()
    raw = env.get("FLOW_SMS_ALLOWLIST", "").strip()
    allow = {n for n in (e164(x) for x in raw.split(",")) if n} if raw else None
    if allow is None and env.get("FLOW_ENV", "local") != "production":
        allow = set()  # outside production, real texts go only to listed test numbers
    public = env.get("FLOW_PUBLIC_URL", "").rstrip("/")
    return TwilioSms(
        sid,
        token,
        sender,
        allowlist=allow,
        status_callback=f"{public}/sms/twilio/status" if public else None,
    )
