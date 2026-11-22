from flow.ai.classifier import SimulatedClassifier
from flow.app import build_app
from flow.core.db import Database
from flow.core.models import RunStatus
from flow.core.retry import TransientError
from flow.integrations.email import SimulatedEmailSender

from .conftest import BILLING, LEAD, SPAM, SUPPORT, msg


def test_lead_updates_crm_and_waits_for_approval(flow):
    r = flow.process(msg(**LEAD))
    assert r.status == RunStatus.AWAITING_APPROVAL
    assert r.crm_record["email"] == "priya@example.com"  # normalised
    assert r.crm_record["deals"][0]["value"] == 12000
    assert len(r.approvals) == 1
    # Nothing has been sent yet: the human hasn't approved.
    assert flow.email.outbox() == []
    pending = flow.engine.approvals.pending()
    assert pending[0]["action"] == "send_email"
    assert pending[0]["reason"].startswith("HIGH-VALUE")


def test_approval_sends_email_and_closes_run(flow):
    r = flow.process(msg(**LEAD))
    out = flow.engine.decide(r.approvals[0], approve=True, decided_by="yaso")
    assert out["status"] == "executed"
    assert len(flow.email.outbox()) == 1
    assert flow.engine.get_run(r.run_id)["status"] == "completed"
    events = [e["event"] for e in flow.engine.audit.for_run(r.run_id)]
    for e in [
        "run.started",
        "input.received",
        "ai.classified",
        "crm.updated",
        "approval.requested",
        "approval.approved",
        "action.executed",
        "run.closed",
    ]:
        assert e in events


def test_rejection_sends_nothing(flow):
    r = flow.process(msg(**LEAD))
    out = flow.engine.decide(r.approvals[0], approve=False, decided_by="yaso", note="wrong tone")
    assert out["status"] == "rejected"
    assert flow.email.outbox() == []
    assert flow.engine.get_run(r.run_id)["status"] == "completed"


def test_support_ticket_without_email_by_default(flow):
    r = flow.process(msg(**SUPPORT))
    assert r.status == RunStatus.COMPLETED
    assert r.crm_record["tickets"][0]["priority"] == "high"
    assert r.approvals == []


def test_billing_refund_needs_two_approvals(flow):
    r = flow.process(msg(**BILLING))
    actions = sorted(p["action"] for p in flow.engine.approvals.pending())
    assert actions == ["issue_refund", "send_email"]
    for a in r.approvals:
        flow.engine.decide(a, approve=True, decided_by="yaso")
    refunds = flow.db.query("SELECT amount FROM sim_refunds")
    assert refunds == [{"amount": 249.0}]


def test_spam_is_ignored_without_crm_writes(flow):
    r = flow.process(msg(**SPAM))
    assert r.status == RunStatus.IGNORED
    assert flow.crm.summary()["contacts"] == 0


def test_low_confidence_goes_to_review(flow):
    r = flow.process(msg(external_id="u1", body="following up on what we discussed"))
    assert r.status == RunStatus.NEEDS_REVIEW
    assert flow.crm.summary()["contacts"] == 0


def test_duplicate_delivery_is_idempotent(flow):
    first = flow.process(msg(**LEAD))
    second = flow.process(msg(**LEAD))
    assert second.status == RunStatus.DUPLICATE and second.run_id == first.run_id
    assert flow.crm.summary()["deals"] == 1


class FlakyClassifier(SimulatedClassifier):
    name = "flaky"

    def __init__(self, failures):
        self.failures = failures

    def classify(self, m):
        if self.failures > 0:
            self.failures -= 1
            raise TransientError("503 from AI provider")
        return super().classify(m)


def test_ai_transient_errors_are_retried(settings, retry):
    flow = build_app(
        settings, db=Database(":memory:"), retry=retry, classifier=FlakyClassifier(failures=2)
    )
    r = flow.process(msg(**LEAD))
    assert r.status == RunStatus.AWAITING_APPROVAL
    assert [e["event"] for e in flow.engine.audit.for_run(r.run_id)].count("ai.retry") == 2


def test_ai_outage_routes_to_human(settings, retry):
    flow = build_app(
        settings, db=Database(":memory:"), retry=retry, classifier=FlakyClassifier(failures=99)
    )
    r = flow.process(msg(**LEAD))
    assert r.status == RunStatus.NEEDS_REVIEW
    assert flow.crm.summary()["deals"] == 0
    assert any(e["event"] == "ai.failed" for e in flow.engine.audit.for_run(r.run_id))


def test_email_retry_then_success(settings, retry):
    db = Database(":memory:")
    email = SimulatedEmailSender(db, "demo", fail_first_n=2)
    flow = build_app(settings, db=db, retry=retry, email=email)
    r = flow.process(msg(**LEAD))
    out = flow.engine.decide(r.approvals[0], approve=True, decided_by="yaso")
    assert out["status"] == "executed" and email.calls == 3
    assert len(email.outbox()) == 1


def test_email_outage_dead_letters(settings, retry):
    db = Database(":memory:")
    email = SimulatedEmailSender(db, "demo", always_fail=True)
    flow = build_app(settings, db=db, retry=retry, email=email)
    r = flow.process(msg(**LEAD))
    out = flow.engine.decide(r.approvals[0], approve=True, decided_by="yaso")
    assert out["status"] == "failed"
    assert db.one("SELECT COUNT(*) AS n FROM dead_letters")["n"] == 1
    assert flow.engine.get_run(r.run_id)["status"] == "failed"


def test_refund_cap_enforced_even_after_approval(flow):
    r = flow.process(
        msg(**{**BILLING, "external_id": "b2", "body": "Charged on invoice, refund $9,000 please"})
    )
    refund = next(p for p in flow.engine.approvals.pending() if p["action"] == "issue_refund")
    out = flow.engine.decide(refund["id"], approve=True, decided_by="yaso")
    assert out["status"] == "failed"
    assert flow.db.query("SELECT * FROM sim_refunds") == []
    assert r.run_id


def test_crash_in_workflow_is_recorded(flow):
    def boom(*_):
        raise RuntimeError("bug")

    flow.triage.classifier.classify = boom
    r = flow.process(msg(**LEAD))
    assert r.status == RunStatus.FAILED
    assert flow.engine.get_run(r.run_id)["status"] == "failed"
