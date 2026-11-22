"""Command line: `flow demo`, `flow approvals`, `flow approve <id>`, `flow reject <id>`."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .app import build_app
from .core.approvals import ApprovalError
from .core.models import InboundMessage

DEFAULT_DATA = Path(__file__).resolve().parents[2] / "data" / "synthetic" / "inbound.jsonl"


def _load(path: Path) -> list[InboundMessage]:
    return [
        InboundMessage.model_validate_json(line)
        for line in path.read_text().splitlines()
        if line.strip()
    ]


def cmd_demo(args) -> int:
    flow = build_app()
    messages = _load(Path(args.data))
    print(
        f"Processing {len(messages)} synthetic messages with classifier "
        f"'{flow.triage.classifier.name}'\n"
    )
    for m in messages:
        r = flow.process(m)
        cat = r.category.value if r.category else "-"
        conf = f"{r.confidence:.2f}" if r.confidence is not None else "-"
        print(f"  [{r.status.value:<17}] {cat:<16} conf={conf}  {r.summary}")
        for a in r.approvals:
            print(f"      -> approval needed: {a}")
    pending = flow.engine.approvals.pending()
    print(f"\nCRM now holds: {flow.crm.summary()}")
    print(f"Pending approvals: {len(pending)}  (run `flow approvals` to review)")
    if args.auto_approve:
        for p in pending:
            out = flow.engine.decide(
                p["id"],
                approve=True,
                decided_by="demo-operator",
                note="auto-approved by --auto-approve",
            )
            print(f"  approved {p['id']} ({p['action']}): {out['status']}")
        print(f"Simulated outbox now holds {len(flow.email.outbox())} email(s).")
    return 0


def cmd_approvals(args) -> int:
    flow = build_app()
    for p in flow.engine.approvals.pending():
        print(json.dumps(p, indent=2))
    return 0


def cmd_decide(args, approve: bool) -> int:
    flow = build_app()
    try:
        out = flow.engine.decide(
            args.approval_id, approve=approve, decided_by=args.by, note=args.note
        )
    except ApprovalError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="flow")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo", help="run the inbound triage demo on synthetic data")
    d.add_argument("--data", default=str(DEFAULT_DATA))
    d.add_argument(
        "--auto-approve", action="store_true", help="approve every pending action (demo only)"
    )
    sub.add_parser("approvals", help="list pending approvals")
    for name in ("approve", "reject"):
        s = sub.add_parser(name)
        s.add_argument("approval_id")
        s.add_argument("--by", required=True, help="who is deciding")
        s.add_argument("--note", default="")
    args = p.parse_args(argv)
    if args.cmd == "demo":
        return cmd_demo(args)
    if args.cmd == "approvals":
        return cmd_approvals(args)
    return cmd_decide(args, approve=args.cmd == "approve")


if __name__ == "__main__":
    raise SystemExit(main())
