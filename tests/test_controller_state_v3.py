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
    signature = private_key.sign(payload.encode("utf-8"))
    return "z" + base58.b58encode(signature).decode("ascii")


def _register_and_bind(
    client: TestClient,
) -> tuple[dict[str, str], Ed25519PrivateKey, str]:
    registered = client.post(
        "/api/v1/agents/register",
        json={"name": "state-v3-atlas"},
    )
    assert registered.status_code == 201
    headers = {"Authorization": f"Bearer {registered.json()['api_key']}"}
    root_private, root_public = _keypair()
    challenge = client.post(
        "/api/v1/agents/me/identity/challenge",
        headers=headers,
        json={"public_key_multibase": root_public},
    ).json()
    verified = client.post(
        "/api/v1/agents/me/identity/verify",
        headers=headers,
        json={
            "challenge_id": challenge["challenge_id"],
            "signature_multibase": _signature(root_private, challenge["payload"]),
        },
    )
    assert verified.status_code == 200
    return headers, root_private, root_public


def _configure_recovery_and_recover(
    client: TestClient,
    headers: dict[str, str],
    active_private: Ed25519PrivateKey,
) -> tuple[Ed25519PrivateKey, str]:
    recovery_private, recovery_public = _keypair()
    policy_challenge = client.post(
        "/api/v1/agents/me/identity/recovery-policy/challenge",
        headers=headers,
        json={"recovery_public_key_multibase": recovery_public},
    ).json()
    policy = client.post(
        "/api/v1/agents/me/identity/recovery-policy/complete",
        headers=headers,
        json={
            "challenge_id": policy_challenge["challenge_id"],
            "active_signature_multibase": _signature(
                active_private,
                policy_challenge["payload"],
            ),
            "recovery_signature_multibase": _signature(
                recovery_private,
                policy_challenge["payload"],
            ),
        },
    )
    assert policy.status_code == 200

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
                recovery_private,
                recovery_challenge["payload"],
            ),
            "new_signature_multibase": _signature(
                replacement_private,
                recovery_challenge["payload"],
            ),
        },
    )
    assert recovered.status_code == 200
    return replacement_private, replacement_public


def test_recovered_current_key_can_sign_new_portable_state_without_root_key() -> None:
    with TestClient(app) as client:
        headers, root_private, root_public = _register_and_bind(client)
        current_private, current_public = _configure_recovery_and_recover(
            client,
            headers,
            root_private,
        )

        signing = client.get(
            "/api/v1/agents/me/state/controller/signing-payload",
            headers=headers,
        )
        assert signing.status_code == 200
        body = signing.json()
        assert body["controller_public_key_multibase"] == current_public
        assert body["identity_sequence"] == 1
        assert body["lineage"]["root_public_key_multibase"] == root_public
        assert "agent-commons/signed-state/v3" in body["payload"]

        exported = client.post(
            "/api/v1/agents/me/state/controller/signed-export",
            headers=headers,
            json={
                "payload": body["payload"],
                "signature_multibase": _signature(current_private, body["payload"]),
            },
        )
        assert exported.status_code == 200
        envelope = exported.json()
        assert envelope["version"] == 3
        assert envelope["controller_public_key_multibase"] == current_public

        verified = client.post(
            "/api/v1/agents/state/controller/verify",
            json={"envelope": envelope, "lineage": body["lineage"]},
        )
        assert verified.status_code == 200
        result = verified.json()
        assert result["valid"] is True
        assert result["controller_public_key_multibase"] == current_public
        assert result["identity_sequence"] == 1


def test_old_root_key_cannot_sign_v3_state_after_recovery() -> None:
    with TestClient(app) as client:
        headers, root_private, _ = _register_and_bind(client)
        _, _ = _configure_recovery_and_recover(client, headers, root_private)

        signing = client.get(
            "/api/v1/agents/me/state/controller/signing-payload",
            headers=headers,
        ).json()
        rejected = client.post(
            "/api/v1/agents/me/state/controller/signed-export",
            headers=headers,
            json={
                "payload": signing["payload"],
                "signature_multibase": _signature(root_private, signing["payload"]),
            },
        )
        assert rejected.status_code == 401
        assert "current-controller" in rejected.json()["detail"].lower()


def test_v3_verification_binds_state_to_exact_lineage() -> None:
    with TestClient(app) as client:
        headers, root_private, _ = _register_and_bind(client)
        current_private, _ = _configure_recovery_and_recover(client, headers, root_private)

        signing = client.get(
            "/api/v1/agents/me/state/controller/signing-payload",
            headers=headers,
        ).json()
        exported = client.post(
            "/api/v1/agents/me/state/controller/signed-export",
            headers=headers,
            json={
                "payload": signing["payload"],
                "signature_multibase": _signature(current_private, signing["payload"]),
            },
        ).json()

        tampered_lineage = signing["lineage"] | {"sequence": 2}
        rejected = client.post(
            "/api/v1/agents/state/controller/verify",
            json={"envelope": exported, "lineage": tampered_lineage},
        )
        assert rejected.status_code == 422
