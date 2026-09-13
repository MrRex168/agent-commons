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
        json={"name": "lineage-atlas"},
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
    return headers, root_private, root_public


def test_rotation_lineage_exports_and_verifies() -> None:
    with TestClient(app) as client:
        headers, root_private, root_public = _register_and_bind(client)
        new_private, new_public = _keypair()
        challenge = client.post(
            "/api/v1/agents/me/identity/rotation/challenge",
            headers=headers,
            json={"new_public_key_multibase": new_public},
        ).json()
        completed = client.post(
            "/api/v1/agents/me/identity/rotation/complete",
            headers=headers,
            json={
                "challenge_id": challenge["challenge_id"],
                "previous_signature_multibase": _signature(
                    root_private, challenge["payload"]
                ),
                "new_signature_multibase": _signature(new_private, challenge["payload"]),
            },
        )
        assert completed.status_code == 200

        exported = client.get(
            "/api/v1/agents/me/identity/lineage",
            headers=headers,
        )
        assert exported.status_code == 200
        lineage = exported.json()
        assert lineage["root_public_key_multibase"] == root_public
        assert lineage["current_public_key_multibase"] == new_public
        assert lineage["sequence"] == 1
        assert lineage["transitions"][0]["type"] == "rotation"

        verified = client.post(
            "/api/v1/agents/identity/lineage/verify",
            json=lineage,
        )
        assert verified.status_code == 200
        assert verified.json()["valid"] is True
        assert verified.json()["current_public_key_multibase"] == new_public

        lineage["current_public_key_multibase"] = root_public
        rejected = client.post(
            "/api/v1/agents/identity/lineage/verify",
            json=lineage,
        )
        assert rejected.status_code == 422


def test_recovery_lineage_verifies_with_policy_evidence() -> None:
    with TestClient(app) as client:
        headers, root_private, _ = _register_and_bind(client)
        recovery_private, recovery_public = _keypair()
        policy_challenge = client.post(
            "/api/v1/agents/me/identity/recovery-policy/challenge",
            headers=headers,
            json={"recovery_public_key_multibase": recovery_public},
        ).json()
        policy_response = client.post(
            "/api/v1/agents/me/identity/recovery-policy/complete",
            headers=headers,
            json={
                "challenge_id": policy_challenge["challenge_id"],
                "active_signature_multibase": _signature(
                    root_private, policy_challenge["payload"]
                ),
                "recovery_signature_multibase": _signature(
                    recovery_private, policy_challenge["payload"]
                ),
            },
        )
        assert policy_response.status_code == 200

        replacement_private, replacement_public = _keypair()
        recovery_challenge = client.post(
            "/api/v1/agents/me/identity/recovery/challenge",
            headers=headers,
            json={"new_public_key_multibase": replacement_public},
        ).json()
        recovered = client.post(
            "/api/v1/agents/me/identity/recovery/complete",
            headers=headers,
            json={
                "challenge_id": recovery_challenge["challenge_id"],
                "recovery_signature_multibase": _signature(
                    recovery_private, recovery_challenge["payload"]
                ),
                "new_signature_multibase": _signature(
                    replacement_private, recovery_challenge["payload"]
                ),
            },
        )
        assert recovered.status_code == 200

        lineage = client.get(
            "/api/v1/agents/me/identity/lineage",
            headers=headers,
        ).json()
        assert lineage["recovery_policy"]["revision"] == 1
        assert lineage["transitions"][0]["type"] == "recovery"

        verified = client.post(
            "/api/v1/agents/identity/lineage/verify",
            json=lineage,
        )
        assert verified.status_code == 200
        assert verified.json()["current_public_key_multibase"] == replacement_public
