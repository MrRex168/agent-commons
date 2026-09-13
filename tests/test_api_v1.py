from fastapi.testclient import TestClient

from agent_commons.db import Base, engine
from agent_commons.main import app


def setup_function() -> None:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def test_v1_agent_identity_round_trip() -> None:
    with TestClient(app) as client:
        register = client.post(
            "/api/v1/agents/register",
            json={"name": "v1-atlas", "description": "Versioned API agent"},
        )
        assert register.status_code == 201
        body = register.json()
        api_key = body["api_key"]

        me = client.get(
            "/api/v1/agents/me",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert me.status_code == 200
        assert me.json()["id"] == body["agent"]["id"]


def test_v1_structured_profile_is_available() -> None:
    with TestClient(app) as client:
        register = client.post("/api/v1/agents/register", json={"name": "v1-profile"})
        api_key = register.json()["api_key"]
        headers = {"Authorization": f"Bearer {api_key}"}

        update = client.put(
            "/api/v1/agents/me/profile",
            headers=headers,
            json={"capabilities": ["research", "coordination"], "runtime": "portable-runtime"},
        )
        assert update.status_code == 200

        public = client.get("/api/v1/agents/v1-profile/profile")
        assert public.status_code == 200
        assert public.json()["capabilities"] == ["research", "coordination"]


def test_legacy_identity_routes_remain_compatible() -> None:
    with TestClient(app) as client:
        register = client.post("/agents/register", json={"name": "legacy-agent"})
        assert register.status_code == 201
        api_key = register.json()["api_key"]

        me = client.get(
            "/agents/me",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert me.status_code == 200
        assert me.json()["name"] == "legacy-agent"
