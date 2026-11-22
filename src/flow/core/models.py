"""Domain models shared across workflows, integrations and the API."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Category(StrEnum):
    SALES_LEAD = "sales_lead"
    SUPPORT_REQUEST = "support_request"
    BILLING = "billing"
    SPAM = "spam"
    OTHER = "other"


class Urgency(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


class InboundMessage(BaseModel):
    """Anything that arrives: an email, a web form, a chat transcript.

    `body` can be unstructured free text; `fields` holds structured form data if any.
    """

    source: str = Field(description="Channel, e.g. email, web_form, chat")
    external_id: str | None = Field(default=None, description="ID from the source system")
    sender_name: str | None = None
    sender_email: str | None = None
    subject: str | None = None
    body: str
    fields: dict[str, Any] = Field(default_factory=dict)
    tenant_id: str = "demo"

    def dedupe_key(self) -> str:
        """Stable key so a re-delivered webhook doesn't run the workflow twice."""
        basis = self.external_id or f"{self.sender_email}|{self.subject}|{self.body}"
        return hashlib.sha256(f"{self.tenant_id}|{self.source}|{basis}".encode()).hexdigest()[:32]


class Classification(BaseModel):
    """What the AI step returns. The schema is enforced on the model output."""

    category: Category
    confidence: float = Field(ge=0.0, le=1.0)
    urgency: Urgency = Urgency.NORMAL
    summary: str = Field(description="One-sentence summary of what the sender wants")
    contact_name: str | None = None
    contact_email: str | None = None
    company: str | None = None
    estimated_value: float | None = Field(default=None, description="Deal or refund amount")
    refund_requested: bool = False
    reasoning: str = Field(default="", description="Short justification for the category")


class RunStatus(StrEnum):
    COMPLETED = "completed"
    AWAITING_APPROVAL = "awaiting_approval"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"
    DUPLICATE = "duplicate"
    IGNORED = "ignored"


class ProposedAction(BaseModel):
    """A consequential action that is held until a human approves it."""

    action: str  # e.g. send_email, issue_refund
    params: dict[str, Any]
    reason: str


class WorkflowResult(BaseModel):
    run_id: str
    status: RunStatus
    category: Category | None = None
    confidence: float | None = None
    summary: str
    crm_contact_id: int | None = None
    crm_record: dict[str, Any] | None = None
    approvals: list[str] = Field(default_factory=list)
    actions_taken: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
