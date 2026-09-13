from fastapi.testclient import TestClient

from agent_commons.main import app

client = TestClient(app)


def test_health() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "agent-commons"}


def test_api_v1_health() -> None:
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "agent-commons",
        "api_version": "v1",
    }
