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
        json={"name": "rotating-atlas"},
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
            "signature_multibase": _signature(
                root_private,
                challenge["payload"],
            ),
        },
    )
    assert verified.status_code == 200
    return headers, root_private, root_public


def test_planned_rotation_preserves_root_and_advances_active_key() -> None:
    with TestClient(app) as client:
        headers, root_private, root_public = _register_and_bind(client)
        new_private, new_public = _keypair()

        initial = client.get(
            "/api/v1/agents/me/identity/rotation",
            headers=headers,
        )
        assert initial.status_code == 200
        initial_body = initial.json()
        assert initial_body["root_public_key_multibase"] == root_public
        assert initial_body["current_public_key_multibase"] == root_public
        assert initial_body["sequence"] == 0

        challenge = client.post(
            "/api/v1/agents/me/identity/rotation/challenge",
            headers=headers,
            json={"new_public_key_multibase": new_public},
        )
        assert challenge.status_code == 200
        challenge_body = challenge.json()
        assert challenge_body["sequence"] == 1
        assert "agent-commons/key-rotation/v1" in challenge_body["payload"]

        complete_body = {
            "challenge_id": challenge_body["challenge_id"],
            "previous_signature_multibase": _signature(
                root_private,
                challenge_body["payload"],
            ),
            "new_signature_multibase": _signature(
                new_private,
                challenge_body["payload"],
            ),
        }
        complete = client.post(
            "/api/v1/agents/me/identity/rotation/complete",
            headers=headers,
            json=complete_body,
        )
        assert complete.status_code == 200
        rotated = complete.json()
        assert rotated["root_public_key_multibase"] == root_public
        assert rotated["current_public_key_multibase"] == new_public
        assert rotated["root_fingerprint"] == initial_body["root_fingerprint"]
        assert rotated["sequence"] == 1

        replay = client.post(
            "/api/v1/agents/me/identity/rotation/complete",
            headers=headers,
            json=complete_body,
        )
        assert replay.status_code == 409

        root_identity = client.get(
            "/api/v1/agents/me/identity",
            headers=headers,
        ).json()
        assert root_identity["public_key_multibase"] == root_public
        assert root_identity["fingerprint"] == initial_body["root_fingerprint"]


def test_superseded_active_key_cannot_authorize_next_rotation() -> None:
    with TestClient(app) as client:
        headers, root_private, _ = _register_and_bind(client)
        second_private, second_public = _keypair()

        first = client.post(
            "/api/v1/agents/me/identity/rotation/challenge",
            headers=headers,
            json={"new_public_key_multibase": second_public},
        ).json()
        first_complete = client.post(
            "/api/v1/agents/me/identity/rotation/complete",
            headers=headers,
            json={
                "challenge_id": first["challenge_id"],
                "previous_signature_multibase": _signature(
                    root_private,
                    first["payload"],
                ),
                "new_signature_multibase": _signature(
                    second_private,
                    first["payload"],
                ),
            },
        )
        assert first_complete.status_code == 200

        third_private, third_public = _keypair()
        second = client.post(
            "/api/v1/agents/me/identity/rotation/challenge",
            headers=headers,
            json={"new_public_key_multibase": third_public},
        ).json()

        rejected = client.post(
            "/api/v1/agents/me/identity/rotation/complete",
            headers=headers,
            json={
                "challenge_id": second["challenge_id"],
                "previous_signature_multibase": _signature(
                    root_private,
                    second["payload"],
                ),
                "new_signature_multibase": _signature(
                    third_private,
                    second["payload"],
                ),
            },
        )
        assert rejected.status_code == 401

        accepted = client.post(
            "/api/v1/agents/me/identity/rotation/complete",
            headers=headers,
            json={
                "challenge_id": second["challenge_id"],
                "previous_signature_multibase": _signature(
                    second_private,
                    second["payload"],
                ),
                "new_signature_multibase": _signature(
                    third_private,
                    second["payload"],
                ),
            },
        )
        assert accepted.status_code == 200
        assert accepted.json()["sequence"] == 2


def test_root_signed_state_export_still_works_after_active_key_rotation() -> None:
    with TestClient(app) as client:
        headers, root_private, _ = _register_and_bind(client)
        active_private, active_public = _keypair()
        challenge = client.post(
            "/api/v1/agents/me/identity/rotation/challenge",
            headers=headers,
            json={"new_public_key_multibase": active_public},
        ).json()
        rotated = client.post(
            "/api/v1/agents/me/identity/rotation/complete",
            headers=headers,
            json={
                "challenge_id": challenge["challenge_id"],
                "previous_signature_multibase": _signature(
                    root_private,
                    challenge["payload"],
                ),
                "new_signature_multibase": _signature(
                    active_private,
                    challenge["payload"],
                ),
            },
        )
        assert rotated.status_code == 200

        signing_payload = client.get(
            "/api/v1/agents/me/state/signing-payload",
            headers=headers,
        )
        assert signing_payload.status_code == 200
        payload = signing_payload.json()["payload"]
        signed_export = client.post(
            "/api/v1/agents/me/state/signed-export",
            headers=headers,
            json={
                "payload": payload,
                "signature_multibase": _signature(root_private, payload),
            },
        )
        assert signed_export.status_code == 200
