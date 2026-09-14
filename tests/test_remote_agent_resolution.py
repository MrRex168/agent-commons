import base58
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from agent_commons.a2a import A2AAgentCard
from agent_commons.a2a_identity import SOVEREIGN_IDENTITY_EXTENSION_URI
from agent_commons.db import Base, engine
from agent_commons.identity_crypto import identity_fingerprint
from agent_commons.main import app
from agent_commons.remote_resolution import _validate_card_url


def setup_function() -> None:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def _register(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/api/v1/agents/register",
        json={"name": "local-resolver"},
    )
    assert response.status_code == 201
    return {"Authorization": f"Bearer {response.json()['api_key']}"}


def _public_key() -> str:
    private_key = Ed25519PrivateKey.generate()
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return "z" + base58.b58encode(b"\xed\x01" + public_bytes).decode("ascii")


def _card(name: str = "nova", sovereign: bool = False) -> A2AAgentCard:
    capabilities: dict = {"streaming": True}
    if sovereign:
        public_key = _public_key()
        fingerprint = identity_fingerprint(public_key)
        lineage = {
            "format": "agent-commons-identity-lineage",
            "version": 2,
            "root_fingerprint": fingerprint,
            "root_public_key_multibase": public_key,
            "current_public_key_multibase": public_key,
            "sequence": 0,
            "recovery_policy": None,
            "recovery_policies": [],
            "transitions": [],
        }
        capabilities["extensions"] = [
            {
                "uri": SOVEREIGN_IDENTITY_EXTENSION_URI,
                "required": False,
                "params": {
                    "rootFingerprint": fingerprint,
                    "currentControllerPublicKey": public_key,
                    "identitySequence": 0,
                    "lineage": lineage,
                },
            }
        ]
    return A2AAgentCard.model_validate(
        {
            "protocolVersion": "0.3.0",
            "name": name,
            "description": "Remote research agent",
            "url": f"https://{name}.example/a2a",
            "preferredTransport": "JSONRPC",
            "version": "1.0.0",
            "capabilities": capabilities,
            "defaultInputModes": ["text/plain"],
            "defaultOutputModes": ["text/plain"],
            "skills": [],
        }
    )


def test_private_literal_agent_card_url_is_rejected() -> None:
    try:
        _validate_card_url("https://127.0.0.1/.well-known/agent-card.json")
    except ValueError as exc:
        assert "public IP" in str(exc)
    else:
        raise AssertionError("private Agent Card URL should be rejected")


def test_resolve_and_cache_standard_a2a_agent(monkeypatch) -> None:
    card = _card()
    monkeypatch.setattr(
        "agent_commons.remote_resolution._fetch_agent_card",
        lambda _url: card,
    )

    with TestClient(app) as client:
        headers = _register(client)
        response = client.post(
            "/api/v1/agents/remote/resolve",
            headers=headers,
            json={"agent_card_url": "https://directory.example/nova/card.json"},
        )
        assert response.status_code == 200
        remote = response.json()
        assert remote["name"] == "nova"
        assert remote["a2a_url"] == "https://nova.example/a2a"
        assert remote["identity_verified"] is False
        assert remote["root_fingerprint"] is None

        listed = client.get("/api/v1/agents/remote", headers=headers)
        assert listed.status_code == 200
        assert len(listed.json()) == 1


def test_resolve_verifies_sovereign_identity_extension(monkeypatch) -> None:
    card = _card(sovereign=True)
    monkeypatch.setattr(
        "agent_commons.remote_resolution._fetch_agent_card",
        lambda _url: card,
    )

    with TestClient(app) as client:
        headers = _register(client)
        response = client.post(
            "/api/v1/agents/remote/resolve",
            headers=headers,
            json={"agent_card_url": "https://directory.example/nova/card.json"},
        )
        assert response.status_code == 200
        remote = response.json()
        extension = card.capabilities.extensions[0]
        assert remote["identity_verified"] is True
        assert remote["root_fingerprint"] == extension.params["rootFingerprint"]
        assert remote["identity_sequence"] == 0
        assert remote["lineage"]["version"] == 2


def test_same_sovereign_agent_can_move_to_new_card_url(monkeypatch) -> None:
    card = _card(sovereign=True)
    monkeypatch.setattr(
        "agent_commons.remote_resolution._fetch_agent_card",
        lambda _url: card,
    )

    with TestClient(app) as client:
        headers = _register(client)
        first = client.post(
            "/api/v1/agents/remote/resolve",
            headers=headers,
            json={"agent_card_url": "https://server-a.example/nova/card.json"},
        )
        second = client.post(
            "/api/v1/agents/remote/resolve",
            headers=headers,
            json={"agent_card_url": "https://server-b.example/nova/card.json"},
        )
        assert first.status_code == 200
        assert second.status_code == 200
        assert second.json()["id"] == first.json()["id"]
        assert second.json()["card_url"] == "https://server-b.example/nova/card.json"

        listed = client.get("/api/v1/agents/remote", headers=headers)
        assert len(listed.json()) == 1
