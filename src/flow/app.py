"""Composition root: wires settings, storage, integrations and workflows together."""

from __future__ import annotations

from dataclasses import dataclass

from .ai.classifier import Classifier, build_classifier
from .config import Settings, load_settings
from .core.audit import configure_logging
from .core.db import Database
from .core.engine import Engine
from .core.models import InboundMessage, WorkflowResult
from .core.retry import RetryPolicy
from .integrations.crm import SqliteCRM
from .integrations.email import SimulatedEmailSender
from .integrations.payments import SimulatedPayments
from .workflows.inbound_triage import WORKFLOW_NAME, InboundTriage


@dataclass
class FlowApp:
    settings: Settings
    db: Database
    engine: Engine
    crm: SqliteCRM
    email: SimulatedEmailSender
    payments: SimulatedPayments
    triage: InboundTriage

    def process(self, msg: InboundMessage) -> WorkflowResult:
        msg = msg.model_copy(update={"tenant_id": self.settings.tenant.tenant_id})
        return self.engine.run(WORKFLOW_NAME, msg, self.triage)


def build_app(
    settings: Settings | None = None,
    *,
    db: Database | None = None,
    classifier: Classifier | None = None,
    email: SimulatedEmailSender | None = None,
    retry: RetryPolicy | None = None,
) -> FlowApp:
    settings = settings or load_settings()
    configure_logging(settings.log_level, settings.log_json)
    if settings.email_provider != "simulated":
        raise ValueError("only the simulated email provider exists in v0.1")
    db = db or Database(settings.db_path)
    tenant_id = settings.tenant.tenant_id
    retry = retry or RetryPolicy(
        attempts=settings.retry_attempts, base_delay=settings.retry_base_delay
    )
    engine = Engine(db, settings.tenant, retry)
    crm = SqliteCRM(db, tenant_id)
    email = email or SimulatedEmailSender(db, tenant_id)
    payments = SimulatedPayments(db, tenant_id)
    classifier = classifier or build_classifier(
        settings.ai_provider, settings.ai_model, settings.ai_effort
    )

    engine.register_action(
        "send_email", lambda p, key: email.send(p["to"], p["subject"], p["body"], key)
    )
    engine.register_action(
        "issue_refund", lambda p, key: payments.refund(p["customer_email"], float(p["amount"]), key)
    )
    triage = InboundTriage(classifier, crm, retry)
    return FlowApp(settings, db, engine, crm, email, payments, triage)
