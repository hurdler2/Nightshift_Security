"""PHASE 0 exit criteria: the API boots and answers health probes."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_liveness(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_request_id_is_echoed(client):
    response = client.get("/health", headers={"x-request-id": "abc-123"})
    assert response.headers["x-request-id"] == "abc-123"


def test_request_id_is_generated_when_absent(client):
    assert client.get("/health").headers["x-request-id"]


def test_readiness_does_not_claim_healthy_dependencies(client):
    """Spec §0.3: never report a component as healthy without checking it."""
    body = client.get("/ready").json()
    assert body["status"] == "degraded"
    assert set(body["checks"].values()) == {"unknown"}


def test_openapi_is_served(client):
    schema = client.get("/openapi.json").json()
    assert schema["info"]["title"] == "Nightshift Security API"
