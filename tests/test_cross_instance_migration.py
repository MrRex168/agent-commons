import base58
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from agent_commons.db import Base, engine
from agent_commons.main import app


def setup_function() -> None:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def _keypair() -> tuple[Ed25519PrivateKey, str]:
    private_key = Ed25519PrivateKey.generate()
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    multikey = b"\xed\x01" + public_bytes
    return private_key, "z" + base58.b58encode(multikey).decode("ascii")


def _signature(private_key: Ed25519PrivateKey, payload: str) -> str:
    raw = private_key.sign(payload.encode("utf-8"))
    return "z" + base58.b58encode(raw).decode("ascii")


def _source_envelope(
    client: TestClient,
    name: str,
) -> tuple[dict, Ed25519PrivateKey, str]:
    registered = client.post("/api/v1/agents/register", json={"name": name})
    api_key = registered.json()["api_key"]
    source_id = registered.json()["agent"]["id"]
    headers = {"Authorization": f"Bearer {api_key}"}
    private_key, public_key_multibase = _keypair()

    challenge = client.post(
        "/api/v1/agents/me/identity/challenge",
        headers=headers,
        json={"public_key_multibase": public_key_multibase},
    ).json()
    verified = client.post(
        "/api/v1/agents/me/identity/verify",
        headers=headers,
        json={
            "challenge_id": challenge["challenge_id"],
            "signature_multibase": _signature(private_key, challenge["payload"]),
        },
    )
    assert verified.status_code == 200

    profile = client.put(
        "/api/v1/agents/me/profile",
        headers=headers,
        json={
            "description": "Migrating research agent",
            "capabilities": ["research", "synthesis"],
            "metadata": {"origin": "server-a"},
            "model_provider": "provider-a",
            "model_name": "model-a",
            "runtime": "runtime-a",
        },
    )
    assert profile.status_code == 200

    memory = client.put(
        "/api/v1/agents/me/memories/continuity-marker",
        headers=headers,
        json={"value": "portable across servers"},
    )
    assert memory.status_code == 200

    signing = client.get(
        "/api/v1/agents/me/state/signing-payload",
        headers=headers,
    ).json()
    exported = client.post(
        "/api/v1/agents/me/state/signed-export",
        headers=headers,
        json={
            "payload": signing["payload"],
            "signature_multibase": _signature(private_key, signing["payload"]),
        },
    )
    assert exported.status_code == 200
    return exported.json(), private_key, source_id


def _fresh_destination() -> None:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def test_agent_can_migrate_to_fresh_instance() -> None:
    with TestClient(app) as client:
        envelope, private_key, source_id = _source_envelope(client, "atlas-portable")
        source_fingerprint = envelope["fingerprint"]
        _fresh_destination()

        challenge = client.post(
            "/api/v1/agents/migrate/challenge",
            json={"envelope": envelope},
        )
        assert challenge.status_code == 200
        challenge_body = challenge.json()
        assert challenge_body["requested_name"] == "atlas-portable"

        completed = client.post(
            "/api/v1/agents/migrate/complete",
            json={
                "challenge_id": challenge_body["challenge_id"],
                "envelope": envelope,
                "signature_multibase": _signature(private_key, challenge_body["payload"]),
            },
        )
        assert completed.status_code == 200
        result = completed.json()
        assert result["agent"]["name"] == "atlas-portable"
        assert result["agent"]["id"] != source_id
        assert result["identity"]["fingerprint"] == source_fingerprint
        assert result["memories_restored"] == 1

        headers = {"Authorization": f"Bearer {result['api_key']}"}
        mine = client.get("/api/v1/agents/me", headers=headers)
        identity = client.get("/api/v1/agents/me/identity", headers=headers)
        profile = client.get("/api/v1/agents/me/profile", headers=headers)
        memories = client.get("/api/v1/agents/me/memories", headers=headers)

        assert mine.status_code == 200
        assert identity.json()["fingerprint"] == source_fingerprint
        assert profile.json()["metadata"] == {"origin": "server-a"}
        assert memories.json()[0]["value"] == "portable across servers"


def test_name_collision_requires_explicit_destination_name() -> None:
    with TestClient(app) as client:
        envelope, private_key, _ = _source_envelope(client, "atlas-collision")
        _fresh_destination()
        occupied = client.post("/api/v1/agents/register", json={"name": "atlas-collision"})
        assert occupied.status_code == 201

        collision = client.post(
            "/api/v1/agents/migrate/challenge",
            json={"envelope": envelope},
        )
        assert collision.status_code == 409

        challenge = client.post(
            "/api/v1/agents/migrate/challenge",
            json={"envelope": envelope, "requested_name": "atlas-collision-2"},
        )
        assert challenge.status_code == 200
        body = challenge.json()
        completed = client.post(
            "/api/v1/agents/migrate/complete",
            json={
                "challenge_id": body["challenge_id"],
                "envelope": envelope,
                "signature_multibase": _signature(private_key, body["payload"]),
            },
        )
        assert completed.status_code == 200
        assert completed.json()["agent"]["name"] == "atlas-collision-2"
        assert completed.json()["source_name"] == "atlas-collision"


def test_wrong_key_cannot_complete_migration() -> None:
    with TestClient(app) as client:
        envelope, _, _ = _source_envelope(client, "atlas-proof")
        wrong_key, _ = _keypair()
        _fresh_destination()

        challenge = client.post(
            "/api/v1/agents/migrate/challenge",
            json={"envelope": envelope},
        ).json()
        rejected = client.post(
            "/api/v1/agents/migrate/complete",
            json={
                "challenge_id": challenge["challenge_id"],
                "envelope": envelope,
                "signature_multibase": _signature(wrong_key, challenge["payload"]),
            },
        )
        assert rejected.status_code == 401


def test_completed_migration_challenge_cannot_be_replayed() -> None:
    with TestClient(app) as client:
        envelope, private_key, _ = _source_envelope(client, "atlas-replay")
        _fresh_destination()

        challenge = client.post(
            "/api/v1/agents/migrate/challenge",
            json={"envelope": envelope},
        ).json()
        request = {
            "challenge_id": challenge["challenge_id"],
            "envelope": envelope,
            "signature_multibase": _signature(private_key, challenge["payload"]),
        }
        first = client.post("/api/v1/agents/migrate/complete", json=request)
        replay = client.post("/api/v1/agents/migrate/complete", json=request)

        assert first.status_code == 200
        assert replay.status_code == 409
