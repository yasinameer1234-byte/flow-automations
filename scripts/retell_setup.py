#!/usr/bin/env python3
"""Create (or preview) the Retell phone agent for one business.

    # preview the exact payloads, no network, no key needed
    python scripts/retell_setup.py --base-url https://flow-automations-demo.onrender.com \
        --slug northside --trade plumbing --dry-run

    # create it (needs RETELL_API_KEY in the environment, never on the command line)
    python scripts/retell_setup.py --base-url https://... --slug northside --trade plumbing

Creating the LLM and agent is free. It does NOT buy a phone number: test with Retell's
web call (dashboard > the agent > Test, or --web-call). A number costs money and is a
separate, approved step.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

API = "https://api.retellai.com"
ROOT = Path(__file__).resolve().parent.parent

TRADES = {
    "plumbing": {
        "label": "plumbing and heating",
        "signs": [
            "water actively leaking, flooding or a burst pipe",
            "sewage backing up into the home",
            "a smell of gas (tell them to leave the building and call the gas utility or 911 "
            "from outside first)",
            "no heat in freezing weather with vulnerable people at home",
        ],
        "notes": "Common jobs: leaks, clogged drains, toilets, water heaters, sump pumps, "
        "furnace and boiler repair. Ask whether water is still running and whether they "
        "know where the main shut-off valve is.",
    },
    "hvac": {
        "label": "heating and air conditioning",
        "signs": [
            "a smell of gas or a carbon monoxide alarm sounding (tell them to leave the "
            "building and call 911 from outside first)",
            "no heat in freezing weather",
            "no cooling during extreme heat with elderly people, infants or medical needs",
        ],
        "notes": "Common jobs: furnace not igniting, no heat, AC not cooling, thermostat "
        "issues, maintenance tune-ups, new system quotes. Ask the system type and age if "
        "the caller knows it, but do not diagnose.",
    },
    "electrical": {
        "label": "electrical",
        "signs": [
            "sparks, smoke, burning smell or scorch marks at an outlet or panel (tell them "
            "to stay away and call 911 if there is fire)",
            "a downed power line (tell them to stay 10 metres away and call 911 and the utility)",
            "total loss of power that the utility says is not an outage",
            "anyone who has had an electric shock (tell them to call 911)",
        ],
        "notes": "Common jobs: breakers tripping, outlets or lights not working, panel "
        "upgrades, EV charger installs, pot lights. Never tell a caller to open a panel "
        "or do electrical work themselves.",
    },
}


def _params(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required}


def tools(base: str, slug: str) -> list[dict]:
    def custom(name, desc, params, speak=False, msg=""):
        t = {
            "type": "custom",
            "name": name,
            "description": desc,
            "url": f"{base}/voice/{slug}/tools/{name}",
            "method": "POST",
            "parameters": params,
            "speak_during_execution": speak,
            "speak_after_execution": True,
            "timeout_ms": 10000,
        }
        if speak:
            t["execution_message_description"] = msg
        return t

    s, i, b = {"type": "string"}, {"type": "integer"}, {"type": "boolean"}
    return [
        custom(
            "business_info",
            "Today's date, local time, open now, hours, emergency "
            "instructions. Call once at the start of every call.",
            _params({}, []),
        ),
        custom(
            "list_services", "The services this business offers, with service_id.", _params({}, [])
        ),
        custom(
            "check_availability",
            "Real open appointment times for a service. The only source of times you may offer.",
            _params(
                {
                    "service_id": i,
                    "date": {"type": "string", "description": "YYYY-MM-DD, optional"},
                    "part_of_day": {"type": "string", "enum": ["morning", "afternoon", "evening"]},
                },
                ["service_id"],
            ),
            True,
            "Let me check the schedule.",
        ),
        custom(
            "book_appointment",
            "Book the exact slot_id the caller chose.",
            _params(
                {
                    "slot_id": s,
                    "name": s,
                    "phone": s,
                    "service_id": i,
                    "reason": {"type": "string", "description": "caller's words"},
                },
                ["slot_id", "name", "phone", "service_id"],
            ),
            True,
            "One moment while I book that.",
        ),
        custom(
            "answer_question",
            "Approved answers about the business (fees, areas, "
            "policies). Repeat only what it returns.",
            _params({"question": s}, ["question"]),
        ),
        custom(
            "transfer_call",
            "Find an available person to take the call. Use emergency=true for emergencies.",
            _params({"reason": s, "emergency": b}, ["reason"]),
        ),
        custom(
            "request_callback",
            "Take a message so staff call back. urgent=true for emergencies nobody could take.",
            _params(
                {"name": s, "phone": s, "reason": s, "service_id": i, "urgent": b},
                ["phone", "reason"],
            ),
        ),
        {
            "type": "end_call",
            "name": "end_call",
            "description": "End the call after saying goodbye.",
        },
    ]


def llm_payload(base: str, slug: str, trade: str, business: str, area: str) -> dict:
    t = TRADES[trade]
    prompt = (
        (ROOT / "retell" / "receptionist_prompt.md")
        .read_text()
        .format(
            business_name=business,
            trade_label=t["label"],
            service_area=area,
            emergency_signs="\n".join(f"   - {x}" for x in t["signs"]),
            trade_notes=t["notes"],
        )
    )
    return {
        "model": "claude-5.5-haiku",
        "start_speaker": "agent",
        "begin_message": f"Thanks for calling {business}. This call is answered by an AI "
        "assistant. How can I help you today?",
        "general_prompt": prompt,
        "general_tools": tools(base, slug),
    }


def agent_payload(base: str, slug: str, business: str, llm_id: str) -> dict:
    return {
        "agent_name": f"{business} receptionist",
        "response_engine": {"type": "retell-llm", "llm_id": llm_id},
        "voice_id": "retell-Cimo",
        "language": "en-US",  # Retell has no en-CA; en-US handles Canadian callers
        "webhook_url": f"{base}/voice/webhooks/retell/{slug}",
        "webhook_events": ["call_started", "call_ended", "call_analyzed"],
        # Caps what one call can spend from the credit (a stuck or abusive web call).
        "max_call_duration_ms": 10 * 60 * 1000,
    }


def post(path: str, body: dict, key: str) -> dict:
    req = urllib.request.Request(  # noqa: S310 (fixed https host)
        API + path,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 (fixed https host)
        return json.loads(r.read())


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", required=True, help="public https URL of the Flow app")
    p.add_argument("--slug", default="northside")
    p.add_argument("--trade", choices=sorted(TRADES), default="plumbing")
    p.add_argument("--business", default="Northside Plumbing & Heating")
    p.add_argument("--area", default="Toronto and the GTA")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--web-call", action="store_true", help="also create a test web call")
    a = p.parse_args()
    base = a.base_url.rstrip("/")
    if not base.startswith("https://"):
        print("error: --base-url must be https", file=sys.stderr)
        return 1
    llm = llm_payload(base, a.slug, a.trade, a.business, a.area)
    if a.dry_run:
        print(
            json.dumps(
                {"llm": llm, "agent": agent_payload(base, a.slug, a.business, "<llm_id>")}, indent=2
            )
        )
        return 0
    key = os.environ.get("RETELL_API_KEY")
    if not key:
        print(
            "error: set RETELL_API_KEY in the environment (never pass it as an argument)",
            file=sys.stderr,
        )
        return 1
    llm_id = post("/create-retell-llm", llm, key)["llm_id"]
    agent = post("/create-agent", agent_payload(base, a.slug, a.business, llm_id), key)
    print(f"Created LLM {llm_id} and agent {agent['agent_id']} for {a.business}")
    if a.web_call:
        call = post("/v3/create-web-call", {"agent_id": agent["agent_id"]}, key)
        print(f"Web call {call['call_id']} ready (expires {call.get('expires_at')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
