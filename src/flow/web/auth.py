"""Minimal session auth for the MVP: scrypt password hashes, HMAC-signed cookies, roles.

Good enough for a local demo and a pilot behind HTTPS. Before self-serve signup we
move to a hosted identity provider (SSO, MFA, password reset).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

ROLES = ("platform_admin", "owner", "staff", "viewer")
SESSION_COOKIE = "flow_session"
SESSION_TTL = 8 * 3600


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1)
    return hmac.compare_digest(digest.hex(), digest_hex)


class SessionSigner:
    def __init__(self, secret: str | None = None) -> None:
        self.secret = (
            secret or os.environ.get("FLOW_SECRET_KEY") or secrets.token_hex(32)
        ).encode()

    def _sig(self, payload: str) -> str:
        return hmac.new(self.secret, payload.encode(), hashlib.sha256).hexdigest()

    def dumps(self, user_id: int) -> str:
        payload = base64.urlsafe_b64encode(
            json.dumps(
                {
                    "uid": user_id,
                    "exp": int(time.time()) + SESSION_TTL,
                    "csrf": secrets.token_hex(8),
                }
            ).encode()
        ).decode()
        return f"{payload}.{self._sig(payload)}"

    def loads(self, token: str | None) -> dict | None:
        if not token or "." not in token:
            return None
        payload, sig = token.rsplit(".", 1)
        if not hmac.compare_digest(sig, self._sig(payload)):
            return None
        try:
            data = json.loads(base64.urlsafe_b64decode(payload.encode()))
        except ValueError:
            return None
        return data if data.get("exp", 0) > time.time() else None

    def csrf_token(self, session: dict) -> str:
        return self._sig(f"csrf:{session['uid']}:{session['csrf']}")[:32]

    # ---------------------------------------------------------------- demo invite links
    # Signed with a "demo:" prefix so a demo link can never be replayed as a session cookie.
    def demo_link_token(self, slug: str, hours: float, label: str = "") -> str:
        payload = base64.urlsafe_b64encode(
            json.dumps(
                {"demo": slug, "exp": int(time.time() + hours * 3600), "label": label[:60]}
            ).encode()
        ).decode()
        return f"{payload}.{self._sig('demo:' + payload)}"

    def demo_link_loads(self, token: str | None, slug: str) -> dict | None:
        if not token or "." not in token:
            return None
        payload, sig = token.rsplit(".", 1)
        if not hmac.compare_digest(sig, self._sig("demo:" + payload)):
            return None
        try:
            data = json.loads(base64.urlsafe_b64decode(payload.encode()))
        except ValueError:
            return None
        if data.get("demo") != slug or data.get("exp", 0) <= time.time():
            return None
        return data
