from fastapi.testclient import TestClient

from flow.api.server import create_api

from .conftest import LEAD


def test_end_to_end_over_http(flow):
    client = TestClient(create_api(flow))
    assert client.get("/health").json()["status"] == "ok"

    r = client.post("/intake", json={"source": "web_form", **LEAD})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "awaiting_approval"

    pending = client.get("/approvals").json()
    assert len(pending) == 1
    aid = pending[0]["id"]
    assert (
        client.post(f"/approvals/{aid}/approve", json={"decided_by": "yaso"}).json()["status"]
        == "executed"
    )
    assert client.post(f"/approvals/{aid}/approve", json={"decided_by": "yaso"}).status_code == 409

    run = client.get(f"/runs/{body['run_id']}").json()
    assert run["status"] == "completed"
    assert any(e["event"] == "action.executed" for e in run["audit"])
    assert client.get("/crm/summary").json()["deals"] == 1


def test_invalid_payload_rejected(flow):
    client = TestClient(create_api(flow))
    assert client.post("/intake", json={"source": "email"}).status_code == 422
    assert client.get("/runs/nope").status_code == 404
