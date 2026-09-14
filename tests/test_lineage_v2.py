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
    public_key = "z" + base58.b58encode(b"\xed\x01" + public_bytes).decode("ascii")
    return private_key, public_key


def _signature(private_key: Ed25519PrivateKey, payload: str) -> str:
    return "z" + base58.b58encode(private_key.sign(payload.encode())).decode("ascii")


def _register_and_bind(client: TestClient):
    registered = client.post(
        "/api/v1/agents/register",
        json={"name": "lineage-v2-atlas"},
    ).json()
    headers = {"Authorization": f"Bearer {registered['api_key']}"}
    root_private, root_public = _keypair()
    challenge = client.post(
        "/api/v1/agents/me/identity/challenge",
        headers=headers,
        json={"public_key_multibase": root_public},
    ).json()
    response = client.post(
        "/api/v1/agents/me/identity/verify",
        headers=headers,
        json={
            "challenge_id": challenge["challenge_id"],
            "signature_multibase": _signature(root_private, challenge["payload"]),
        },
    )
    assert response.status_code == 200
    return headers, root_private


def _set_policy(
    client: TestClient,
    headers: dict[str, str],
    active_private: Ed25519PrivateKey,
    recovery_private: Ed25519PrivateKey,
    recovery_public: str,
) -> None:
    challenge = client.post(
        "/api/v1/agents/me/identity/recovery-policy/challenge",
        headers=headers,
        json={"recovery_public_key_multibase": recovery_public},
    )
    assert challenge.status_code == 200
    body = challenge.json()
    completed = client.post(
        "/api/v1/agents/me/identity/recovery-policy/complete",
        headers=headers,
        json={
            "challenge_id": body["challenge_id"],
            "active_signature_multibase": _signature(active_private, body["payload"]),
            "recovery_signature_multibase": _signature(
                recovery_private,
                body["payload"],
            ),
        },
    )
    assert completed.status_code == 200


def _recover(
    client: TestClient,
    headers: dict[str, str],
    recovery_private: Ed25519PrivateKey,
    new_private: Ed25519PrivateKey,
    new_public: str,
) -> None:
    challenge = client.post(
        "/api/v1/agents/me/identity/recovery/challenge",
        headers=headers,
        json={"new_public_key_multibase": new_public},
    )
    assert challenge.status_code == 200
    body = challenge.json()
    completed = client.post(
        "/api/v1/agents/me/identity/recovery/complete",
        headers=headers,
        json={
            "challenge_id": body["challenge_id"],
            "recovery_signature_multibase": _signature(
                recovery_private,
                body["payload"],
            ),
            "new_signature_multibase": _signature(new_private, body["payload"]),
        },
    )
    assert completed.status_code == 200


def test_lineage_v2_preserves_multiple_recovery_policy_revisions() -> None:
    with TestClient(app) as client:
        headers, root_private = _register_and_bind(client)

        recovery1_private, recovery1_public = _keypair()
        _set_policy(
            client,
            headers,
            root_private,
            recovery1_private,
            recovery1_public,
        )

        key1_private, key1_public = _keypair()
        _recover(client, headers, recovery1_private, key1_private, key1_public)

        recovery2_private, recovery2_public = _keypair()
        _set_policy(
            client,
            headers,
            key1_private,
            recovery2_private,
            recovery2_public,
        )

        key2_private, key2_public = _keypair()
        _recover(client, headers, recovery2_private, key2_private, key2_public)

        exported = client.get(
            "/api/v1/agents/me/identity/lineage",
            headers=headers,
        )
        assert exported.status_code == 200
        lineage = exported.json()

        assert lineage["version"] == 2
        assert [item["revision"] for item in lineage["recovery_policies"]] == [1, 2]
        assert lineage["recovery_policy"]["revision"] == 2
        assert [item["policy_revision"] for item in lineage["transitions"]] == [1, 2]
        assert lineage["current_public_key_multibase"] == key2_public

        verified = client.post(
            "/api/v1/agents/identity/lineage/verify",
            json=lineage,
        )
        assert verified.status_code == 200
        assert verified.json()["valid"] is True
        assert verified.json()["current_public_key_multibase"] == key2_public
        assert verified.json()["recovery_policy_revision"] == 2


def test_lineage_v2_rejects_missing_historical_policy_evidence() -> None:
    with TestClient(app) as client:
        headers, root_private = _register_and_bind(client)

        recovery1_private, recovery1_public = _keypair()
        _set_policy(
            client,
            headers,
            root_private,
            recovery1_private,
            recovery1_public,
        )
        key1_private, key1_public = _keypair()
        _recover(client, headers, recovery1_private, key1_private, key1_public)

        recovery2_private, recovery2_public = _keypair()
        _set_policy(
            client,
            headers,
            key1_private,
            recovery2_private,
            recovery2_public,
        )

        lineage = client.get(
            "/api/v1/agents/me/identity/lineage",
            headers=headers,
        ).json()
        lineage["recovery_policies"] = [
            item for item in lineage["recovery_policies"] if item["revision"] != 1
        ]

        rejected = client.post(
            "/api/v1/agents/identity/lineage/verify",
            json=lineage,
        )
        assert rejected.status_code == 422
        assert "exact portable policy evidence" in rejected.json()["detail"].lower()
