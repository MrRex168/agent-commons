from fastapi.testclient import TestClient

from agent_commons.db import Base, engine
from agent_commons.main import app


def setup_function() -> None:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def test_register_and_restore_agent_identity() -> None:
    with TestClient(app) as client:
        register = client.post(
            "/agents/register",
            json={
                "name": "atlas-42",
                "description": "Research agent",
                "capabilities": "research, synthesis",
            },
        )

        assert register.status_code == 201
        body = register.json()
        api_key = body["api_key"]
        assert api_key.startswith("ac_")
        assert body["agent"]["name"] == "atlas-42"

        me = client.get(
            "/agents/me",
            headers={"Authorization": f"Bearer {api_key}"},
        )

        assert me.status_code == 200
        assert me.json()["id"] == body["agent"]["id"]
        assert me.json()["name"] == "atlas-42"


def test_invalid_agent_key_is_rejected() -> None:
    with TestClient(app) as client:
        response = client.get(
            "/agents/me",
            headers={"Authorization": "Bearer ac_invalid"},
        )

        assert response.status_code == 401


def test_duplicate_agent_name_is_rejected() -> None:
    with TestClient(app) as client:
        payload = {"name": "nova-7"}
        first = client.post("/agents/register", json=payload)
        second = client.post("/agents/register", json=payload)

        assert first.status_code == 201
        assert second.status_code == 409
