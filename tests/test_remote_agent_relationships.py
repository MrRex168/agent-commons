import uuid

from fastapi.testclient import TestClient

from agent_commons.db import Base, SessionLocal, engine
from agent_commons.main import app
from agent_commons.relationship_models import RemoteAgentRelationship
from agent_commons.remote_models import RemoteAgentReference


def setup_function() -> None:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def _register(client: TestClient, name: str) -> tuple[dict, dict[str, str]]:
    response = client.post("/api/v1/agents/register", json={"name": name})
    assert response.status_code == 201
    body = response.json()
    return body, {"Authorization": f"Bearer {body['api_key']}"}


def _remote(*, name: str = "Nova", root: str = "sha256:nova") -> RemoteAgentReference:
    with SessionLocal() as db:
        reference = RemoteAgentReference(
            card_url=f"https://remote.example/{uuid.uuid4()}/agent-card.json",
            name=name,
            description="Remote agent",
            a2a_url="https://remote.example/a2a",
            preferred_transport="JSONRPC",
            root_fingerprint=root,
            current_controller_public_key="z6Mktest",
            identity_sequence=2,
            identity_verified=True,
            lineage={"format": "test"},
            card_snapshot={"name": name},
        )
        db.add(reference)
        db.commit()
        db.refresh(reference)
        db.expunge(reference)
        return reference


def test_agent_can_follow_and_unfollow_remote_reference() -> None:
    with TestClient(app) as client:
        _, headers = _register(client, "local-agent")
        reference = _remote()
        created = client.post(
            "/api/v1/agents/relationships",
            headers=headers,
            json={"remote_reference_id": str(reference.id), "kind": "follow"},
        )
        assert created.status_code == 201
        body = created.json()
        assert body["remote_reference_id"] == str(reference.id)
        assert body["remote_name"] == "Nova"
        assert body["remote_root_fingerprint"] == "sha256:nova"
        assert body["remote_identity_verified"] is True
        assert len(client.get("/api/v1/agents/relationships", headers=headers).json()) == 1
        duplicate = client.post(
            "/api/v1/agents/relationships",
            headers=headers,
            json={"remote_reference_id": str(reference.id), "kind": "follow"},
        )
        assert duplicate.status_code == 409
        deleted = client.delete(
            f"/api/v1/agents/relationships/{body['id']}", headers=headers
        )
        assert deleted.status_code == 204
        assert client.get("/api/v1/agents/relationships", headers=headers).json() == []


def test_relationship_is_owned_by_local_agent() -> None:
    with TestClient(app) as client:
        owner, _ = _register(client, "owner-agent")
        _, other_headers = _register(client, "other-agent")
        reference = _remote(name="Atlas", root="sha256:atlas")
        with SessionLocal() as db:
            relationship = RemoteAgentRelationship(
                agent_id=uuid.UUID(owner["id"]),
                remote_reference_id=reference.id,
                kind="follow",
            )
            db.add(relationship)
            db.commit()
            db.refresh(relationship)
            relationship_id = relationship.id
        assert client.get(
            "/api/v1/agents/relationships", headers=other_headers
        ).json() == []
        response = client.delete(
            f"/api/v1/agents/relationships/{relationship_id}", headers=other_headers
        )
        assert response.status_code == 404


def test_relationship_requires_existing_remote_reference() -> None:
    with TestClient(app) as client:
        _, headers = _register(client, "local-agent")
        response = client.post(
            "/api/v1/agents/relationships",
            headers=headers,
            json={"remote_reference_id": str(uuid.uuid4()), "kind": "follow"},
        )
        assert response.status_code == 404
