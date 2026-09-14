from fastapi.testclient import TestClient

from agent_commons.config import settings
from agent_commons.db import Base, engine
from agent_commons.main import app


def setup_function() -> None:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    settings.a2a_default_agent = None


def _register(client: TestClient, name: str = "atlas") -> dict[str, str]:
    result = client.post(
        "/api/v1/agents/register",
        json={"name": name, "description": "Research and analysis agent"},
    )
    assert result.status_code == 201
    return {"Authorization": f"Bearer {result.json()['api_key']}"}


def _configure_a2a(client: TestClient, headers: dict[str, str]) -> None:
    response = client.put(
        "/api/v1/agents/me/profile",
        headers=headers,
        json={
            "capabilities": ["research", "summarization"],
            "metadata": {
                "a2a": {
                    "url": "https://atlas.example/a2a",
                    "preferredTransport": "JSONRPC",
                    "version": "2.1.0",
                    "defaultInputModes": ["text/plain", "application/json"],
                    "defaultOutputModes": ["text/plain"],
                    "capabilities": {"streaming": True},
                    "skills": [
                        {
                            "id": "deep-research",
                            "name": "Deep Research",
                            "description": "Researches a topic and returns a sourced synthesis.",
                            "tags": ["research", "analysis"],
                            "examples": ["Research the current agent identity landscape."],
                        }
                    ],
                }
            },
        },
    )
    assert response.status_code == 200


def test_agent_card_requires_explicit_a2a_endpoint() -> None:
    with TestClient(app) as client:
        _register(client)
        response = client.get("/api/v1/agents/atlas/agent-card")
        assert response.status_code == 409
        assert "non-empty 'url'" in response.json()["detail"]


def test_publish_a2a_03_agent_card_from_structured_profile() -> None:
    with TestClient(app) as client:
        headers = _register(client)
        _configure_a2a(client, headers)

        response = client.get("/api/v1/agents/atlas/agent-card")
        assert response.status_code == 200
        card = response.json()
        assert card["protocolVersion"] == "0.3.0"
        assert card["name"] == "atlas"
        assert card["description"] == "Research and analysis agent"
        assert card["url"] == "https://atlas.example/a2a"
        assert card["preferredTransport"] == "JSONRPC"
        assert card["version"] == "2.1.0"
        assert card["capabilities"]["streaming"] is True
        assert card["defaultInputModes"] == ["text/plain", "application/json"]
        assert card["defaultOutputModes"] == ["text/plain"]
        assert card["skills"][0]["id"] == "deep-research"
        assert card["skills"][0]["tags"] == ["research", "analysis"]


def test_capabilities_become_basic_skills_when_explicit_skills_are_omitted() -> None:
    with TestClient(app) as client:
        headers = _register(client)
        response = client.put(
            "/api/v1/agents/me/profile",
            headers=headers,
            json={
                "capabilities": ["lead qualification"],
                "metadata": {"a2a": {"url": "https://atlas.example/a2a"}},
            },
        )
        assert response.status_code == 200

        card = client.get("/api/v1/agents/atlas/agent-card").json()
        assert card["skills"] == [
            {
                "id": "lead-qualification",
                "name": "lead qualification",
                "description": "Agent capability: lead qualification",
                "tags": ["lead qualification"],
                "examples": None,
                "inputModes": None,
                "outputModes": None,
            }
        ]


def test_well_known_card_is_opt_in_for_single_agent_deployments() -> None:
    with TestClient(app) as client:
        headers = _register(client)
        _configure_a2a(client, headers)

        missing = client.get("/.well-known/agent-card.json")
        assert missing.status_code == 404

        settings.a2a_default_agent = "atlas"
        response = client.get("/.well-known/agent-card.json")
        assert response.status_code == 200
        assert response.json()["name"] == "atlas"
        assert response.json()["url"] == "https://atlas.example/a2a"
