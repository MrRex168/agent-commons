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


def _register(client: TestClient, name: str) -> dict[str, str]:
    response = client.post("/agents/register", json={"name": name})
    assert response.status_code == 201
    return {
        "api_key": response.json()["api_key"],
        "agent_id": response.json()["agent"]["id"],
    }


def test_agent_can_bind_cryptographic_identity() -> None:
    with TestClient(app) as client:
        registered = _register(client, "crypto-atlas")
        headers = {"Authorization": f"Bearer {registered['api_key']}"}
        private_key, public_key_multibase = _keypair()

        challenge = client.post(
            "/agents/me/identity/challenge",
            headers=headers,
            json={"public_key_multibase": public_key_multibase},
        )
        assert challenge.status_code == 200
        challenge_body = challenge.json()
        assert challenge_body["fingerprint"].startswith("sha256:")
        assert "operation:bind" in challenge_body["payload"]
        assert "agent-commons/identity-challenge/v1" in challenge_body["payload"]

        verify = client.post(
            "/agents/me/identity/verify",
            headers=headers,
            json={
                "challenge_id": challenge_body["challenge_id"],
                "signature_multibase": _signature(private_key, challenge_body["payload"]),
            },
        )
        assert verify.status_code == 200
        identity = verify.json()
        assert identity["public_key_multibase"] == public_key_multibase
        assert identity["fingerprint"] == challenge_body["fingerprint"]

        mine = client.get("/agents/me/identity", headers=headers)
        public = client.get("/agents/crypto-atlas/identity")
        assert mine.status_code == 200
        assert public.status_code == 200
        assert mine.json() == public.json() == identity


def test_invalid_signature_does_not_consume_challenge() -> None:
    with TestClient(app) as client:
        registered = _register(client, "crypto-retry")
        headers = {"Authorization": f"Bearer {registered['api_key']}"}
        private_key, public_key_multibase = _keypair()
        wrong_key, _ = _keypair()

        challenge = client.post(
            "/agents/me/identity/challenge",
            headers=headers,
            json={"public_key_multibase": public_key_multibase},
        ).json()

        rejected = client.post(
            "/agents/me/identity/verify",
            headers=headers,
            json={
                "challenge_id": challenge["challenge_id"],
                "signature_multibase": _signature(wrong_key, challenge["payload"]),
            },
        )
        assert rejected.status_code == 401

        accepted = client.post(
            "/agents/me/identity/verify",
            headers=headers,
            json={
                "challenge_id": challenge["challenge_id"],
                "signature_multibase": _signature(private_key, challenge["payload"]),
            },
        )
        assert accepted.status_code == 200


def test_verified_challenge_cannot_be_replayed() -> None:
    with TestClient(app) as client:
        registered = _register(client, "crypto-replay")
        headers = {"Authorization": f"Bearer {registered['api_key']}"}
        private_key, public_key_multibase = _keypair()
        challenge = client.post(
            "/agents/me/identity/challenge",
            headers=headers,
            json={"public_key_multibase": public_key_multibase},
        ).json()
        body = {
            "challenge_id": challenge["challenge_id"],
            "signature_multibase": _signature(private_key, challenge["payload"]),
        }

        first = client.post("/agents/me/identity/verify", headers=headers, json=body)
        replay = client.post("/agents/me/identity/verify", headers=headers, json=body)

        assert first.status_code == 200
        assert replay.status_code == 409


def test_bound_identity_cannot_be_replaced_without_rotation_protocol() -> None:
    with TestClient(app) as client:
        registered = _register(client, "crypto-fixed")
        headers = {"Authorization": f"Bearer {registered['api_key']}"}
        private_key, first_public_key = _keypair()
        challenge = client.post(
            "/agents/me/identity/challenge",
            headers=headers,
            json={"public_key_multibase": first_public_key},
        ).json()
        verify = client.post(
            "/agents/me/identity/verify",
            headers=headers,
            json={
                "challenge_id": challenge["challenge_id"],
                "signature_multibase": _signature(private_key, challenge["payload"]),
            },
        )
        assert verify.status_code == 200

        _, second_public_key = _keypair()
        rotation = client.post(
            "/agents/me/identity/challenge",
            headers=headers,
            json={"public_key_multibase": second_public_key},
        )
        assert rotation.status_code == 409


def test_same_sovereign_identity_cannot_bind_to_two_local_agents() -> None:
    with TestClient(app) as client:
        first = _register(client, "crypto-owner")
        second = _register(client, "crypto-copy")
        first_headers = {"Authorization": f"Bearer {first['api_key']}"}
        second_headers = {"Authorization": f"Bearer {second['api_key']}"}
        private_key, public_key_multibase = _keypair()

        challenge = client.post(
            "/agents/me/identity/challenge",
            headers=first_headers,
            json={"public_key_multibase": public_key_multibase},
        ).json()
        verify = client.post(
            "/agents/me/identity/verify",
            headers=first_headers,
            json={
                "challenge_id": challenge["challenge_id"],
                "signature_multibase": _signature(private_key, challenge["payload"]),
            },
        )
        assert verify.status_code == 200

        duplicate = client.post(
            "/agents/me/identity/challenge",
            headers=second_headers,
            json={"public_key_multibase": public_key_multibase},
        )
        assert duplicate.status_code == 409


def test_malformed_multikey_is_rejected() -> None:
    with TestClient(app) as client:
        registered = _register(client, "crypto-invalid")
        headers = {"Authorization": f"Bearer {registered['api_key']}"}
        response = client.post(
            "/agents/me/identity/challenge",
            headers=headers,
            json={"public_key_multibase": "znot-an-ed25519-multikey"},
        )
        assert response.status_code == 422
