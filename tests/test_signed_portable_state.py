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


def _register_and_bind(
    client: TestClient,
    name: str,
) -> tuple[dict[str, str], Ed25519PrivateKey]:
    registered = client.post("/api/v1/agents/register", json={"name": name})
    assert registered.status_code == 201
    api_key = registered.json()["api_key"]
    headers = {"Authorization": f"Bearer {api_key}"}
    private_key, public_key_multibase = _keypair()

    challenge = client.post(
        "/api/v1/agents/me/identity/challenge",
        headers=headers,
        json={"public_key_multibase": public_key_multibase},
    )
    assert challenge.status_code == 200
    challenge_body = challenge.json()

    verified = client.post(
        "/api/v1/agents/me/identity/verify",
        headers=headers,
        json={
            "challenge_id": challenge_body["challenge_id"],
            "signature_multibase": _signature(private_key, challenge_body["payload"]),
        },
    )
    assert verified.status_code == 200
    return headers, private_key


def test_signing_payload_requires_bound_identity() -> None:
    with TestClient(app) as client:
        registered = client.post("/api/v1/agents/register", json={"name": "unsigned-state"})
        headers = {"Authorization": f"Bearer {registered.json()['api_key']}"}

        response = client.get("/api/v1/agents/me/state/signing-payload", headers=headers)

        assert response.status_code == 409


def test_agent_can_create_and_verify_signed_portable_state() -> None:
    with TestClient(app) as client:
        headers, private_key = _register_and_bind(client, "signed-atlas")
        memory = client.put(
            "/api/v1/agents/me/memories/continuity-marker",
            headers=headers,
            json={"value": "survives runtime changes"},
        )
        assert memory.status_code == 200

        signing = client.get("/api/v1/agents/me/state/signing-payload", headers=headers)
        assert signing.status_code == 200
        signing_body = signing.json()
        assert signing_body["payload"].startswith("agent-commons/signed-state/v1\n")
        assert signing_body["state"]["memories"][0]["key"] == "continuity-marker"

        signature = _signature(private_key, signing_body["payload"])
        exported = client.post(
            "/api/v1/agents/me/state/signed-export",
            headers=headers,
            json={"payload": signing_body["payload"], "signature_multibase": signature},
        )
        assert exported.status_code == 200
        envelope = exported.json()
        assert envelope["format"] == "agent-commons-signed-state"
        assert envelope["version"] == 1
        assert envelope["fingerprint"] == signing_body["fingerprint"]
        assert envelope["public_key_multibase"] == signing_body["public_key_multibase"]

        verified = client.post("/api/v1/agents/state/verify", json=envelope)
        assert verified.status_code == 200
        assert verified.json()["valid"] is True
        assert verified.json()["state"]["identity"]["name"] == "signed-atlas"
        assert verified.json()["state"]["memories"][0] == {
            "key": "continuity-marker",
            "value": "survives runtime changes",
        }


def test_signed_export_rejects_wrong_signature() -> None:
    with TestClient(app) as client:
        headers, _ = _register_and_bind(client, "signed-wrong-key")
        wrong_key, _ = _keypair()
        signing = client.get(
            "/api/v1/agents/me/state/signing-payload",
            headers=headers,
        ).json()

        response = client.post(
            "/api/v1/agents/me/state/signed-export",
            headers=headers,
            json={
                "payload": signing["payload"],
                "signature_multibase": _signature(wrong_key, signing["payload"]),
            },
        )

        assert response.status_code == 401


def test_public_verifier_detects_tampered_signature() -> None:
    with TestClient(app) as client:
        headers, private_key = _register_and_bind(client, "signed-tamper")
        signing = client.get(
            "/api/v1/agents/me/state/signing-payload",
            headers=headers,
        ).json()
        valid_signature = _signature(private_key, signing["payload"])
        wrong_key, _ = _keypair()
        tampered_signature = _signature(wrong_key, signing["payload"])
        assert tampered_signature != valid_signature

        identity = client.get("/api/v1/agents/me/identity", headers=headers).json()
        response = client.post(
            "/api/v1/agents/state/verify",
            json={
                "format": "agent-commons-signed-state",
                "version": 1,
                "fingerprint": identity["fingerprint"],
                "public_key_multibase": identity["public_key_multibase"],
                "payload": signing["payload"],
                "signature_multibase": tampered_signature,
            },
        )

        assert response.status_code == 200
        assert response.json()["valid"] is False
