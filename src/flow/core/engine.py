"""Workflow engine: run bookkeeping, idempotency, policy-gated actions, execution.

Deliberately small. It gives every workflow the same guarantees:
  * a run record per input, deduplicated by a stable key (safe webhook re-delivery)
  * an audit trail for each step
  * consequential actions go through `propose()`; tenant policy decides whether
    they need human approval or may run immediately
  * actions execute with retries; exhausted retries go to a dead-letter table
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..config import TenantPolicy
from .approvals import ApprovalError, ApprovalStore
from .audit import AuditLog
from .db import Database
from .models import InboundMessage, ProposedAction, RunStatus, WorkflowResult, utcnow
from .retry import PermanentError, RetryExhausted, RetryPolicy, with_retry

ActionHandler = Callable[[dict[str, Any], str], str]  # (params, idempotency_key) -> provider ref


@dataclass
class RunContext:
    run_id: str
    tenant: TenantPolicy
    audit: AuditLog
    engine: Engine
    actions_taken: list[str] = field(default_factory=list)
    approvals: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def step(self, event: str, **detail: Any) -> None:
        self.audit.record(event, run_id=self.run_id, **detail)

    def propose(self, proposal: ProposedAction) -> None:
        """Run now if policy allows, otherwise file for human approval."""
        if proposal.action in self.tenant.approval_required:
            approval_id = self.engine.approvals.request(self.run_id, proposal)
            self.approvals.append(approval_id)
            self.step(
                "approval.requested",
                approval_id=approval_id,
                action=proposal.action,
                reason=proposal.reason,
                params=proposal.params,
            )
        else:
            ok, ref = self.engine.execute_action(
                self.run_id,
                proposal.action,
                proposal.params,
                idempotency_key=f"{self.run_id}:{proposal.action}",
            )
            (self.actions_taken if ok else self.errors).append(f"{proposal.action}: {ref}")


class Engine:
    def __init__(self, db: Database, tenant: TenantPolicy, retry: RetryPolicy) -> None:
        self.db = db
        self.tenant = tenant
        self.retry = retry
        self.audit = AuditLog(db, tenant.tenant_id)
        self.approvals = ApprovalStore(db, tenant.tenant_id)
        self.handlers: dict[str, ActionHandler] = {}

    # ------------------------------------------------------------------ registration
    def register_action(self, name: str, handler: ActionHandler) -> None:
        self.handlers[name] = handler

    # ------------------------------------------------------------------ runs
    def start_run(self, workflow: str, msg: InboundMessage) -> tuple[str, bool]:
        """Returns (run_id, is_new). Duplicate deliveries return the original run."""
        key = msg.dedupe_key()
        existing = self.db.one(
            "SELECT id FROM runs WHERE tenant_id = ? AND workflow = ? AND dedupe_key = ?",
            (self.tenant.tenant_id, workflow, key),
        )
        if existing:
            return existing["id"], False
        run_id = f"run_{uuid.uuid4().hex[:12]}"
        now = utcnow()
        self.db.execute(
            "INSERT INTO runs (id, tenant_id, workflow, dedupe_key, status, input_json, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, 'running', ?, ?, ?)",
            (run_id, self.tenant.tenant_id, workflow, key, msg.model_dump_json(), now, now),
        )
        return run_id, True

    def finish_run(self, result: WorkflowResult) -> None:
        self.db.execute(
            "UPDATE runs SET status = ?, result_json = ?, updated_at = ? WHERE id = ?",
            (result.status.value, result.model_dump_json(), utcnow(), result.run_id),
        )

    def get_run(self, run_id: str) -> dict | None:
        row = self.db.one(
            "SELECT * FROM runs WHERE id = ? AND tenant_id = ?", (run_id, self.tenant.tenant_id)
        )
        if row:
            row["input"] = json.loads(row.pop("input_json"))
            row["result"] = json.loads(row.pop("result_json")) if row["result_json"] else None
        return row

    def run(
        self,
        workflow: str,
        msg: InboundMessage,
        body: Callable[[RunContext, InboundMessage], WorkflowResult],
    ) -> WorkflowResult:
        run_id, is_new = self.start_run(workflow, msg)
        if not is_new:
            self.audit.record("run.duplicate_ignored", run_id=run_id, source=msg.source)
            prior = self.get_run(run_id)
            return WorkflowResult(
                run_id=run_id,
                status=RunStatus.DUPLICATE,
                summary=f"Already processed (status: {prior['status']})",
            )
        ctx = RunContext(run_id=run_id, tenant=self.tenant, audit=self.audit, engine=self)
        ctx.step("run.started", workflow=workflow, source=msg.source, external_id=msg.external_id)
        try:
            result = body(ctx, msg)
        except Exception as e:  # last-resort guard: never lose a run silently
            ctx.audit.record(
                "run.crashed", run_id=run_id, level="error", error=f"{type(e).__name__}: {e}"
            )
            result = WorkflowResult(
                run_id=run_id,
                status=RunStatus.FAILED,
                summary="Workflow crashed; see audit log",
                errors=[f"{type(e).__name__}: {e}"],
            )
        self.finish_run(result)
        ctx.step("run.finished", status=result.status.value)
        return result

    # ------------------------------------------------------------------ actions
    def execute_action(
        self, run_id: str, action: str, params: dict[str, Any], idempotency_key: str
    ) -> tuple[bool, str]:
        handler = self.handlers.get(action)
        if handler is None:
            self.audit.record("action.unknown", run_id=run_id, level="error", action=action)
            return False, "no handler registered"

        def on_retry(attempt: int, err: Exception, delay: float) -> None:
            self.audit.record(
                "action.retry",
                run_id=run_id,
                level="warning",
                action=action,
                attempt=attempt,
                error=str(err),
                next_delay_s=round(delay, 2),
            )

        try:
            ref = with_retry(
                lambda: handler(params, idempotency_key),
                operation=action,
                policy=self.retry,
                on_retry=on_retry,
            )
        except (RetryExhausted, PermanentError) as e:
            self.db.execute(
                "INSERT INTO dead_letters (tenant_id, run_id, operation, payload_json, error, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (self.tenant.tenant_id, run_id, action, json.dumps(params), str(e), utcnow()),
            )
            self.audit.record(
                "action.failed",
                run_id=run_id,
                level="error",
                action=action,
                error=str(e),
                dead_lettered=True,
            )
            return False, str(e)
        self.audit.record("action.executed", run_id=run_id, action=action, provider_ref=ref)
        return True, ref

    def decide(self, approval_id: str, *, approve: bool, decided_by: str, note: str = "") -> dict:
        """Record a human decision; on approval, execute the held action."""
        apr = self.approvals.decide(approval_id, approve=approve, decided_by=decided_by, note=note)
        run_id = apr["run_id"]
        self.audit.record(
            "approval.approved" if approve else "approval.rejected",
            run_id=run_id,
            actor=f"human:{decided_by}",
            approval_id=approval_id,
            action=apr["action"],
            note=note,
        )
        outcome: dict[str, Any] = {"approval_id": approval_id, "status": apr["status"]}
        if approve:
            ok, ref = self.execute_action(
                run_id, apr["action"], apr["params"], idempotency_key=approval_id
            )
            self.approvals.mark(approval_id, "executed" if ok else "failed")
            outcome.update(status="executed" if ok else "failed", provider_ref=ref)
        self._maybe_close_run(run_id)
        return outcome

    def _maybe_close_run(self, run_id: str) -> None:
        pending = self.db.one(
            "SELECT COUNT(*) AS n FROM approvals WHERE run_id = ? AND status = 'pending'", (run_id,)
        )
        if pending and pending["n"] == 0:
            failed = self.db.one(
                "SELECT COUNT(*) AS n FROM approvals WHERE run_id = ? AND status = 'failed'",
                (run_id,),
            )
            status = RunStatus.FAILED if failed and failed["n"] else RunStatus.COMPLETED
            self.db.execute(
                "UPDATE runs SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
                (status.value, utcnow(), run_id, RunStatus.AWAITING_APPROVAL.value),
            )
            self.audit.record("run.closed", run_id=run_id, status=status.value)


__all__ = ["ApprovalError", "Engine", "RunContext"]
