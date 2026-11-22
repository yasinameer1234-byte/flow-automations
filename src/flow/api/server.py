"""HTTP API (FastAPI). Run: `uvicorn flow.api.server:app --reload`.

Local/dev only: no authentication yet. Do not expose this publicly.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from ..app import FlowApp, build_app
from ..core.approvals import ApprovalError
from ..core.models import InboundMessage, WorkflowResult


class Decision(BaseModel):
    decided_by: str
    note: str = ""


def create_api(flow: FlowApp | None = None) -> FastAPI:
    flow = flow or build_app()
    api = FastAPI(title="Flow Automations (dev)", version="0.1.0")

    @api.get("/health")
    def health() -> dict:
        return {
            "status": "ok",
            "ai_provider": flow.settings.ai_provider,
            "tenant": flow.settings.tenant.tenant_id,
        }

    @api.post("/intake", response_model=WorkflowResult)
    def intake(msg: InboundMessage) -> WorkflowResult:
        return flow.process(msg)

    @api.get("/runs/{run_id}")
    def get_run(run_id: str) -> dict:
        run = flow.engine.get_run(run_id)
        if not run:
            raise HTTPException(404, "run not found")
        run["audit"] = flow.engine.audit.for_run(run_id)
        return run

    @api.get("/approvals")
    def approvals() -> list[dict]:
        return flow.engine.approvals.pending()

    @api.post("/approvals/{approval_id}/approve")
    def approve(approval_id: str, d: Decision) -> dict:
        try:
            return flow.engine.decide(
                approval_id, approve=True, decided_by=d.decided_by, note=d.note
            )
        except ApprovalError as e:
            raise HTTPException(409, str(e)) from e

    @api.post("/approvals/{approval_id}/reject")
    def reject(approval_id: str, d: Decision) -> dict:
        try:
            return flow.engine.decide(
                approval_id, approve=False, decided_by=d.decided_by, note=d.note
            )
        except ApprovalError as e:
            raise HTTPException(409, str(e)) from e

    @api.get("/crm/summary")
    def crm_summary() -> dict:
        return flow.crm.summary()

    @api.get("/audit")
    def audit(limit: int = 100) -> list[dict]:
        return flow.engine.audit.recent(min(limit, 1000))

    return api


def __getattr__(name: str):  # lazy so importing the module doesn't open a DB
    if name == "app":
        return create_api()
    raise AttributeError(name)
