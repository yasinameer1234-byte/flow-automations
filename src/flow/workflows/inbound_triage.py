"""Demo workflow: AI triage of inbound business messages.

  receive -> classify (AI) -> policy checks -> CRM update -> propose replies/refunds
  -> human approval for consequential actions -> execute -> audit

The routing table (ROUTES) is the only market-specific part. Swapping verticals
(e.g. property management, clinics, agencies) means changing prompts, categories
and route handlers, not the engine.
"""

from __future__ import annotations

from collections.abc import Callable

from ..ai.classifier import Classifier
from ..core.engine import RunContext
from ..core.models import (
    Category,
    Classification,
    InboundMessage,
    ProposedAction,
    RunStatus,
    Urgency,
    WorkflowResult,
)
from ..core.retry import PermanentError, RetryExhausted, RetryPolicy, with_retry
from ..integrations.crm import CRM

WORKFLOW_NAME = "inbound_triage"


def _reply_templates(company: str) -> dict[str, tuple[str, str]]:
    return {
        Category.SALES_LEAD: (
            "Thanks for reaching out to {company}",
            "Hi {name},\n\nThanks for your interest. We'd love to learn more about {summary_lc}. "
            "Could you share two times that work for a 20-minute call this week?\n\n"
            "Best,\nThe {company} team",
        ),
        Category.SUPPORT_REQUEST: (
            "We received your request",
            "Hi {name},\n\nWe've opened ticket #{ticket_id} for: {summary}. "
            "Our team will follow up shortly.\n\nThe {company} team",
        ),
        Category.BILLING: (
            "About your billing question",
            "Hi {name},\n\nWe've logged your billing request (ticket #{ticket_id}). "
            "{refund_line}\n\nThe {company} team",
        ),
    }


class InboundTriage:
    def __init__(self, classifier: Classifier, crm: CRM, retry: RetryPolicy) -> None:
        self.classifier = classifier
        self.crm = crm
        self.retry = retry
        self.routes: dict[
            Category, Callable[[RunContext, InboundMessage, Classification], WorkflowResult]
        ] = {
            Category.SALES_LEAD: self._sales_lead,
            Category.SUPPORT_REQUEST: self._support,
            Category.BILLING: self._billing,
            Category.SPAM: self._spam,
            Category.OTHER: self._needs_review_other,
        }

    # ------------------------------------------------------------------ entry
    def __call__(self, ctx: RunContext, msg: InboundMessage) -> WorkflowResult:
        ctx.step(
            "input.received",
            source=msg.source,
            has_fields=bool(msg.fields),
            body_chars=len(msg.body),
        )

        def on_retry(attempt: int, err: Exception, delay: float) -> None:
            ctx.audit.record(
                "ai.retry", run_id=ctx.run_id, level="warning", attempt=attempt, error=str(err)
            )

        try:
            c = with_retry(
                lambda: self.classifier.classify(msg),
                operation="classify",
                policy=self.retry,
                on_retry=on_retry,
            )
        except (RetryExhausted, PermanentError) as e:
            ctx.audit.record("ai.failed", run_id=ctx.run_id, level="error", error=str(e))
            return self._result(
                ctx,
                RunStatus.NEEDS_REVIEW,
                None,
                "AI classification unavailable; queued for a person",
                errors=[str(e)],
            )

        ctx.step(
            "ai.classified",
            classifier=self.classifier.name,
            category=c.category.value,
            confidence=c.confidence,
            urgency=c.urgency.value,
            reasoning=c.reasoning,
        )

        if c.confidence < ctx.tenant.min_confidence and c.category != Category.SPAM:
            ctx.step(
                "policy.low_confidence",
                confidence=c.confidence,
                threshold=ctx.tenant.min_confidence,
            )
            return self._result(
                ctx,
                RunStatus.NEEDS_REVIEW,
                c,
                f"Low confidence ({c.confidence:.2f}); queued for a person",
            )
        return self.routes[c.category](ctx, msg, c)

    # ------------------------------------------------------------------ routes
    def _sales_lead(self, ctx, msg, c) -> WorkflowResult:
        if not c.contact_email:
            return self._result(ctx, RunStatus.NEEDS_REVIEW, c, "Lead has no email; needs a person")
        contact_id = self.crm.upsert_contact(c.contact_email, c.contact_name, c.company, "lead")
        deal_id = self.crm.create_deal(contact_id, c.summary[:120], c.estimated_value, ctx.run_id)
        self.crm.add_note(contact_id, f"[AI triage] {c.summary}")
        ctx.step("crm.updated", contact_id=contact_id, deal_id=deal_id, value=c.estimated_value)
        high_value = (c.estimated_value or 0) >= ctx.tenant.high_value_deal_threshold
        subject, body = self._render(ctx, Category.SALES_LEAD, c)
        ctx.propose(
            ProposedAction(
                action="send_email",
                params={"to": c.contact_email, "subject": subject, "body": body},
                reason=("HIGH-VALUE lead: " if high_value else "") + "first reply to a new lead",
            )
        )
        return self._result(
            ctx, None, c, f"New lead captured (deal #{deal_id})", contact_id=contact_id
        )

    def _support(self, ctx, msg, c) -> WorkflowResult:
        contact_id = (
            self.crm.upsert_contact(c.contact_email, c.contact_name, c.company)
            if c.contact_email
            else None
        )
        priority = "high" if c.urgency == Urgency.HIGH else "normal"
        ticket_id = self.crm.create_ticket(
            contact_id, "support", c.summary[:120], priority, ctx.run_id
        )
        ctx.step("crm.updated", contact_id=contact_id, ticket_id=ticket_id, priority=priority)
        if c.contact_email and ctx.tenant.auto_acknowledge_support:
            subject, body = self._render(ctx, Category.SUPPORT_REQUEST, c, ticket_id=ticket_id)
            ctx.propose(
                ProposedAction(
                    action="send_email",
                    params={"to": c.contact_email, "subject": subject, "body": body},
                    reason="support acknowledgement",
                )
            )
        return self._result(
            ctx, None, c, f"Support ticket #{ticket_id} opened ({priority})", contact_id=contact_id
        )

    def _billing(self, ctx, msg, c) -> WorkflowResult:
        contact_id = (
            self.crm.upsert_contact(c.contact_email, c.contact_name, c.company)
            if c.contact_email
            else None
        )
        ticket_id = self.crm.create_ticket(
            contact_id,
            "billing",
            c.summary[:120],
            "high" if c.refund_requested else "normal",
            ctx.run_id,
        )
        ctx.step("crm.updated", contact_id=contact_id, ticket_id=ticket_id)
        if c.refund_requested and c.contact_email and c.estimated_value:
            ctx.propose(
                ProposedAction(
                    action="issue_refund",
                    params={"customer_email": c.contact_email, "amount": c.estimated_value},
                    reason=f"customer requested refund of {c.estimated_value:.2f}",
                )
            )
        if c.contact_email:
            subject, body = self._render(ctx, Category.BILLING, c, ticket_id=ticket_id)
            ctx.propose(
                ProposedAction(
                    action="send_email",
                    params={"to": c.contact_email, "subject": subject, "body": body},
                    reason="billing reply",
                )
            )
        return self._result(
            ctx, None, c, f"Billing ticket #{ticket_id} opened", contact_id=contact_id
        )

    def _spam(self, ctx, msg, c) -> WorkflowResult:
        ctx.step("policy.spam_dropped")
        return self._result(ctx, RunStatus.IGNORED, c, "Spam; no action taken")

    def _needs_review_other(self, ctx, msg, c) -> WorkflowResult:
        return self._result(ctx, RunStatus.NEEDS_REVIEW, c, "Uncategorised; queued for a person")

    # ------------------------------------------------------------------ helpers
    def _render(self, ctx, category, c, ticket_id: int | None = None) -> tuple[str, str]:
        subject_t, body_t = _reply_templates(ctx.tenant.company_name)[category]
        refund_line = (
            "Your refund request is being reviewed by our team."
            if c.refund_requested
            else "We'll get back to you within one business day."
        )
        values = {
            "company": ctx.tenant.company_name,
            "name": (c.contact_name or "there").split()[0],
            "summary": c.summary,
            "summary_lc": c.summary[:1].lower() + c.summary[1:],
            "ticket_id": ticket_id,
            "refund_line": refund_line,
        }
        return subject_t.format(**values), body_t.format(**values)

    def _result(
        self,
        ctx: RunContext,
        status: RunStatus | None,
        c: Classification | None,
        summary: str,
        contact_id: int | None = None,
        errors: list[str] | None = None,
    ) -> WorkflowResult:
        errs = list(ctx.errors) + (errors or [])
        if status is None:
            if errs:
                status = RunStatus.FAILED
            elif ctx.approvals:
                status = RunStatus.AWAITING_APPROVAL
            else:
                status = RunStatus.COMPLETED
        record = self.crm.get_contact(contact_id) if contact_id else None
        return WorkflowResult(
            run_id=ctx.run_id,
            status=status,
            category=c.category if c else None,
            confidence=c.confidence if c else None,
            summary=summary,
            crm_contact_id=contact_id,
            crm_record=record,
            approvals=list(ctx.approvals),
            actions_taken=list(ctx.actions_taken),
            errors=errs,
        )
