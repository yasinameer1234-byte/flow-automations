"""Command line: `flow demo`, `flow approvals`, `flow approve <id>`, `flow reject <id>`."""

from __future__ import annotations

import argparse
import json
import os
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


def _start_revenue_loop(engine, every_seconds: int = 60) -> None:
    """Run the Revenue Engine for every business once a minute, in the background."""
    import threading
    import time

    def loop() -> None:
        while True:
            try:
                engine.tick_all()
            except Exception as e:  # keep the loop alive; the error is logged, not swallowed
                print(f"revenue tick failed: {e}", file=sys.stderr)
            time.sleep(every_seconds)

    if os.environ.get("FLOW_REVENUE_LOOP", "1") != "0":
        threading.Thread(target=loop, name="revenue-engine", daemon=True).start()


def cmd_revenue_tick(args) -> int:
    from .core.db import Database
    from .revenue.engine import RevenueEngine
    from .secretary.store import Store

    db = Database(os.environ.get("FLOW_DB_PATH", "var/flow.db"))
    print(json.dumps(RevenueEngine(db, Store(db)).tick_all(), indent=2))
    return 0


def cmd_create_tenant(args) -> int:
    """Create a real client business from a profile file. Owner password is prompted."""
    import getpass

    from pydantic import ValidationError

    from .core.db import Database
    from .onboarding import OnboardingError, create_tenant, load_profile
    from .web.auth import hash_password

    try:
        profile = load_profile(args.profile)
    except (ValidationError, OSError, ValueError) as e:
        print(f"error: the profile is not valid:\n{e}", file=sys.stderr)
        return 1
    if args.check:
        print(f"OK: {profile.business.name} ({profile.business.slug}) is valid.")
        return 0
    pw = getpass.getpass(f"Password for {profile.owner.email} (min 12 characters): ")
    if len(pw) < 12 or pw != getpass.getpass("Repeat password: "):
        print("error: passwords must match and be at least 12 characters", file=sys.stderr)
        return 1
    db = Database(os.environ.get("FLOW_DB_PATH", "var/flow.db"))
    try:
        out = create_tenant(db, profile, owner_password_hash=hash_password(pw))
    except OnboardingError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"Created {profile.business.name} as {out['tenant_id']} (slug {out['slug']}).")
    print(f"Owner login: {profile.owner.email}")
    print(
        "Retell phone agent: python scripts/retell_setup.py --base-url <app URL> "
        f'--slug {out["slug"]} --business "{profile.business.name}"'
    )
    return 0


def cmd_check_config(args) -> int:
    from .deploy import ConfigError, check_required, current_env, secret_status

    try:
        env = args.env or current_env()
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"Environment: {env}  (values are never shown)")
    for row in secret_status(env):
        if row["valid"]:
            state = "ok"
        elif row["set"]:
            state = f"TOO SHORT (min {row['min_length']})"
        else:
            state = "MISSING" if row["required"] else "not set (optional here)"
        print(f"  {row['name']:<22} {state:<26} {row['purpose']}")
    try:
        check_required(env)
    except ConfigError as e:
        print(f"\nNot ready: {e}", file=sys.stderr)
        return 1
    print("\nReady: every required secret is set.")
    return 0


def cmd_create_admin(args) -> int:
    """Create a platform admin. The password is typed at a hidden prompt, never an argument."""
    import getpass

    from .core.db import Database
    from .core.models import utcnow
    from .secretary.store import Store
    from .web.auth import hash_password

    db = Database(os.environ.get("FLOW_DB_PATH", "var/flow.db"))
    Store(db)  # creates the platform tables if needed
    if db.one("SELECT id FROM users WHERE email = ?", (args.email,)):
        print(f"error: {args.email} already exists", file=sys.stderr)
        return 1
    pw = getpass.getpass("New admin password (min 12 characters): ")
    if len(pw) < 12 or pw != getpass.getpass("Repeat password: "):
        print("error: passwords must match and be at least 12 characters", file=sys.stderr)
        return 1
    db.execute(
        "INSERT INTO users (email, name, password_hash, role, tenant_id, created_at) "
        "VALUES (?, ?, ?, 'platform_admin', NULL, ?)",
        (args.email, args.name, hash_password(pw), utcnow()),
    )
    print(f"Created platform admin {args.email}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="flow")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo", help="run the inbound triage demo on synthetic data")
    d.add_argument("--data", default=str(DEFAULT_DATA))
    d.add_argument(
        "--auto-approve", action="store_true", help="approve every pending action (demo only)"
    )
    sv = sub.add_parser("serve", help="run the web app (dashboard, admin, browser call demo)")
    sv.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    sv.add_argument("--host", default=os.environ.get("FLOW_HOST", "127.0.0.1"))
    cc = sub.add_parser("check-config", help="show which deployment secrets are set (no values)")
    cc.add_argument("--env", choices=["local", "staging", "production"])
    ca = sub.add_parser("create-admin", help="create a platform admin (password prompted)")
    ca.add_argument("email")
    ca.add_argument("--name", default="Platform Admin")
    ct = sub.add_parser("create-tenant", help="create a real client business from a profile file")
    ct.add_argument("profile", help="path to a client profile .toml")
    ct.add_argument("--check", action="store_true", help="validate the file only")
    sub.add_parser("revenue-tick", help="run the Revenue Engine once for every business")
    sub.add_parser("approvals", help="list pending approvals")
    for name in ("approve", "reject"):
        s = sub.add_parser(name)
        s.add_argument("approval_id")
        s.add_argument("--by", required=True, help="who is deciding")
        s.add_argument("--note", default="")
    args = p.parse_args(argv)
    if args.cmd == "demo":
        return cmd_demo(args)
    if args.cmd == "serve":
        import uvicorn

        from .deploy import ConfigError
        from .web.app import create_web_app

        try:
            web = create_web_app()
        except ConfigError as e:
            print(f"error: {e}\nRun `flow check-config` to see what is missing.", file=sys.stderr)
            return 1
        _start_revenue_loop(web.state.revenue)
        # Behind a hosting proxy (Render), trust its X-Forwarded-For so rate limits see the
        # real client address. Locally only 127.0.0.1 is trusted.
        uvicorn.run(
            web,
            host=args.host,
            port=args.port,
            proxy_headers=True,
            forwarded_allow_ips=os.environ.get("FLOW_TRUSTED_PROXIES", "127.0.0.1"),
        )
        return 0
    if args.cmd == "create-tenant":
        return cmd_create_tenant(args)
    if args.cmd == "revenue-tick":
        return cmd_revenue_tick(args)
    if args.cmd == "check-config":
        return cmd_check_config(args)
    if args.cmd == "create-admin":
        return cmd_create_admin(args)
    if args.cmd == "approvals":
        return cmd_approvals(args)
    return cmd_decide(args, approve=args.cmd == "approve")


if __name__ == "__main__":
    raise SystemExit(main())
