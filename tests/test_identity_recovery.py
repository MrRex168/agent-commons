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
    public_key_multibase = "z" + base58.b58encode(multikey).decode("ascii")
    return private_key, public_key_multibase


def _signature(private_key: Ed25519PrivateKey, payload: str) -> str:
    signature = private_key.sign(payload.encode("utf-8"))
    return "z" + base58.b58encode(signature).decode("ascii")


def _register_and_bind(
    client: TestClient,
) -> tuple[dict[str, str], Ed25519PrivateKey, str]:
    registered = client.post(
        "/api/v1/agents/register",
        json={"name": "recoverable-atlas"},
    ).json()
    headers = {"Authorization": f"Bearer {registered['api_key']}"}
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


def _configure_recovery_policy(
    client: TestClient,
    headers: dict[str, str],
    active_private: Ed25519PrivateKey,
    recovery_private: Ed25519PrivateKey,
    recovery_public: str,
) -> dict:
    challenge = client.post(
        "/api/v1/agents/me/identity/recovery-policy/challenge",
        headers=headers,
        json={"recovery_public_key_multibase": recovery_public},
    )
    assert challenge.status_code == 200
    body = challenge.json()
    complete = client.post(
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
    assert complete.status_code == 200
    return complete.json()


def test_offline_recovery_replaces_lost_active_key() -> None:
    with TestClient(app) as client:
        headers, root_private, root_public = _register_and_bind(client)
        recovery_private, recovery_public = _keypair()
        policy = _configure_recovery_policy(
            client,
            headers,
            root_private,
            recovery_private,
            recovery_public,
        )
        assert policy["revision"] == 1

        replacement_private, replacement_public = _keypair()
        challenge = client.post(
            "/api/v1/agents/me/identity/recovery/challenge",
            headers=headers,
            json={"new_public_key_multibase": replacement_public},
        )
        assert challenge.status_code == 200
        body = challenge.json()
        assert body["sequence"] == 1

        recovered = client.post(
            "/api/v1/agents/me/identity/recovery/complete",
            headers=headers,
            json={
                "challenge_id": body["challenge_id"],
                "recovery_signature_multibase": _signature(
                    recovery_private,
                    body["payload"],
                ),
                "new_signature_multibase": _signature(
                    replacement_private,
                    body["payload"],
                ),
            },
        )
        assert recovered.status_code == 200
        recovered_body = recovered.json()
        assert recovered_body["current_public_key_multibase"] == replacement_public
        assert recovered_body["sequence"] == 1

        state = client.get(
            "/api/v1/agents/me/identity/rotation",
            headers=headers,
        ).json()
        assert state["root_public_key_multibase"] == root_public
        assert state["current_public_key_multibase"] == replacement_public
        assert state["sequence"] == 1

        next_private, next_public = _keypair()
        next_challenge = client.post(
            "/api/v1/agents/me/identity/rotation/challenge",
            headers=headers,
            json={"new_public_key_multibase": next_public},
        ).json()
        old_key_rejected = client.post(
            "/api/v1/agents/me/identity/rotation/complete",
            headers=headers,
            json={
                "challenge_id": next_challenge["challenge_id"],
                "previous_signature_multibase": _signature(
                    root_private,
                    next_challenge["payload"],
                ),
                "new_signature_multibase": _signature(
                    next_private,
                    next_challenge["payload"],
                ),
            },
        )
        assert old_key_rejected.status_code == 401


def test_recovery_rejects_wrong_authority_and_replay() -> None:
    with TestClient(app) as client:
        headers, root_private, _ = _register_and_bind(client)
        recovery_private, recovery_public = _keypair()
        _configure_recovery_policy(
            client,
            headers,
            root_private,
            recovery_private,
            recovery_public,
        )

        wrong_recovery_private, _ = _keypair()
        replacement_private, replacement_public = _keypair()
        challenge = client.post(
            "/api/v1/agents/me/identity/recovery/challenge",
            headers=headers,
            json={"new_public_key_multibase": replacement_public},
        ).json()

        rejected = client.post(
            "/api/v1/agents/me/identity/recovery/complete",
            headers=headers,
            json={
                "challenge_id": challenge["challenge_id"],
                "recovery_signature_multibase": _signature(
                    wrong_recovery_private,
                    challenge["payload"],
                ),
                "new_signature_multibase": _signature(
                    replacement_private,
                    challenge["payload"],
                ),
            },
        )
        assert rejected.status_code == 401

        request = {
            "challenge_id": challenge["challenge_id"],
            "recovery_signature_multibase": _signature(
                recovery_private,
                challenge["payload"],
            ),
            "new_signature_multibase": _signature(
                replacement_private,
                challenge["payload"],
            ),
        }
        accepted = client.post(
            "/api/v1/agents/me/identity/recovery/complete",
            headers=headers,
            json=request,
        )
        assert accepted.status_code == 200

        replay = client.post(
            "/api/v1/agents/me/identity/recovery/complete",
            headers=headers,
            json=request,
        )
        assert replay.status_code == 409


def test_recovery_policy_replacement_revokes_old_recovery_key() -> None:
    with TestClient(app) as client:
        headers, root_private, _ = _register_and_bind(client)
        first_private, first_public = _keypair()
        first = _configure_recovery_policy(
            client,
            headers,
            root_private,
            first_private,
            first_public,
        )
        assert first["revision"] == 1

        second_private, second_public = _keypair()
        second = _configure_recovery_policy(
            client,
            headers,
            root_private,
            second_private,
            second_public,
        )
        assert second["revision"] == 2

        replacement_private, replacement_public = _keypair()
        challenge = client.post(
            "/api/v1/agents/me/identity/recovery/challenge",
            headers=headers,
            json={"new_public_key_multibase": replacement_public},
        ).json()
        assert challenge["policy_revision"] == 2

        rejected = client.post(
            "/api/v1/agents/me/identity/recovery/complete",
            headers=headers,
            json={
                "challenge_id": challenge["challenge_id"],
                "recovery_signature_multibase": _signature(
                    first_private,
                    challenge["payload"],
                ),
                "new_signature_multibase": _signature(
                    replacement_private,
                    challenge["payload"],
                ),
            },
        )
        assert rejected.status_code == 401

        accepted = client.post(
            "/api/v1/agents/me/identity/recovery/complete",
            headers=headers,
            json={
                "challenge_id": challenge["challenge_id"],
                "recovery_signature_multibase": _signature(
                    second_private,
                    challenge["payload"],
                ),
                "new_signature_multibase": _signature(
                    replacement_private,
                    challenge["payload"],
                ),
            },
        )
        assert accepted.status_code == 200

        history = client.get(
            "/api/v1/agents/me/identity/recovery/history",
            headers=headers,
        )
        assert history.status_code == 200
        assert len(history.json()) == 1
        assert history.json()[0]["policy_revision"] == 2
