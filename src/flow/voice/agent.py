"""Keeps one Retell voice agent per business and starts browser voice calls with it.

The Retell API key stays on the server. The agent is found or created on demand:
the id stored on the business, then FLOW_RETELL_AGENT_<SLUG>, then an existing agent with
the expected name in the Retell account (so a server restart, which wipes the free tier's
disk, never creates duplicates), and only then a new one. Creating an agent is free.
"""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

from ..core.db import Database
from . import setup as retell_setup

API = retell_setup.API


def _request(method: str, path: str, body: dict | None, key: str) -> dict | list:
    req = urllib.request.Request(  # noqa: S310 (fixed https host)
        API + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 (fixed https host)
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        detail = e.read()[:300].decode("utf-8", "replace")
        raise RetellError(f"Retell {method} {path} -> HTTP {e.code}: {detail}") from e


class RetellError(RuntimeError):
    pass


Requester = Callable[[str, str, "dict | None", str], "dict | list"]


class VoiceAgents:
    def __init__(
        self,
        db: Database,
        key: str,
        base_url: Callable[[], str],
        requester: Requester | None = None,
    ) -> None:
        self.db = db
        self.key = key
        self.base_url = base_url
        self.request = requester or _request
        self.status: dict[str, str] = {}
        self._chat_agents: dict[str, str] = {}
        self._lock = threading.Lock()

    @staticmethod
    def agent_name(business: str) -> str:
        return f"{business} receptionist"

    @staticmethod
    def trade_for(tenant: dict) -> str | None:
        """The receptionist script that fits this business, or None (no voice agent)."""
        name = tenant["name"].lower()
        for word, trade in (
            ("plumb", "plumbing"),
            ("hvac", "hvac"),
            ("heating", "hvac"),
            ("air", "hvac"),
            ("electric", "electrical"),
        ):
            if word in name:
                return trade
        return None

    def _post(self, path: str, body: dict, key: str) -> dict:
        return self.request("POST", path, body, key)  # type: ignore[return-value]

    def ensure(
        self, tenant: dict, trade: str = "plumbing", area: str = "Toronto and the GTA"
    ) -> str:
        """Return this business's agent id, finding or creating the agent if needed."""
        if not self.key:
            raise RetellError("RETELL_API_KEY is not set on the server")
        slug = tenant["slug"]
        with self._lock:
            row = self.db.one("SELECT retell_agent_id FROM tenants WHERE id = ?", (tenant["id"],))
            agent_id = (row or {}).get("retell_agent_id") or os.environ.get(
                "FLOW_RETELL_AGENT_" + slug.upper().replace("-", "_")
            )
            how = "stored"
            try:
                if not agent_id:
                    want = self.agent_name(tenant["name"])
                    found = self.request(
                        "POST",
                        "/v2/list-agents?limit=1000",
                        {
                            "filter_criteria": {
                                "channel": {"type": "string", "op": "eq", "value": "voice"},
                                "query": want,
                            }
                        },
                        self.key,
                    )
                    items = found.get("items", []) if isinstance(found, dict) else found
                    matches = [a for a in items or [] if a.get("agent_name") == want]
                    if matches:
                        newest = max(
                            matches,
                            key=lambda a: a.get(
                                "user_modified_timestamp", a.get("last_modification_timestamp", 0)
                            ),
                        )
                        agent_id, how = newest["agent_id"], "found"
                if not agent_id:
                    if not self.base_url():
                        raise RetellError("no public URL for the agent's tools (FLOW_PUBLIC_URL)")
                    self.status[slug] = "creating"
                    ids = retell_setup.create_agent(
                        self.key,
                        self.base_url(),
                        slug,
                        trade,
                        tenant["name"],
                        area,
                        poster=self._post,
                    )
                    agent_id, how = ids["agent_id"], "created"
            except Exception as e:
                self.status[slug] = f"error: {str(e)[:200]}"
                raise
            self.db.execute(
                "UPDATE tenants SET retell_agent_id = ? WHERE id = ?", (agent_id, tenant["id"])
            )
            self.status[slug] = f"ready ({how})"
            return agent_id

    def web_call(self, tenant: dict, trade: str, metadata: dict | None = None) -> dict:
        """Start a browser voice call. Returns what the browser SDK needs, never the key."""
        agent_id = self.ensure(tenant, trade)
        res = self.request(
            "POST",
            "/v3/create-web-call",
            {"agent_id": agent_id, "metadata": metadata or {}},
            self.key,
        )
        if not isinstance(res, dict) or not res.get("access_token"):
            raise RetellError("Retell returned no access token")
        return {
            k: res.get(k)
            for k in ("call_id", "access_token", "transport", "url", "ice_servers")
            if res.get(k) is not None
        }

    def call_info(self, tenant: dict, call_id: str) -> dict:
        """What happened on a web call (status, why it ended, transcript), for the demo page."""
        res = self.request("GET", f"/v2/get-call/{urllib.parse.quote(call_id)}", None, self.key)
        if not isinstance(res, dict):
            raise RetellError("Retell returned no call")
        row = self.db.one("SELECT retell_agent_id FROM tenants WHERE id = ?", (tenant["id"],))
        if res.get("agent_id") != (row or {}).get("retell_agent_id"):
            raise PermissionError("call belongs to another agent")
        return {
            k: res.get(k)
            for k in (
                "call_id",
                "call_status",
                "disconnection_reason",
                "duration_ms",
                "transcript",
            )
        }

    # ---------------------------------------------------------------- text chat, same brain
    # A Retell chat agent that shares the voice agent's LLM (prompt and tools): lets anyone
    # without a microphone, and our own automated tests, talk to the real receptionist in text.

    def chat_agent(self, tenant: dict, trade: str) -> str:
        slug = tenant["slug"]
        if slug in self._chat_agents:
            return self._chat_agents[slug]
        voice_id = self.ensure(tenant, trade)
        want = self.agent_name(tenant["name"]) + " (text)"
        found = self.request(
            "POST",
            "/v2/list-agents?limit=1000",
            {
                "filter_criteria": {
                    "channel": {"type": "string", "op": "eq", "value": "chat"},
                    "query": want,
                }
            },
            self.key,
        )
        items = found.get("items", []) if isinstance(found, dict) else found
        matches = [a for a in items or [] if a.get("agent_name") == want]
        if matches:
            agent_id = max(matches, key=lambda a: a.get("user_modified_timestamp", 0))["agent_id"]
        else:
            voice = self.request(
                "GET", f"/get-agent/{urllib.parse.quote(voice_id)}", None, self.key
            )
            llm_id = ((voice or {}).get("response_engine") or {}).get("llm_id")
            if not llm_id:
                raise RetellError("voice agent has no Retell LLM to share")
            created = self.request(
                "POST",
                "/create-chat-agent",
                {
                    "agent_name": want,
                    "response_engine": {"type": "retell-llm", "llm_id": llm_id},
                },
                self.key,
            )
            agent_id = created["agent_id"]
        self._chat_agents[slug] = agent_id
        return agent_id

    def chat(self, tenant: dict, trade: str, chat_id: str | None, content: str) -> dict:
        """Send one caller message; returns the chat id and the agent's new messages."""
        if not chat_id:
            agent_id = self.chat_agent(tenant, trade)
            chat = self.request(
                "POST",
                "/create-chat",
                {"agent_id": agent_id, "metadata": {"source": "browser_demo"}},
                self.key,
            )
            chat_id = chat["chat_id"]
        res = self.request(
            "POST",
            "/create-chat-completion",
            {"chat_id": chat_id, "content": content},
            self.key,
        )
        out = []
        for m in (res or {}).get("messages", []):
            role = m.get("role")
            if role in ("agent", "user"):
                out.append({"role": role, "content": m.get("content", "")})
            elif role == "tool_call_invocation":
                out.append({"role": "tool", "name": m.get("name"), "args": m.get("arguments")})
            elif role == "tool_call_result":
                out.append({"role": "tool_result", "content": str(m.get("content", ""))[:500]})
        return {"chat_id": chat_id, "messages": out}
