from copy import deepcopy

from fastapi.testclient import TestClient

from agent_commons.db import Base, engine
from agent_commons.main import app


def setup_function() -> None:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def register(client: TestClient, name: str) -> tuple[str, dict[str, str]]:
    response = client.post(
        "/api/v1/agents/register",
        json={"name": name, "description": "Continuity test agent"},
    )
    assert response.status_code == 201
    body = response.json()
    return body["agent"]["id"], auth(body["api_key"])


def build_export(client: TestClient, headers: dict[str, str]) -> dict:
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
    return exported.json()


def test_export_contains_profile_and_memories_but_no_credentials() -> None:
    with TestClient(app) as client:
        _, headers = register(client, "portable-atlas")
        state = build_export(client, headers)

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
        serialized = str(state).lower()
        assert "api_key" not in serialized
        assert "api_key_hash" not in serialized


def test_state_export_requires_authentication() -> None:
    with TestClient(app) as client:
        response = client.get("/api/v1/agents/me/state/export")
        assert response.status_code == 401


def test_restore_recovers_profile_and_missing_memories() -> None:
    with TestClient(app) as client:
        _, headers = register(client, "restore-atlas")
        state = build_export(client, headers)

        changed_profile = client.put(
            "/api/v1/agents/me/profile",
            headers=headers,
            json={
                "capabilities": ["temporary"],
                "metadata": {"role": "changed"},
                "model_provider": "provider-b",
                "model_name": "model-b",
                "runtime": "runtime-b",
            },
        )
        assert changed_profile.status_code == 200

        existing = client.put(
            "/api/v1/agents/me/memories/relationship-beta",
            headers=headers,
            json={"value": "Newer local value"},
        )
        assert existing.status_code == 200
        state["memories"].append({"key": "restored-only", "value": "Recovered memory"})

        restored = client.post(
            "/api/v1/agents/me/state/restore",
            headers=headers,
            json=state,
        )
        assert restored.status_code == 200
        assert restored.json() == {
            "profile_updated": True,
            "memories_created": 1,
            "memories_updated": 0,
            "memories_skipped": 1,
        }

        profile = client.get("/api/v1/agents/me/profile", headers=headers).json()
        assert profile["capabilities"] == ["research", "coordination"]
        assert profile["metadata"] == {"role": "analyst"}
        assert profile["runtime"] == "runtime-a"

        memories = client.get("/api/v1/agents/me/memories", headers=headers).json()
        values = {item["key"]: item["value"] for item in memories}
        assert values["relationship-beta"] == "Newer local value"
        assert values["restored-only"] == "Recovered memory"


def test_restore_can_explicitly_overwrite_existing_memories() -> None:
    with TestClient(app) as client:
        _, headers = register(client, "overwrite-atlas")
        state = build_export(client, headers)

        client.put(
            "/api/v1/agents/me/memories/relationship-beta",
            headers=headers,
            json={"value": "Local value"},
        )
        restored = client.post(
            "/api/v1/agents/me/state/restore?overwrite_memories=true",
            headers=headers,
            json=state,
        )
        assert restored.status_code == 200
        assert restored.json()["memories_updated"] == 1

        memories = client.get("/api/v1/agents/me/memories", headers=headers).json()
        values = {item["key"]: item["value"] for item in memories}
        assert values["relationship-beta"] == "Beta prefers concise coordination messages."


def test_restore_rejects_state_for_another_agent() -> None:
    with TestClient(app) as client:
        _, atlas_headers = register(client, "identity-atlas")
        state = build_export(client, atlas_headers)
        _, beta_headers = register(client, "identity-beta")

        response = client.post(
            "/api/v1/agents/me/state/restore",
            headers=beta_headers,
            json=state,
        )
        assert response.status_code == 409


def test_restore_requires_authentication() -> None:
    with TestClient(app) as client:
        _, headers = register(client, "auth-atlas")
        state = build_export(client, headers)
        response = client.post("/api/v1/agents/me/state/restore", json=state)
        assert response.status_code == 401


def test_restore_rejects_unknown_fields_and_invalid_format() -> None:
    with TestClient(app) as client:
        _, headers = register(client, "strict-atlas")
        state = build_export(client, headers)

        with_extra = deepcopy(state)
        with_extra["api_key"] = "must-not-be-accepted"
        extra_response = client.post(
            "/api/v1/agents/me/state/restore",
            headers=headers,
            json=with_extra,
        )
        assert extra_response.status_code == 422

        invalid_format = deepcopy(state)
        invalid_format["format"] = "other-state-format"
        format_response = client.post(
            "/api/v1/agents/me/state/restore",
            headers=headers,
            json=invalid_format,
        )
        assert format_response.status_code == 422
