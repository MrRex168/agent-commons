import uuid

from fastapi.testclient import TestClient

from agent_commons.db import Base, SessionLocal, engine
from agent_commons.main import app
from agent_commons.remote_models import RemoteAgentReference


def setup_function() -> None:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def _register(client: TestClient) -> dict[str, str]:
    response = client.post("/api/v1/agents/register", json={"name": "skill-seeker"})
    assert response.status_code == 201
    return {"Authorization": f"Bearer {response.json()['api_key']}"}


def _remote(name: str, skill_name: str, tags: list[str], *, verified: bool) -> None:
    with SessionLocal() as db:
        db.add(
            RemoteAgentReference(
                card_url=f"https://remote.example/{uuid.uuid4()}/agent-card.json",
                name=name,
                description=f"{name} remote agent",
                a2a_url=f"https://remote.example/{name.lower()}/a2a",
                preferred_transport="JSONRPC",
                root_fingerprint=f"sha256:{name.lower()}" if verified else None,
                current_controller_public_key="z6Mktest" if verified else None,
                identity_sequence=1 if verified else None,
                identity_verified=verified,
                lineage={"format": "test"} if verified else None,
                card_snapshot={
                    "name": name,
                    "skills": [
                        {
                            "id": skill_name.lower().replace(" ", "-"),
                            "name": skill_name,
                            "description": f"Can perform {skill_name}",
                            "tags": tags,
                        }
                    ],
                },
            )
        )
        db.commit()


def test_discovers_remote_agent_by_a2a_skill() -> None:
    with TestClient(app) as client:
        headers = _register(client)
        _remote("Nova", "Deep Research", ["research", "web"], verified=True)
        _remote("Pixel", "Image Design", ["design"], verified=False)
        response = client.get(
            "/api/v1/agents/discovery/remote?skill=research", headers=headers
        )
        assert response.status_code == 200
        body = response.json()
        assert body["query"] == ""
        assert body["skill"] == "research"
        assert [item["name"] for item in body["agents"]] == ["Nova"]
        assert body["agents"][0]["skills"][0]["name"] == "Deep Research"


def test_combines_text_skill_and_verified_filters() -> None:
    with TestClient(app) as client:
        headers = _register(client)
        _remote("Nova Research", "Lead Research", ["sales"], verified=True)
        _remote("Nomad Research", "Lead Research", ["sales"], verified=False)
        response = client.get(
            "/api/v1/agents/discovery/remote?q=research&skill=sales&verified_only=true",
            headers=headers,
        )
        assert response.status_code == 200
        assert [item["name"] for item in response.json()["agents"]] == ["Nova Research"]


def test_skill_filter_is_case_insensitive() -> None:
    with TestClient(app) as client:
        headers = _register(client)
        _remote("Ops", "CRM Automation", ["Sales Ops"], verified=True)
        response = client.get(
            "/api/v1/agents/discovery/remote?skill=SALES", headers=headers
        )
        assert response.status_code == 200
        assert [item["name"] for item in response.json()["agents"]] == ["Ops"]
