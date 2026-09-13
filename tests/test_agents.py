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


def test_structured_profile_can_be_updated_and_read_publicly() -> None:
    with TestClient(app) as client:
        register = client.post("/agents/register", json={"name": "atlas-profile"})
        assert register.status_code == 201
        api_key = register.json()["api_key"]
        headers = {"Authorization": f"Bearer {api_key}"}

        update = client.put(
            "/agents/me/profile",
            headers=headers,
            json={
                "description": "Portable research agent",
                "capabilities": ["research", "web-search", "summarization"],
                "metadata": {"languages": ["en", "zh"], "region": "global"},
                "model_provider": "provider-agnostic",
                "model_name": "replaceable",
                "runtime": "custom-agent-runtime",
            },
        )

        assert update.status_code == 200
        profile = update.json()
        assert profile["capabilities"] == ["research", "web-search", "summarization"]
        assert profile["metadata"]["languages"] == ["en", "zh"]
        assert profile["runtime"] == "custom-agent-runtime"

        public = client.get("/agents/atlas-profile/profile")
        assert public.status_code == 200
        assert public.json() == profile


def test_structured_profile_defaults_are_stable() -> None:
    with TestClient(app) as client:
        register = client.post("/agents/register", json={"name": "blank-profile"})
        api_key = register.json()["api_key"]
        response = client.get(
            "/agents/me/profile",
            headers={"Authorization": f"Bearer {api_key}"},
        )

        assert response.status_code == 200
        assert response.json()["capabilities"] == []
        assert response.json()["metadata"] == {}
        assert response.json()["model_provider"] is None


def test_structured_profile_rejects_excessive_capabilities() -> None:
    with TestClient(app) as client:
        register = client.post("/agents/register", json={"name": "limit-profile"})
        api_key = register.json()["api_key"]
        response = client.put(
            "/agents/me/profile",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"capabilities": [f"cap-{index}" for index in range(51)]},
        )

        assert response.status_code == 422


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
