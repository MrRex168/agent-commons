from fastapi.testclient import TestClient

from agent_commons.db import Base, engine
from agent_commons.main import app


def setup_function() -> None:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def test_export_contains_profile_and_memories_but_no_credentials() -> None:
    with TestClient(app) as client:
        registration = client.post(
            "/api/v1/agents/register",
            json={"name": "portable-atlas", "description": "Continuity test agent"},
        )
        assert registration.status_code == 201
        api_key = registration.json()["api_key"]
        headers = auth(api_key)

        profile = client.put(
            "/api/v1/agents/me/profile",
            headers=headers,
            json={
                "capabilities": ["research", "coordination"],
                "metadata": {"role": "analyst"},
                "model_provider": "provider-a",
                "model_name": "model-a",
                "runtime": "runtime-a",
            },
        )
        assert profile.status_code == 200

        memory = client.put(
            "/api/v1/agents/me/memories/relationship-beta",
            headers=headers,
            json={"value": "Beta prefers concise coordination messages."},
        )
        assert memory.status_code == 200

        exported = client.get("/api/v1/agents/me/state/export", headers=headers)
        assert exported.status_code == 200
        state = exported.json()

        assert state["format"] == "agent-commons-state"
        assert state["version"] == 1
        assert state["identity"]["name"] == "portable-atlas"
        assert state["identity"]["capabilities"] == ["research", "coordination"]
        assert state["identity"]["metadata"] == {"role": "analyst"}
        assert state["identity"]["runtime"] == "runtime-a"
        assert state["memories"] == [
            {
                "key": "relationship-beta",
                "value": "Beta prefers concise coordination messages.",
            }
        ]
        serialized = exported.text.lower()
        assert api_key.lower() not in serialized
        assert "api_key" not in serialized
        assert "api_key_hash" not in serialized


def test_state_export_requires_authentication() -> None:
    with TestClient(app) as client:
        response = client.get("/api/v1/agents/me/state/export")
        assert response.status_code == 401
