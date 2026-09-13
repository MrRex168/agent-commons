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
) -> tuple[dict[str, str], Ed25519PrivateKey]:
    registered = client.post(
        "/api/v1/agents/register",
        json={"name": "fresh-atlas"},
    )
    assert registered.status_code == 201
    headers = {"Authorization": f"Bearer {registered.json()['api_key']}"}
    private_key, public_key = _keypair()
    challenge = client.post(
        "/api/v1/agents/me/identity/challenge",
        headers=headers,
        json={"public_key_multibase": public_key},
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
    return headers, private_key


def _fresh_envelope(
    client: TestClient,
    headers: dict[str, str],
    private_key: Ed25519PrivateKey,
) -> dict:
    signing = client.get(
        "/api/v1/agents/me/state/freshness/signing-payload",
        headers=headers,
    )
    assert signing.status_code == 200
    body = signing.json()
    exported = client.post(
        "/api/v1/agents/me/state/freshness/signed-export",
        headers=headers,
        json={
            "payload": body["payload"],
            "signature_multibase": _signature(private_key, body["payload"]),
        },
    )
    assert exported.status_code == 200
    return exported.json()


def test_fresh_signed_state_uses_monotonic_sequence() -> None:
    with TestClient(app) as client:
        headers, private_key = _register_and_bind(client)
        first = _fresh_envelope(client, headers, private_key)
        second = _fresh_envelope(client, headers, private_key)

        assert first["version"] == 2
        assert first["state_sequence"] == 1
        assert second["state_sequence"] == 2
        assert "agent-commons/signed-state/v2" in first["payload"]

        verified = client.post(
            "/api/v1/agents/state/freshness/verify",
            json=second,
        )
        assert verified.status_code == 200
        assert verified.json()["valid"] is True
        assert verified.json()["state_sequence"] == 2


def test_destination_rejects_older_signed_state_after_newer_observation() -> None:
    with TestClient(app) as client:
        headers, private_key = _register_and_bind(client)
        old_envelope = _fresh_envelope(client, headers, private_key)
        new_envelope = _fresh_envelope(client, headers, private_key)

        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)

        newer = client.post(
            "/api/v1/agents/migrate/challenge",
            json={"envelope": new_envelope},
        )
        assert newer.status_code == 200

        rollback = client.post(
            "/api/v1/agents/migrate/challenge",
            json={"envelope": old_envelope},
        )
        assert rollback.status_code == 409
        assert "rollback detected" in rollback.json()["detail"].lower()


def test_legacy_state_is_rejected_after_freshness_aware_state_seen() -> None:
    with TestClient(app) as client:
        headers, private_key = _register_and_bind(client)

        legacy_payload = client.get(
            "/api/v1/agents/me/state/signing-payload",
            headers=headers,
        ).json()["payload"]
        legacy = client.post(
            "/api/v1/agents/me/state/signed-export",
            headers=headers,
            json={
                "payload": legacy_payload,
                "signature_multibase": _signature(private_key, legacy_payload),
            },
        )
        assert legacy.status_code == 200

        fresh = _fresh_envelope(client, headers, private_key)

        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)

        seen = client.post(
            "/api/v1/agents/migrate/challenge",
            json={"envelope": fresh},
        )
        assert seen.status_code == 200

        rejected = client.post(
            "/api/v1/agents/migrate/challenge",
            json={"envelope": legacy.json()},
        )
        assert rejected.status_code == 409
        assert "legacy signed state" in rejected.json()["detail"].lower()
