import pytest
from fastapi.testclient import TestClient

from api.main import app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def test_health(client):
    res = client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert "models" in body


def test_security_headers_present(client):
    res = client.get("/health")
    assert res.headers["X-Content-Type-Options"] == "nosniff"
    assert res.headers["X-Frame-Options"] == "DENY"
    assert "Content-Security-Policy" in res.headers


def test_orchestrate_happy_path(client):
    res = client.post("/orchestrate", json={"task": "What is 2+2?", "mode": "single"})
    assert res.status_code == 200
    body = res.json()
    assert body["final_answer"]
    assert body["decision_log"]["run_id"] == body["run_id"]


def test_orchestrate_rejects_empty_task(client):
    res = client.post("/orchestrate", json={"task": "   ", "mode": "single"})
    assert res.status_code == 422


def test_run_fetch_roundtrip(client):
    res = client.post("/orchestrate", json={"task": "hi", "mode": "single"})
    run_id = res.json()["run_id"]
    fetched = client.get(f"/runs/{run_id}")
    assert fetched.status_code == 200
    assert fetched.json()["run_id"] == run_id
