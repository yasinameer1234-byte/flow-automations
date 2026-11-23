"""Web application: login, customer dashboard, admin panel, browser call demo, voice tools.

Run locally: `flow serve` (or `uvicorn flow.web.app:app`). Local/dev only.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
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
from ..voice.agent import VoiceAgents
from ..voice.retell import RetellWebhook, verify_signature
from .auth import SESSION_COOKIE, SessionSigner, hash_password, verify_password
from .ratelimit import RateLimiter

HERE = Path(__file__).parent


@dataclass
class WebContext:
    db: Database
    store: Store
    calls: CallService
    signer: SessionSigner
    clock: object


# Browser-demo calls older than a cutoff, children first. Each takes (tenant_id, cutoff).
_PURGE_DEMO_SQL = (
    "DELETE FROM appointments WHERE source_call_id IN (SELECT id FROM calls WHERE "
    "tenant_id = ? AND channel IN ('browser_demo', 'voice_web') AND started_at < ?)",
    "DELETE FROM tasks WHERE lead_id IN (SELECT id FROM leads WHERE source_call_id IN "
    "(SELECT id FROM calls WHERE tenant_id = ? AND channel IN ('browser_demo', 'voice_web') "
    "AND started_at < ?))",
    "DELETE FROM leads WHERE source_call_id IN (SELECT id FROM calls WHERE "
    "tenant_id = ? AND channel IN ('browser_demo', 'voice_web') AND started_at < ?)",
    "DELETE FROM call_turns WHERE call_id IN (SELECT id FROM calls WHERE "
    "tenant_id = ? AND channel IN ('browser_demo', 'voice_web') AND started_at < ?)",
    "DELETE FROM calls WHERE tenant_id = ? AND channel IN ('browser_demo', 'voice_web') "
    "AND started_at < ?",
)


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

    # ------------------------------------------------------------------ abuse limits
    login_email_limit = RateLimiter(5, 15 * 60)  # failed logins per account
    login_ip_limit = RateLimiter(20, 15 * 60)  # failed logins per address
    demo_call_limit = RateLimiter(10, 3600)  # demo calls per address per hour
    demo_turn_limit = RateLimiter(120, 600)  # demo messages per address per 10 minutes
    demo_sms_limit = RateLimiter(20, 3600)
    demo_daily_limit = RateLimiter(int(os.environ.get("FLOW_DEMO_CALLS_PER_DAY", "300")), 86400)
    # Real voice calls spend Retell credit, so they get their own, tighter limits.
    demo_voice_limit = RateLimiter(6, 3600)  # voice calls per address per hour
    demo_voice_daily = RateLimiter(int(os.environ.get("FLOW_VOICE_CALLS_PER_DAY", "40")), 86400)
    app.state.limits = (
        login_email_limit,
        login_ip_limit,
        demo_call_limit,
        demo_daily_limit,
        demo_voice_limit,
        demo_voice_daily,
    )

    # ------------------------------------------------------------------ Retell voice agents
    retell_key = os.environ.get("RETELL_API_KEY", "")

    def public_base() -> str:
        return (
            os.environ.get("FLOW_PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL") or ""
        ).rstrip("/")

    voice_agents = VoiceAgents(db, retell_key, public_base)
    app.state.voice_agents = voice_agents

    def warm_voice_agents() -> None:
        # After a restart the free tier's disk is empty: find (or make) each demo agent now,
        # so the first prospect doesn't wait for it.
        for t in store.tenants():
            trade = VoiceAgents.trade_for(t)
            if t["is_demo"] and trade:
                try:
                    voice_agents.ensure(t, trade)
                except Exception as e:  # noqa: BLE001 (status is shown on /health)
                    logging.getLogger("flow.voice").warning(
                        "voice agent warm-up failed for %s: %s", t["slug"], e
                    )

    if hosted and retell_key:
        threading.Thread(target=warm_voice_agents, name="voice-warmup", daemon=True).start()

    # Behind a proxy (Render), the caller's address is the entry that the LAST trusted proxy
    # appended to X-Forwarded-For: FLOW_PROXY_HOPS entries from the right. The left-most
    # entry is whatever the caller typed, so trusting it would let anyone dodge the limits.
    proxy_hops = int(os.environ.get("FLOW_PROXY_HOPS", "0"))

    # Some hosts put the verified caller address in a header of its own that callers can't
    # set (Cloudflare's CF-Connecting-IP). When configured, that wins.
    client_ip_header = os.environ.get("FLOW_CLIENT_IP_HEADER", "").strip().lower()

    def client_ip(request: Request) -> str:
        if client_ip_header and request.headers.get(client_ip_header):
            return request.headers[client_ip_header].split(",")[0].strip()
        if proxy_hops > 0:
            hops = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",")]
            hops = [h for h in hops if h]
            if len(hops) >= proxy_hops:
                return hops[-proxy_hops]
        peer = request.scope.get("client")
        return peer[0] if peer else "unknown"

    @app.get("/whoami", include_in_schema=False)
    def whoami(request: Request):
        """Echo back only the caller's own address headers, to set FLOW_PROXY_HOPS correctly."""
        h = request.headers
        return {
            "seen_as": client_ip(request),
            "x_forwarded_for": h.get("x-forwarded-for"),
            "cf_connecting_ip": h.get("cf-connecting-ip"),
            "true_client_ip": h.get("true-client-ip"),
            "x_real_ip": h.get("x-real-ip"),
            "proxy_hops": proxy_hops,
            "client_ip_header": client_ip_header or None,
        }

    # ------------------------------------------------------------------ demo access
    # "public": anyone with the URL can try the demo business (local development).
    # "invite": hosted default. The page needs a signed, expiring link made in /admin, or a
    # signed-in admin / owner of that demo business. Real clients never have a demo page.
    demo_access = os.environ.get("FLOW_DEMO_ACCESS") or ("public" if env == "local" else "invite")
    demo_retention_hours = float(os.environ.get("FLOW_DEMO_RETENTION_HOURS", "24"))
    DEMO_COOKIE = "flow_demo"

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
        email = email.strip().lower()
        ip = client_ip(request)
        if login_email_limit.blocked(email) or login_ip_limit.blocked(ip):
            AuditLog(db, "platform").record("auth.login_throttled", level="warning", email=email)
            resp = render(
                request,
                "login.html",
                error="Too many attempts. Please wait 15 minutes and try again.",
                user=None,
            )
            resp.status_code = 429
            return resp
        user = db.one("SELECT * FROM users WHERE email = ?", (email,))
        audit = AuditLog(db, user["tenant_id"] or "platform" if user else "platform")
        if not user or not verify_password(password, user["password_hash"]):
            login_email_limit.hit(email)
            login_ip_limit.hit(ip)
            audit.record("auth.login_failed", level="warning", email=email)
            return render(request, "login.html", error="Wrong email or password.", user=None)
        login_email_limit.reset(email)
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
    def demo_inbound_sms(slug: str, body: SmsIn, request: Request):
        """Simulated inbound text (a customer replying to a follow-up). Demo businesses only."""
        tid = _demo_tenant(slug, request, demo_sms_limit)
        return revenue.inbound_sms(tid, body.from_number, body.text)

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
        return render(
            request,
            "admin.html",
            user=user,
            rows=rows,
            errors=errors,
            mrr=mrr,
            demo_access=demo_access,
            seen_ip=client_ip(request),
            demo_link=request.query_params.get("demo_link"),
            retell_ready=bool(retell_key),
            voice_status=voice_agents.status,
        )

    @app.post("/admin/tenants/{tenant_id}/retell-agent", include_in_schema=False)
    def setup_retell_agent(
        request: Request,
        tenant_id: str,
        trade: str = Form("plumbing"),
        area: str = Form("Toronto and the GTA"),
        replace: str = Form(""),
        csrf: str = Form(""),
        user: dict = Depends(require_admin),
    ):
        """Create this business's Retell agent with the key already in the server's env.

        Free: creates an LLM and an agent only. Never buys a phone number.
        """
        check_csrf(user, csrf)
        from ..voice import setup as retell_setup

        t = store.tenant(tenant_id)
        if not t:
            raise HTTPException(404, "unknown business")
        if not retell_key:
            raise HTTPException(503, "RETELL_API_KEY is not set on the server")
        if t.get("retell_agent_id") and replace != "1":
            raise HTTPException(409, "this business already has a voice agent")
        base = os.environ.get("FLOW_PUBLIC_URL", "").rstrip("/") or str(request.base_url).rstrip(
            "/"
        )
        if base.startswith("http://") and env != "local":
            base = "https://" + base[len("http://") :]  # behind Render's TLS proxy
        poster = getattr(app.state, "retell_poster", None)
        try:
            ids = retell_setup.create_agent(
                retell_key, base, t["slug"], trade, t["name"], area[:80], poster=poster
            )
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except Exception as e:  # Retell down or refused: show it, store nothing
            AuditLog(db, tenant_id).record(
                "retell.agent_setup_failed", level="error", error=f"{type(e).__name__}: {e}"[:200]
            )
            raise HTTPException(502, f"Retell refused the setup: {type(e).__name__}") from e
        db.execute(
            "UPDATE tenants SET retell_agent_id = ?, retell_llm_id = ? WHERE id = ?",
            (ids["agent_id"], ids["llm_id"], tenant_id),
        )
        AuditLog(db, tenant_id).record(
            "retell.agent_created",
            actor=f"user:{user['email']}",
            agent_id=ids["agent_id"],
            tools_base=base,
            trade=trade,
        )
        return RedirectResponse("/admin", 303)

    @app.post("/admin/demo-links", include_in_schema=False)
    def create_demo_link(
        request: Request,
        slug: str = Form(...),
        hours: float = Form(72),
        label: str = Form(""),
        csrf: str = Form(""),
        user: dict = Depends(require_admin),
    ):
        check_csrf(user, csrf)
        t = store.tenant_by_slug(slug)
        if not t or not t["is_demo"]:
            raise HTTPException(404, "demo links are only for demo businesses")
        hours = min(max(hours, 1), 24 * 14)
        token = ctx.signer.demo_link_token(slug, hours, label)
        AuditLog(db, t["id"]).record(
            "demo.link_created", actor=f"user:{user['email']}", hours=hours, label=label[:60]
        )
        from urllib.parse import quote

        url = f"{str(request.base_url).rstrip('/')}/demo/{slug}?k={quote(token)}"
        return RedirectResponse(f"/admin?demo_link={quote(url)}", 303)

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
    def demo_allowed(request: Request, t: dict, token: str | None = None) -> bool:
        if demo_access == "public":
            return True
        user = current_user(request)
        if user and (user["role"] == "platform_admin" or user["tenant_id"] == t["id"]):
            return True
        token = token or request.cookies.get(DEMO_COOKIE)
        return ctx.signer.demo_link_loads(token, t["slug"]) is not None

    @app.get("/demo/{slug}", response_class=HTMLResponse, include_in_schema=False)
    def demo_page(request: Request, slug: str, k: str | None = None):
        t = store.tenant_by_slug(slug)
        if not t or not t["is_demo"]:  # real clients never get a public demo page
            raise HTTPException(404, "unknown business")
        if not demo_allowed(request, t, token=k):
            return HTMLResponse(
                "<!doctype html><title>Demo link needed</title><body style='font-family:"
                "system-ui;max-width:32rem;margin:4rem auto;padding:0 1rem'><h1>This demo "
                "needs an invite link</h1><p>The link may have expired. Ask Flow Automations "
                "for a new one.</p></body>",
                status_code=403,
            )
        # Real voice agent in the browser (Retell web call). The server starts the call with
        # its own key and hands the browser a one-call token, so no key reaches the page.
        retell_web = None
        if retell_key and VoiceAgents.trade_for(t):
            retell_web = {"slug": slug}
        resp = render(
            request, "call.html", tenant=t, user=current_user(request), retell_web=retell_web
        )
        link = ctx.signer.demo_link_loads(k, slug) if k else None
        if link:  # remember the invite so the page's own API calls are allowed
            resp.set_cookie(
                DEMO_COOKIE,
                k,
                max_age=max(60, int(link["exp"] - datetime.now(UTC).timestamp())),
                httponly=True,
                samesite="lax",
                secure=env != "local",
            )
        return resp

    def purge_old_demo_calls(tenant_id: str) -> None:
        """Browser-demo bookings expire, so strangers can't fill the demo calendar for good."""
        cutoff = datetime.fromtimestamp(
            clock().timestamp() - demo_retention_hours * 3600, UTC
        ).isoformat()
        args = (tenant_id, cutoff)
        for sql in _PURGE_DEMO_SQL:
            db.execute(sql, args)

    def _demo_tenant(slug: str, request: Request | None = None, limiter=None) -> str:
        t = store.tenant_by_slug(slug)
        if not t or not t["is_demo"]:
            raise HTTPException(404, "unknown business")
        if request is not None and not demo_allowed(request, t):
            raise HTTPException(403, "This demo needs an invite link.")
        if request is not None and limiter is not None:
            if not limiter.hit(f"{client_ip(request)}|{slug}"):
                raise HTTPException(429, "Too many demo requests. Please try again later.")
        return t["id"]

    @app.post("/api/demo/{slug}/calls")
    def demo_start(slug: str, request: Request):
        tid = _demo_tenant(slug, request, demo_call_limit)
        purge_old_demo_calls(tid)
        if not demo_daily_limit.hit(slug):
            raise HTTPException(429, "The demo line is busy today. Please try again tomorrow.")
        try:
            return ctx.calls.start(tid, channel="browser_demo")
        except CallError as e:
            raise HTTPException(409, str(e)) from e

    @app.post("/api/demo/{slug}/voice-call")
    def demo_voice_call(slug: str, request: Request):
        """Start a real Retell voice call in the browser. Returns a one-call access token."""
        tid = _demo_tenant(slug, request, demo_voice_limit)
        t = store.tenant(tid)
        trade = VoiceAgents.trade_for(t)
        if not retell_key or not trade:
            raise HTTPException(404, "This business has no voice agent.")
        if not demo_voice_daily.hit(slug):
            raise HTTPException(429, "The voice line is busy today. Try the text demo below.")
        purge_old_demo_calls(tid)
        try:
            return voice_agents.web_call(t, trade, metadata={"source": "browser_demo"})
        except Exception as e:  # Retell down or refused: say so, keep the text demo usable
            AuditLog(db, tid).record(
                "retell.web_call_failed", level="error", error=f"{type(e).__name__}: {e}"[:200]
            )
            raise HTTPException(
                502, "The voice agent couldn't start. The text demo below still works."
            ) from e

    @app.post("/api/demo/{slug}/calls/{call_id}/turn")
    def demo_turn(slug: str, call_id: str, body: TurnIn, request: Request):
        tid = _demo_tenant(slug, request, demo_turn_limit)
        try:
            return ctx.calls.turn(tid, call_id, body.text)
        except CallError as e:
            raise HTTPException(409, str(e)) from e

    @app.post("/api/demo/{slug}/calls/{call_id}/end")
    def demo_end(slug: str, call_id: str, request: Request):
        try:
            c = ctx.calls.end(_demo_tenant(slug, request), call_id)
        except CallError as e:
            raise HTTPException(404, str(e)) from e
        return {
            "call_id": c["id"],
            "outcome": c["outcome"],
            "summary": c["summary"],
            "billed_minutes": c["billed_minutes"],
        }

    # ------------------------------------------------------------------ SMS provider webhooks
    # Twilio calls these. Both check Twilio's signature against FLOW_PUBLIC_URL + the path,
    # because behind a proxy the URL the app sees is not the one Twilio signed.
    async def twilio_form(request: Request) -> dict:
        sender = revenue.sms
        if sender.name != "twilio":
            raise HTTPException(503, "Twilio is not configured")
        params = {k: str(v) for k, v in (await request.form()).items()}
        public = os.environ.get("FLOW_PUBLIC_URL", "").rstrip("/")
        url = (public + request.url.path) if public else str(request.url)
        if request.url.query:
            url += "?" + request.url.query
        if not sender.valid_signature(url, params, request.headers.get("x-twilio-signature")):
            raise HTTPException(403, "bad signature")
        return params

    @app.post("/sms/twilio/status", include_in_schema=False)
    async def twilio_status(request: Request):
        """Delivery receipts: the only proof a text actually reached the phone."""
        p = await twilio_form(request)
        status = {"delivered": "delivered", "undelivered": "undelivered", "failed": "failed"}.get(
            p.get("MessageStatus", ""), None
        )
        if status and p.get("MessageSid"):
            err = p.get("ErrorCode")
            db.execute(
                "UPDATE messages SET status = ?, error = COALESCE(?, error), updated_at = ? "
                "WHERE provider = 'twilio' AND provider_id = ?",
                (
                    status,
                    f"Twilio error {err}" if err else None,
                    clock().isoformat(),
                    p["MessageSid"],
                ),
            )
        return Response(status_code=204)

    @app.post("/sms/twilio/inbound/{slug}", include_in_schema=False)
    async def twilio_inbound(slug: str, request: Request):
        """A customer texts the business: STOP/START, C to cancel, or a reply to a follow-up."""
        p = await twilio_form(request)
        t = store.tenant_by_slug(slug)
        if not t or t["status"] != "active":
            raise HTTPException(404, "unknown business")
        revenue.inbound_sms(t["id"], p.get("From", ""), p.get("Body", ""))
        # Empty TwiML: any reply text is sent by our own outbox, not by Twilio.
        return Response("<Response/>", media_type="application/xml")

    # ------------------------------------------------------------------ voice agent tools
    # Shaped for Retell custom functions: POST {"name", "args", "call"}. Auth by tenant key.
    def voice_tenant(authorization: str | None = Header(None)) -> str:
        key = (authorization or "").removeprefix("Bearer ").strip()
        h = hashlib.sha256(key.encode()).hexdigest()
        t = db.one("SELECT id FROM tenants WHERE api_key_hash = ? AND status = 'active'", (h,))
        if not key or not t:
            raise HTTPException(401, "invalid tenant key")
        return t["id"]

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
                sms = tools.send_confirmation(booked["appointment_id"], call_id=call_ref)
                return {**booked, "lead_id": lead_id, "confirmation_text_sent": sms == "sent"}
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
            **({"voice_agents": voice_agents.status} if env != "production" else {}),
        }

    app.state.json = json
    return app


def __getattr__(name: str):  # `uvicorn flow.web.app:app`
    if name == "app":
        return create_web_app()
    raise AttributeError(name)
