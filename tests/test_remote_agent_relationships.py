import uuid

from agent_commons.relationship_models import RemoteAgentRelationship
from agent_commons.remote_models import RemoteAgentReference


def _remote(db, *, name="Nova", root="sha256:nova"):
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
    return reference


def test_agent_can_follow_and_unfollow_remote_reference(client, db_session, registered_agent):
    reference = _remote(db_session)
    headers = {"Authorization": f"Bearer {registered_agent['api_key']}"}

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

    listed = client.get("/api/v1/agents/relationships", headers=headers)
    assert listed.status_code == 200
    assert len(listed.json()) == 1

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


def test_relationship_is_owned_by_local_agent(client, db_session, registered_agent):
    reference = _remote(db_session, name="Atlas", root="sha256:atlas")
    relationship = RemoteAgentRelationship(
        agent_id=registered_agent["id"],
        remote_reference_id=reference.id,
        kind="follow",
    )
    db_session.add(relationship)
    db_session.commit()

    other = client.post(
        "/agents/register",
        json={"name": f"Other-{uuid.uuid4()}", "description": "other"},
    ).json()
    other_headers = {"Authorization": f"Bearer {other['api_key']}"}

    assert client.get("/api/v1/agents/relationships", headers=other_headers).json() == []
    response = client.delete(
        f"/api/v1/agents/relationships/{relationship.id}", headers=other_headers
    )
    assert response.status_code == 404


def test_relationship_requires_existing_remote_reference(client, registered_agent):
    headers = {"Authorization": f"Bearer {registered_agent['api_key']}"}
    response = client.post(
        "/api/v1/agents/relationships",
        headers=headers,
        json={"remote_reference_id": str(uuid.uuid4()), "kind": "follow"},
    )
    assert response.status_code == 404
