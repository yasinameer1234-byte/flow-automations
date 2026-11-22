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
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from ..core.audit import AuditLog, configure_logging
from ..core.db import Database
from ..secretary.availability import is_open
from ..secretary.calls import CallError, CallService
from ..secretary.metrics import tenant_metrics
from ..secretary.schema import PACKAGES
from ..secretary.seed import seed
from ..secretary.store import Store
from ..secretary.tools import SecretaryTools, ToolError, format_phone
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
            now=clock(),
        )
    ctx = WebContext(db, store, CallService(db, store, clock=clock), SessionSigner(secret), clock)

    app = FastAPI(title="Flow Automations", version="0.2.0", docs_url="/api/docs")
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters.update(money=_fmt_money, phone=_fmt_phone, dt=_fmt_dt, local=_fmt_local)
    app.state.ctx = ctx

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
            secure=os.environ.get("FLOW_SECURE_COOKIES") == "1",
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
        if not store.tenant(tenant_id) or key not in ("ai_secretary", "booking_confirmations"):
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

    @app.post("/voice/tools/{tool}")
    async def voice_tool(tool: str, request: Request, tenant_id: str = Depends(voice_tenant)):
        body = await request.json()
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
                return tools.book_appointment(
                    args["slot_id"],
                    args["name"],
                    args["phone"],
                    int(args["service_id"]),
                    call_id=call_ref,
                )
            if tool == "answer_question":
                return tools.answer_question(args["question"], call_id=call_ref)
            if tool == "transfer_call":
                return tools.transfer_call(
                    args.get("reason", ""), bool(args.get("emergency")), call_id=call_ref
                )
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
            "voice": "browser demo (simulated)",
            "messaging": "simulated",
        }

    app.state.json = json
    return app


def __getattr__(name: str):  # `uvicorn flow.web.app:app`
    if name == "app":
        return create_web_app()
    raise AttributeError(name)
