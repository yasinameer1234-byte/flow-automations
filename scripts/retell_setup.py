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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from flow.voice.setup import TRADES, agent_payload, llm_payload, post  # noqa: E402


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
