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
    private = Ed25519PrivateKey.generate()
    raw = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return private, "z" + base58.b58encode(b"\xed\x01" + raw).decode("ascii")


def _signature(private: Ed25519PrivateKey, payload: str) -> str:
    return "z" + base58.b58encode(private.sign(payload.encode())).decode("ascii")


def _bind_root(client: TestClient):
    registered = client.post(
        "/api/v1/agents/register",
        json={"name": "atlas-portable"},
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
    return registered, headers, root_private, root_public


def test_rotated_and_recovered_identity_migrates_with_current_controller() -> None:
    with TestClient(app) as client:
        registered, headers, root_private, root_public = _bind_root(client)
        source_id = registered["agent"]["id"]

        recovery_private, recovery_public = _keypair()
        policy_challenge = client.post(
            "/api/v1/agents/me/identity/recovery-policy/challenge",
            headers=headers,
            json={"recovery_public_key_multibase": recovery_public},
        ).json()
        policy_complete = client.post(
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
        assert policy_complete.status_code == 200

        rotated_private, rotated_public = _keypair()
        rotation = client.post(
            "/api/v1/agents/me/identity/rotation/challenge",
            headers=headers,
            json={"new_public_key_multibase": rotated_public},
        ).json()
        rotated = client.post(
            "/api/v1/agents/me/identity/rotation/complete",
            headers=headers,
            json={
                "challenge_id": rotation["challenge_id"],
                "previous_signature_multibase": _signature(
                    root_private, rotation["payload"]
                ),
                "new_signature_multibase": _signature(
                    rotated_private, rotation["payload"]
                ),
            },
        )
        assert rotated.status_code == 200

        current_private, current_public = _keypair()
        recovery = client.post(
            "/api/v1/agents/me/identity/recovery/challenge",
            headers=headers,
            json={"new_public_key_multibase": current_public},
        ).json()
        recovered = client.post(
            "/api/v1/agents/me/identity/recovery/complete",
            headers=headers,
            json={
                "challenge_id": recovery["challenge_id"],
                "recovery_signature_multibase": _signature(
                    recovery_private, recovery["payload"]
                ),
                "new_signature_multibase": _signature(
                    current_private, recovery["payload"]
                ),
            },
        )
        assert recovered.status_code == 200
        assert recovered.json()["sequence"] == 2

        memory = client.put(
            "/api/v1/agents/me/memories/project",
            headers=headers,
            json={"value": "continue after migration"},
        )
        assert memory.status_code == 200

        signing = client.get(
            "/api/v1/agents/me/state/freshness/signing-payload",
            headers=headers,
        ).json()
        signed = client.post(
            "/api/v1/agents/me/state/freshness/signed-export",
            headers=headers,
            json={
                "payload": signing["payload"],
                "signature_multibase": _signature(root_private, signing["payload"]),
            },
        )
        assert signed.status_code == 200
        envelope = signed.json()

        lineage_response = client.get(
            "/api/v1/agents/me/identity/lineage",
            headers=headers,
        )
        assert lineage_response.status_code == 200
        lineage = lineage_response.json()
        root_fingerprint = lineage["root_fingerprint"]
        assert lineage["root_public_key_multibase"] == root_public
        assert lineage["current_public_key_multibase"] == current_public
        assert lineage["sequence"] == 2

        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)

        challenge_response = client.post(
            "/api/v1/agents/migrate/lineage/challenge",
            json={"envelope": envelope, "lineage": lineage},
        )
        assert challenge_response.status_code == 200
        challenge = challenge_response.json()

        completed = client.post(
            "/api/v1/agents/migrate/lineage/complete",
            json={
                "challenge_id": challenge["challenge_id"],
                "envelope": envelope,
                "lineage": lineage,
                "signature_multibase": _signature(
                    current_private, challenge["payload"]
                ),
            },
        )
        assert completed.status_code == 200
        result = completed.json()
        assert result["agent"]["id"] != source_id
        assert result["identity"]["fingerprint"] == root_fingerprint
        assert result["memories_restored"] == 1

        destination_headers = {"Authorization": f"Bearer {result['api_key']}"}
        rotation_state = client.get(
            "/api/v1/agents/me/identity/rotation",
            headers=destination_headers,
        ).json()
        assert rotation_state["root_public_key_multibase"] == root_public
        assert rotation_state["current_public_key_multibase"] == current_public
        assert rotation_state["sequence"] == 2

        exported_again = client.get(
            "/api/v1/agents/me/identity/lineage",
            headers=destination_headers,
        )
        assert exported_again.status_code == 200
        verified = client.post(
            "/api/v1/agents/identity/lineage/verify",
            json=exported_again.json(),
        )
        assert verified.status_code == 200
        assert verified.json()["current_public_key_multibase"] == current_public
        assert verified.json()["sequence"] == 2


def test_lineage_migration_rejects_old_controller_proof() -> None:
    with TestClient(app) as client:
        _, headers, root_private, _ = _bind_root(client)
        current_private, current_public = _keypair()
        rotation = client.post(
            "/api/v1/agents/me/identity/rotation/challenge",
            headers=headers,
            json={"new_public_key_multibase": current_public},
        ).json()
        assert client.post(
            "/api/v1/agents/me/identity/rotation/complete",
            headers=headers,
            json={
                "challenge_id": rotation["challenge_id"],
                "previous_signature_multibase": _signature(root_private, rotation["payload"]),
                "new_signature_multibase": _signature(current_private, rotation["payload"]),
            },
        ).status_code == 200

        signing = client.get(
            "/api/v1/agents/me/state/freshness/signing-payload",
            headers=headers,
        ).json()
        envelope = client.post(
            "/api/v1/agents/me/state/freshness/signed-export",
            headers=headers,
            json={
                "payload": signing["payload"],
                "signature_multibase": _signature(root_private, signing["payload"]),
            },
        ).json()
        lineage = client.get(
            "/api/v1/agents/me/identity/lineage",
            headers=headers,
        ).json()

        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        challenge = client.post(
            "/api/v1/agents/migrate/lineage/challenge",
            json={"envelope": envelope, "lineage": lineage},
        ).json()
        rejected = client.post(
            "/api/v1/agents/migrate/lineage/complete",
            json={
                "challenge_id": challenge["challenge_id"],
                "envelope": envelope,
                "lineage": lineage,
                "signature_multibase": _signature(root_private, challenge["payload"]),
            },
        )
        assert rejected.status_code == 401
