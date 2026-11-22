"""Hosted environments must refuse to start without real secrets."""

import pytest

from flow.cli import main
from flow.deploy import ConfigError, check_required, secret_status
from flow.web.app import create_web_app

GOOD = {
    "FLOW_SECRET_KEY": "s" * 64,
    "FLOW_DEMO_PASSWORD": "staging-password-123",
    "FLOW_VOICE_TOOL_KEY": "v" * 64,
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for name in [*GOOD, "RETELL_API_KEY", "ANTHROPIC_API_KEY", "FLOW_ENV"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FLOW_DB_PATH", str(tmp_path / "t.db"))


def test_local_needs_nothing():
    check_required("local")


def test_staging_lists_every_missing_secret():
    with pytest.raises(ConfigError) as e:
        check_required("staging")
    msg = str(e.value)
    for name in GOOD:
        assert name in msg


def test_short_secret_rejected(monkeypatch):
    for k, v in GOOD.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("FLOW_SECRET_KEY", "short")
    with pytest.raises(ConfigError, match="shorter"):
        check_required("staging")


def test_status_never_contains_values(monkeypatch):
    for k, v in GOOD.items():
        monkeypatch.setenv(k, v)
    dump = repr(secret_status("staging"))
    for v in GOOD.values():
        assert v not in dump


def test_web_app_refuses_hosted_start_without_secrets(monkeypatch):
    monkeypatch.setenv("FLOW_ENV", "staging")
    with pytest.raises(ConfigError):
        create_web_app()


def test_staging_uses_voice_key_and_secure_cookies(monkeypatch):
    from fastapi.testclient import TestClient

    for k, v in GOOD.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("FLOW_ENV", "staging")
    c = TestClient(create_web_app(), base_url="https://testserver")
    # the old built-in demo key no longer works; the configured one does
    body = {"name": "check_availability", "args": {}, "call": {"call_id": "t1"}}
    bad = c.post(
        "/voice/tools/check_availability",
        json=body,
        headers={"Authorization": "Bearer demo-northside-key"},
    )
    assert bad.status_code == 401
    ok = c.post(
        "/voice/tools/check_availability",
        json=body,
        headers={"Authorization": f"Bearer {GOOD['FLOW_VOICE_TOOL_KEY']}"},
    )
    assert ok.status_code == 200
    r = c.post(
        "/login",
        data={"email": "owner@northside.example", "password": GOOD["FLOW_DEMO_PASSWORD"]},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "secure" in r.headers["set-cookie"].lower()
    # the default local password is not accepted
    r = TestClient(create_web_app(), base_url="https://testserver").post(
        "/login",
        data={"email": "owner@northside.example", "password": "flow-demo-owner"},
        follow_redirects=False,
    )
    assert r.status_code != 303


def test_production_does_not_seed_demo_tenants(monkeypatch):
    from fastapi.testclient import TestClient

    for k, v in GOOD.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("FLOW_ENV", "production")
    app = create_web_app()
    assert app.state.ctx.store.tenants() == []
    assert TestClient(app).get("/health").status_code == 200


def test_check_config_cli(monkeypatch, capsys):
    assert main(["check-config", "--env", "staging"]) == 1
    for k, v in GOOD.items():
        monkeypatch.setenv(k, v)
    assert main(["check-config", "--env", "staging"]) == 0
    out = capsys.readouterr().out
    assert "Ready" in out
    for v in GOOD.values():
        assert v not in out
