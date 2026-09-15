import uuid

from fastapi.testclient import TestClient

from agent_commons.db import Base, SessionLocal, engine
from agent_commons.main import app
from agent_commons.remote_models import RemoteAgentReference


def setup_function() -> None:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def _register(client: TestClient) -> dict[str, str]:
    response = client.post("/api/v1/agents/register", json={"name": "discoverer"})
    assert response.status_code == 201
    return {"Authorization": f"Bearer {response.json()['api_key']}"}


def _remote(name: str, description: str, *, verified: bool) -> None:
    with SessionLocal() as db:
        db.add(
            RemoteAgentReference(
                card_url=f"https://remote.example/{uuid.uuid4()}/agent-card.json",
                name=name,
                description=description,
                a2a_url=f"https://remote.example/{name.lower()}/a2a",
                preferred_transport="JSONRPC",
                root_fingerprint=f"sha256:{name.lower()}" if verified else None,
                current_controller_public_key="z6Mktest" if verified else None,
                identity_sequence=3 if verified else None,
                identity_verified=verified,
                lineage={"format": "test"} if verified else None,
                card_snapshot={"name": name},
            )
        )
        db.commit()


def test_searches_resolved_remote_agents() -> None:
    with TestClient(app) as client:
        headers = _register(client)
        _remote("Nova", "Research and planning agent", verified=True)
        _remote("Pixel", "Design agent", verified=False)
        response = client.get(
            "/api/v1/agents/discovery/remote?q=research", headers=headers
        )
        assert response.status_code == 200
        body = response.json()
        assert body["query"] == "research"
        assert [item["name"] for item in body["agents"]] == ["Nova"]
        assert body["agents"][0]["identity_verified"] is True


def test_verified_only_filters_unverified_agents() -> None:
    with TestClient(app) as client:
        headers = _register(client)
        _remote("Nova", "Operations agent", verified=True)
        _remote("Nomad", "Operations agent", verified=False)
        response = client.get(
            "/api/v1/agents/discovery/remote?q=operations&verified_only=true",
            headers=headers,
        )
        assert response.status_code == 200
        assert [item["name"] for item in response.json()["agents"]] == ["Nova"]


def test_remote_discovery_requires_authentication() -> None:
    with TestClient(app) as client:
        response = client.get("/api/v1/agents/discovery/remote?q=nova")
        assert response.status_code == 401
