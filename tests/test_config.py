from flow.config import load_settings, load_tenant_policy


def test_tenant_policy_file(tmp_path):
    p = tmp_path / "t.toml"
    p.write_text(
        '[tenant]\nid="acme"\n[policy]\nmin_confidence=0.9\n'
        'approval_required=["issue_refund"]\nauto_acknowledge_support=true\n'
    )
    t = load_tenant_policy(p)
    assert t.tenant_id == "acme" and t.min_confidence == 0.9
    assert t.approval_required == ("issue_refund",) and t.auto_acknowledge_support


def test_defaults_are_safe(monkeypatch):
    monkeypatch.delenv("FLOW_AI_PROVIDER", raising=False)
    monkeypatch.setenv("FLOW_TENANT_CONFIG", "does-not-exist.toml")
    s = load_settings()
    assert s.ai_provider == "simulated"  # never calls a paid API unless asked
    assert "send_email" in s.tenant.approval_required


def test_repo_demo_tenant_parses():
    t = load_tenant_policy("config/tenants/demo.toml")
    assert t.tenant_id == "demo"


def test_auto_ack_policy_sends_without_approval(settings, retry):
    from dataclasses import replace

    from flow.app import build_app
    from flow.core.db import Database
    from flow.core.models import InboundMessage

    s = replace(
        settings,
        tenant=replace(
            settings.tenant, auto_acknowledge_support=True, approval_required=("issue_refund",)
        ),
    )
    flow = build_app(s, db=Database(":memory:"), retry=retry)
    r = flow.process(
        InboundMessage(
            source="email",
            sender_email="t@example.com",
            subject="Portal error",
            body="Login broken, error",
        )
    )
    assert r.approvals == [] and len(flow.email.outbox()) == 1
