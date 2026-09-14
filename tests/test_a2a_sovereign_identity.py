import base58
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from agent_commons.a2a_identity import SOVEREIGN_IDENTITY_EXTENSION_URI
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


def _register(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/api/v1/agents/register",
        json={"name": "atlas", "description": "Persistent research agent"},
    )
    assert response.status_code == 201
    return {"Authorization": f"Bearer {response.json()['api_key']}"}


def _bind_identity(client: TestClient, headers: dict[str, str]) -> None:
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


def _configure(client: TestClient, headers: dict[str, str], publish: bool) -> None:
    response = client.put(
        "/api/v1/agents/me/profile",
        headers=headers,
        json={
            "capabilities": ["research"],
            "metadata": {
                "a2a": {
                    "url": "https://atlas.example/a2a",
                    "publishSovereignIdentity": publish,
                }
            },
        },
    )
    assert response.status_code == 200


def test_sovereign_identity_extension_is_explicit_opt_in() -> None:
    with TestClient(app) as client:
        headers = _register(client)
        _bind_identity(client, headers)
        _configure(client, headers, publish=False)

        card = client.get("/api/v1/agents/atlas/agent-card")
        assert card.status_code == 200
        assert card.json()["capabilities"]["extensions"] is None


def test_a2a_card_can_publish_portable_sovereign_identity_lineage() -> None:
    with TestClient(app) as client:
        headers = _register(client)
        _bind_identity(client, headers)
        _configure(client, headers, publish=True)

        response = client.get("/api/v1/agents/atlas/agent-card")
        assert response.status_code == 200
        extensions = response.json()["capabilities"]["extensions"]
        assert len(extensions) == 1
        extension = extensions[0]
        assert extension["uri"] == SOVEREIGN_IDENTITY_EXTENSION_URI
        assert extension["required"] is False

        params = extension["params"]
        assert params["rootFingerprint"].startswith("sha256:")
        assert params["identitySequence"] == 0
        assert params["currentControllerPublicKey"].startswith("z")
        assert params["lineage"]["format"] == "agent-commons-identity-lineage"
        assert params["lineage"]["version"] == 2
        assert params["lineage"]["root_fingerprint"] == params["rootFingerprint"]


def test_identity_publication_requires_bound_identity() -> None:
    with TestClient(app) as client:
        headers = _register(client)
        _configure(client, headers, publish=True)

        response = client.get("/api/v1/agents/atlas/agent-card")
        assert response.status_code == 409
        assert "Bind a cryptographic identity" in response.json()["detail"]
