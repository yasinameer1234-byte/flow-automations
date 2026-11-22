import pytest

from flow.core.approvals import ApprovalError, ApprovalStore
from flow.core.audit import AuditLog, redact
from flow.core.db import Database
from flow.core.models import ProposedAction

from .conftest import LEAD, msg


def test_cannot_decide_twice(flow):
    r = flow.process(msg(**LEAD))
    flow.engine.decide(r.approvals[0], approve=True, decided_by="a")
    with pytest.raises(ApprovalError, match="already executed"):
        flow.engine.decide(r.approvals[0], approve=True, decided_by="b")
    assert len(flow.email.outbox()) == 1


def test_decider_required():
    store = ApprovalStore(Database(":memory:"), "t1")
    aid = store.request("run_x", ProposedAction(action="send_email", params={}, reason="r"))
    with pytest.raises(ApprovalError):
        store.decide(aid, approve=True, decided_by="")


def test_tenant_isolation():
    db = Database(":memory:")
    a = ApprovalStore(db, "tenant_a")
    b = ApprovalStore(db, "tenant_b")
    aid = a.request("run_x", ProposedAction(action="send_email", params={}, reason="r"))
    assert b.pending() == []
    with pytest.raises(ApprovalError):
        b.decide(aid, approve=True, decided_by="intruder")


def test_secrets_are_redacted_in_audit():
    db = Database(":memory:")
    log = AuditLog(db, "t")
    log.record("x", run_id="r", api_key="sk-123", nested={"password": "p", "ok": 1})
    detail = log.for_run("r")[0]["detail"]
    assert detail == {
        "api_key": "***redacted***",
        "nested": {"password": "***redacted***", "ok": 1},
    }
    assert redact({"Authorization": "Bearer x"}) == {"Authorization": "***redacted***"}
