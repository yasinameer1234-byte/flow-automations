"""Web application: login, customer dashboard, admin panel, browser call demo, voice tools.

Run locally: `flow serve` (or `uvicorn flow.web.app:app`). Local/dev only.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from ..core.audit import AuditLog, configure_logging
from ..core.db import Database
from ..deploy import check_required, current_env
from ..revenue.engine import RevenueEngine
from ..revenue.playbooks import PLAYBOOKS
from ..secretary.availability import is_open
from ..secretary.calls import CallError, CallService
from ..secretary.metrics import tenant_metrics
from ..secretary.schema import PACKAGES
from ..secretary.seed import seed
from ..secretary.store import Store
from ..secretary.tools import SecretaryTools, ToolError, format_phone
from ..voice.retell import RetellWebhook, verify_signature
from .auth import SESSION_COOKIE, SessionSigner, hash_password, verify_password

HERE = Path(__file__).parent


@dataclass
class WebContext:
    db: Database
    store: Store
    calls: CallService
    signer: SessionSigner
    clock: object


def _fmt_money(v) -> str:
    return f"${v:,.0f}" if v is not None else "n/a"


def _fmt_phone(v) -> str:
    return format_phone(v) if v and len(v) == 10 else (v or "")


def _fmt_local(v, tz: str) -> str:
    from zoneinfo import ZoneInfo

    d = datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(ZoneInfo(tz))
    return d.strftime("%a %b %d, %I:%M %p").replace(" 0", " ")


def _fmt_dt(v) -> str:
    if not v:
        return ""
    try:
        d = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return v
    return d.strftime("%b %d, %H:%M UTC")


class TurnIn(BaseModel):
    text: str


class SmsIn(BaseModel):
    from_number: str
    text: str


def create_web_app(
    db: Database | None = None,
    *,
    clock=None,
    secret: str | None = None,
    seed_demo: bool = True,
    admin_password: str | None = None,
    owner_password: str | None = None,
) -> FastAPI:
    configure_logging(os.environ.get("FLOW_LOG_LEVEL", "WARNING"), True)
    env = current_env()
    hosted = env != "local"
    if hosted:
        # Fail fast: a hosted instance must never use demo passwords or a random session key.
        check_required(env)
        secret = secret or os.environ["FLOW_SECRET_KEY"]
    if env == "production":
        seed_demo = False  # production holds real client data only, never synthetic tenants
    db = db or Database(os.environ.get("FLOW_DB_PATH", "var/flow.db"))
    clock = clock or (lambda: datetime.now(UTC))
    store = Store(db)
    if seed_demo:
        pw = os.environ.get("FLOW_DEMO_PASSWORD")
        seed(
            db,
            password_hasher=hash_password,
            admin_password=admin_password or pw or "flow-demo-admin",
            owner_password=owner_password or pw or "flow-demo-owner",
            northside_api_key=os.environ.get("FLOW_VOICE_TOOL_KEY") or "demo-northside-key",
            now=clock(),
        )
    secure_cookies = hosted or os.environ.get("FLOW_SECURE_COOKIES") == "1"
    ctx = WebContext(db, store, CallService(db, store, clock=clock), SessionSigner(secret), clock)
    revenue = RevenueEngine(db, store, clock=clock)

    app = FastAPI(title="Flow Automations", version="0.2.0", docs_url="/api/docs")
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters.update(money=_fmt_money, phone=_fmt_phone, dt=_fmt_dt, local=_fmt_local)
    app.state.ctx = ctx
    app.state.revenue = revenue

    # ------------------------------------------------------------------ auth helpers
    def current_user(request: Request) -> dict | None:
        sess = ctx.signer.loads(request.cookies.get(SESSION_COOKIE))
        if not sess:
            return None
        user = db.one(
            "SELECT id, email, name, role, tenant_id FROM users WHERE id = ?", (sess["uid"],)
        )
        if user:
            user["csrf"] = ctx.signer.csrf_token(sess)
        return user

    def require_user(request: Request) -> dict:
        user = current_user(request)
        if not user:
            raise HTTPException(303, headers={"Location": "/login"})
        return user

    def require_admin(user: dict = Depends(require_user)) -> dict:
        if user["role"] != "platform_admin":
            raise HTTPException(403, "platform admins only")
        return user

    def check_csrf(user: dict, token: str) -> None:
        if not hmac.compare_digest(user["csrf"], token or ""):
            raise HTTPException(403, "invalid form token")

    def resolve_tenant(user: dict, requested: str | None) -> str:
        if user["role"] == "platform_admin":
            tid = requested or "t_northside"
            if not store.tenant(tid):
                raise HTTPException(404, "tenant not found")
            return tid
        if requested and requested != user["tenant_id"]:
            raise HTTPException(403, "not your business")  # tenant isolation
        return user["tenant_id"]

    def render(request: Request, name: str, **kw) -> HTMLResponse:
        return templates.TemplateResponse(request, name, kw)

    # ------------------------------------------------------------------ pages
    @app.get("/", include_in_schema=False)
    def home(request: Request):
        user = current_user(request)
        if not user:
            return RedirectResponse("/login", 303)
        return RedirectResponse("/admin" if user["role"] == "platform_admin" else "/app", 303)

    @app.get("/login", response_class=HTMLResponse, include_in_schema=False)
    def login_page(request: Request, error: str | None = None):
        return render(request, "login.html", error=error, user=None)

    @app.post("/login", include_in_schema=False)
    def login(request: Request, email: str = Form(...), password: str = Form(...)):
        user = db.one("SELECT * FROM users WHERE email = ?", (email.strip().lower(),))
        audit = AuditLog(db, user["tenant_id"] or "platform" if user else "platform")
        if not user or not verify_password(password, user["password_hash"]):
            audit.record("auth.login_failed", level="warning", email=email)
            return render(request, "login.html", error="Wrong email or password.", user=None)
        audit.record("auth.login", actor=f"user:{user['email']}")
        resp = RedirectResponse("/", 303)
        resp.set_cookie(
            SESSION_COOKIE,
            ctx.signer.dumps(user["id"]),
            httponly=True,
            samesite="lax",
            secure=secure_cookies,
            max_age=8 * 3600,
        )
        return resp

    @app.post("/logout", include_in_schema=False)
    def logout():
        resp = RedirectResponse("/login", 303)
        resp.delete_cookie(SESSION_COOKIE)
        return resp

    @app.get("/app", response_class=HTMLResponse, include_in_schema=False)
    def dashboard(request: Request, tenant: str | None = None, user: dict = Depends(require_user)):
        tid = resolve_tenant(user, tenant)
        ctx.calls.expire_stale(tid)
        m = tenant_metrics(db, store, tid, now=clock())
        recent = db.query(
            "SELECT * FROM calls WHERE tenant_id = ? ORDER BY started_at DESC LIMIT 8", (tid,)
        )
        tasks = db.query(
            "SELECT * FROM tasks WHERE tenant_id = ? AND status = 'open' ORDER BY id DESC LIMIT 5",
            (tid,),
        )
        upcoming = db.query(
            "SELECT a.*, s.name AS service FROM appointments a JOIN services s ON s.id = "
            "a.service_id WHERE a.tenant_id = ? AND a.status = 'booked' AND a.starts_at >= ? "
            "ORDER BY a.starts_at LIMIT 6",
            (tid, clock().strftime("%Y-%m-%dT%H:%M:%SZ")),
        )
        return render(
            request,
            "dashboard.html",
            user=user,
            tenant=store.tenant(tid),
            m=m,
            recent=recent,
            tasks=tasks,
            upcoming=upcoming,
            tz=store.profile(tid).timezone,
            open_now=is_open(store.profile(tid), clock()),
        )

    @app.get("/app/calls/{call_id}", response_class=HTMLResponse, include_in_schema=False)
    def call_detail(
        request: Request,
        call_id: str,
        tenant: str | None = None,
        user: dict = Depends(require_user),
    ):
        tid = resolve_tenant(user, tenant)
        try:
            call = ctx.calls.get(tid, call_id)
        except CallError as e:
            raise HTTPException(404, "call not found") from e
        return render(
            request,
            "call_detail.html",
            user=user,
            tenant=store.tenant(tid),
            call=call,
            turns=ctx.calls.transcript(tid, call_id),
            audit=AuditLog(db, tid).for_run(call_id),
        )

    @app.get("/app/leads", response_class=HTMLResponse, include_in_schema=False)
    def leads_page(request: Request, tenant: str | None = None, user: dict = Depends(require_user)):
        tid = resolve_tenant(user, tenant)
        leads = db.query(
            "SELECT l.*, s.name AS service FROM leads l LEFT JOIN services s ON s.id = "
            "l.service_id WHERE l.tenant_id = ? ORDER BY l.created_at DESC LIMIT 100",
            (tid,),
        )
        return render(request, "leads.html", user=user, tenant=store.tenant(tid), leads=leads)

    @app.post("/app/leads/{lead_id}/outcome", include_in_schema=False)
    def lead_outcome(
        lead_id: int,
        outcome: str = Form(...),
        value: float | None = Form(None),
        csrf: str = Form(""),
        tenant: str | None = Form(None),
        user: dict = Depends(require_user),
    ):
        check_csrf(user, csrf)
        if user["role"] == "viewer":
            raise HTTPException(403, "viewers can't change leads")
        tid = resolve_tenant(user, tenant)
        if outcome not in ("won", "lost"):
            raise HTTPException(400, "outcome must be won or lost")
        if outcome == "won" and (value is None or value <= 0):
            raise HTTPException(400, "a confirmed job value is required")
        cur = db.execute(
            "UPDATE leads SET status = ?, confirmed_value = ?, updated_at = ? "
            "WHERE id = ? AND tenant_id = ?",
            (outcome, value if outcome == "won" else None, clock().isoformat(), lead_id, tid),
        )
        if cur.rowcount != 1:
            raise HTTPException(404, "lead not found")
        AuditLog(db, tid).record(
            "lead.outcome",
            actor=f"user:{user['email']}",
            lead_id=lead_id,
            outcome=outcome,
            confirmed_value=value,
        )
        suffix = f"?tenant={tid}" if user["role"] == "platform_admin" else ""
        return RedirectResponse(f"/app/leads{suffix}", 303)

    # ------------------------------------------------------------------ revenue engine
    @app.get("/app/revenue", response_class=HTMLResponse, include_in_schema=False)
    def revenue_page(
        request: Request, tenant: str | None = None, user: dict = Depends(require_user)
    ):
        tid = resolve_tenant(user, tenant)
        revenue.tick(tid)  # catch up on anything due before showing the page
        enrollments = db.query(
            "SELECT * FROM enrollments WHERE tenant_id = ? ORDER BY updated_at DESC LIMIT 25",
            (tid,),
        )
        outbox = db.query(
            "SELECT m.* FROM messages m WHERE m.tenant_id = ? AND m.purpose = 'follow_up' "
            "ORDER BY m.id DESC LIMIT 15",
            (tid,),
        )
        return render(
            request,
            "revenue.html",
            user=user,
            tenant=store.tenant(tid),
            included=revenue.included(tid),
            enabled=store.automation_enabled(tid, "revenue_engine"),
            playbooks=revenue.playbook_status(tid),
            r=revenue.results(tid),
            enrollments=enrollments,
            outbox=outbox,
            settings=revenue.settings(tid),
            labels={k: p.title for k, p in PLAYBOOKS.items()},
            tz=store.profile(tid).timezone,
        )

    @app.post("/app/revenue/playbooks/{key}", include_in_schema=False)
    def revenue_playbook(
        key: str,
        action: str = Form(...),
        csrf: str = Form(""),
        tenant: str | None = Form(None),
        user: dict = Depends(require_user),
    ):
        check_csrf(user, csrf)
        if user["role"] not in ("owner", "platform_admin"):
            raise HTTPException(403, "only the business owner can approve messages")
        tid = resolve_tenant(user, tenant)
        if key not in PLAYBOOKS or action not in ("approve", "pause"):
            raise HTTPException(404, "unknown playbook")
        if not revenue.included(tid):
            raise HTTPException(403, "the Revenue Engine is not in this package")
        actor = f"user:{user['email']}"
        if action == "approve":
            revenue.approve(tid, key, actor)
            revenue.tick(tid)  # enrol matching people right away; sends follow their delays
        else:
            revenue.revoke(tid, key, actor)
        suffix = f"?tenant={tid}" if user["role"] == "platform_admin" else ""
        return RedirectResponse(f"/app/revenue{suffix}", 303)

    @app.post("/api/demo/{slug}/sms")
    def demo_inbound_sms(slug: str, body: SmsIn):
        """Simulated inbound text (a customer replying to a follow-up)."""
        return revenue.inbound_sms(_demo_tenant(slug), body.from_number, body.text)

    @app.get("/admin", response_class=HTMLResponse, include_in_schema=False)
    def admin(request: Request, user: dict = Depends(require_admin)):
        rows = []
        for t in store.tenants():
            m = tenant_metrics(db, store, t["id"], now=clock())
            fails = db.one(
                "SELECT COUNT(*) AS n FROM audit_log WHERE tenant_id = ? AND level = 'error'",
                (t["id"],),
            )["n"]
            rows.append(
                {"t": t, "m": m, "failures": fails, "automations": store.automations(t["id"])}
            )
        errors = db.query(
            "SELECT tenant_id, run_id, ts, event, detail_json FROM audit_log "
            "WHERE level IN ('error', 'warning') ORDER BY id DESC LIMIT 20"
        )
        mrr = sum(
            PACKAGES[r["t"]["package"]]["monthly_cad"] for r in rows if r["t"]["status"] == "active"
        )
        return render(request, "admin.html", user=user, rows=rows, errors=errors, mrr=mrr)

    @app.post("/admin/tenants/{tenant_id}/automations/{key}", include_in_schema=False)
    def toggle_automation(
        tenant_id: str,
        key: str,
        enabled: str = Form(...),
        csrf: str = Form(""),
        user: dict = Depends(require_admin),
    ):
        check_csrf(user, csrf)
        if not store.tenant(tenant_id) or key not in (
            "ai_secretary",
            "booking_confirmations",
            "revenue_engine",
        ):
            raise HTTPException(404, "unknown automation")
        store.set_automation(tenant_id, key, enabled == "1", user["email"])
        AuditLog(db, tenant_id).record(
            "automation.toggled", actor=f"user:{user['email']}", key=key, enabled=enabled == "1"
        )
        return RedirectResponse("/admin", 303)

    # ------------------------------------------------------------------ browser call demo
    @app.get("/demo/{slug}", response_class=HTMLResponse, include_in_schema=False)
    def demo_page(request: Request, slug: str):
        t = store.tenant_by_slug(slug)
        if not t:
            raise HTTPException(404, "unknown business")
        return render(request, "call.html", tenant=t, user=current_user(request))

    def _demo_tenant(slug: str) -> str:
        t = store.tenant_by_slug(slug)
        if not t:
            raise HTTPException(404, "unknown business")
        return t["id"]

    @app.post("/api/demo/{slug}/calls")
    def demo_start(slug: str):
        try:
            return ctx.calls.start(_demo_tenant(slug), channel="browser_demo")
        except CallError as e:
            raise HTTPException(409, str(e)) from e

    @app.post("/api/demo/{slug}/calls/{call_id}/turn")
    def demo_turn(slug: str, call_id: str, body: TurnIn):
        try:
            return ctx.calls.turn(_demo_tenant(slug), call_id, body.text)
        except CallError as e:
            raise HTTPException(409, str(e)) from e

    @app.post("/api/demo/{slug}/calls/{call_id}/end")
    def demo_end(slug: str, call_id: str):
        try:
            c = ctx.calls.end(_demo_tenant(slug), call_id)
        except CallError as e:
            raise HTTPException(404, str(e)) from e
        return {
            "call_id": c["id"],
            "outcome": c["outcome"],
            "summary": c["summary"],
            "billed_minutes": c["billed_minutes"],
        }

    # ------------------------------------------------------------------ voice agent tools
    # Shaped for Retell custom functions: POST {"name", "args", "call"}. Auth by tenant key.
    def voice_tenant(authorization: str | None = Header(None)) -> str:
        key = (authorization or "").removeprefix("Bearer ").strip()
        h = hashlib.sha256(key.encode()).hexdigest()
        t = db.one("SELECT id FROM tenants WHERE api_key_hash = ? AND status = 'active'", (h,))
        if not key or not t:
            raise HTTPException(401, "invalid tenant key")
        return t["id"]

    retell_key = os.environ.get("RETELL_API_KEY", "")
    webhook = RetellWebhook(db, store, clock=clock)
    audit = AuditLog(db, "platform")

    async def retell_body(request: Request) -> dict:
        """Read the body; when a Retell key is configured, require Retell's signature."""
        raw = await request.body()
        if retell_key and not verify_signature(
            raw, request.headers.get("x-retell-signature"), retell_key
        ):
            audit.record("voice.bad_signature", level="warning", path=request.url.path)
            raise HTTPException(401, "invalid signature")
        try:
            return json.loads(raw or b"{}")
        except ValueError as e:
            raise HTTPException(400, "invalid JSON") from e

    @app.post("/voice/webhooks/retell/{slug}", status_code=204)
    async def retell_webhook(slug: str, request: Request):
        if not retell_key:
            raise HTTPException(503, "RETELL_API_KEY is not configured")
        payload = await retell_body(request)
        tenant = store.tenant_by_slug(slug)
        if not tenant:
            raise HTTPException(404, "unknown business")
        try:
            result = webhook.handle(tenant["id"], payload)
        except PermissionError as e:
            raise HTTPException(403, str(e)) from e
        audit.record("voice.webhook", retell_event=payload.get("event"), result=result)
        return Response(status_code=204)

    @app.post("/voice/{slug}/tools/{tool}")
    async def retell_tool(slug: str, tool: str, request: Request):
        """Retell-native route: the business comes from the URL, trust from Retell's signature."""
        if not retell_key:
            raise HTTPException(503, "RETELL_API_KEY is not configured")
        body = await retell_body(request)
        tenant = store.tenant_by_slug(slug)
        if not tenant or tenant["status"] != "active":
            raise HTTPException(404, "unknown business")
        return run_tool(tenant["id"], tool, body)

    @app.post("/voice/tools/{tool}")
    async def voice_tool(tool: str, request: Request, tenant_id: str = Depends(voice_tenant)):
        body = await retell_body(request)
        return run_tool(tenant_id, tool, body)

    def run_tool(tenant_id: str, tool: str, body: dict):
        args = body.get("args", {})
        call_ref = (body.get("call") or {}).get("call_id")
        tools = SecretaryTools(db, store, tenant_id, clock=clock)
        try:
            if tool == "check_availability":
                from datetime import date

                on = date.fromisoformat(args["date"]) if args.get("date") else None
                return tools.check_availability(
                    args.get("service_id"),
                    on_date=on,
                    part_of_day=args.get("part_of_day"),
                    call_id=call_ref,
                )
            if tool == "book_appointment":
                booked = tools.book_appointment(
                    args["slot_id"],
                    args["name"],
                    args["phone"],
                    int(args["service_id"]),
                    call_id=call_ref,
                )
                # Same as the browser agent: every booking is also a qualified lead,
                # and the caller gets a (simulated) transactional confirmation.
                lead_id = tools.save_lead(
                    name=args["name"],
                    phone=args["phone"],
                    reason=args.get("reason"),
                    service_id=int(args["service_id"]),
                    status="booked",
                    qualified=True,
                    call_id=call_ref,
                )
                db.execute(
                    "UPDATE appointments SET lead_id = ? WHERE id = ? AND tenant_id = ?",
                    (lead_id, booked["appointment_id"], tenant_id),
                )
                tools.send_confirmation(booked["appointment_id"], call_id=call_ref)
                return {**booked, "lead_id": lead_id}
            if tool == "answer_question":
                return tools.answer_question(args["question"], call_id=call_ref)
            if tool == "transfer_call":
                return tools.transfer_call(
                    args.get("reason", ""), bool(args.get("emergency")), call_id=call_ref
                )
            if tool == "business_info":
                from zoneinfo import ZoneInfo

                prof = store.profile(tenant_id)
                now = clock()
                return {
                    "business_name": store.tenant(tenant_id)["name"],
                    "timezone": prof.timezone,
                    "local_now": now.astimezone(ZoneInfo(prof.timezone)).strftime(
                        "%A, %B %-d %Y, %-I:%M %p"
                    ),
                    "open_now": is_open(prof, now),
                    "hours": prof.hours,
                    "emergency_instructions": prof.emergency_instructions,
                }
            if tool == "list_services":
                return {
                    "services": [
                        {
                            "service_id": s["id"],
                            "name": s["name"],
                            "duration_minutes": s["duration_minutes"],
                            "bookable": bool(s["bookable"]),
                        }
                        for s in store.services(tenant_id)
                    ]
                }
            if tool == "request_callback":
                lead_id = tools.save_lead(
                    name=args.get("name"),
                    phone=args["phone"],
                    reason=args.get("reason"),
                    service_id=args.get("service_id"),
                    status="callback",
                    qualified=bool(args.get("service_id")),
                    call_id=call_ref,
                )
                urgent = bool(args.get("urgent"))
                task_id = tools.create_task(
                    "emergency_callback" if urgent else "callback",
                    f"Call back {args.get('name') or 'caller'}: "
                    f"{args.get('reason') or 'no reason given'}",
                    lead_id,
                    call_id=call_ref,
                )
                return {"lead_id": lead_id, "task_id": task_id, "urgent": urgent}
            if tool == "save_lead":
                lead_id = tools.save_lead(
                    name=args.get("name"),
                    phone=args.get("phone"),
                    reason=args.get("reason"),
                    service_id=args.get("service_id"),
                    status="new",
                    qualified=bool(args.get("qualified")),
                    call_id=call_ref,
                )
                return {"lead_id": lead_id}
        except ToolError as e:
            return JSONResponse({"error": e.code, "message": str(e)}, status_code=422)
        except KeyError as e:
            return JSONResponse({"error": "missing_arg", "message": str(e)}, status_code=422)
        raise HTTPException(404, "unknown tool")

    @app.get("/health")
    def health():
        return {
            "status": "ok",
            "tenants": len(store.tenants()),
            "voice": "browser demo + Retell phone" if retell_key else "browser demo only",
            "messaging": "simulated",
        }

    app.state.json = json
    return app


def __getattr__(name: str):  # `uvicorn flow.web.app:app`
    if name == "app":
        return create_web_app()
    raise AttributeError(name)
