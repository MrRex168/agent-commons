from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event
from time import monotonic, sleep

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from test_lineage_v2 import _keypair, _recover, _register_and_bind, _set_policy, _signature

from agent_commons import remote_resolution
from agent_commons.a2a import A2AAgentCard
from agent_commons.a2a_identity import (
    SOVEREIGN_ENDPOINT_EXTENSION_URI,
    SOVEREIGN_IDENTITY_EXTENSION_URI,
)
from agent_commons.db import Base, SessionLocal, engine
from agent_commons.endpoint_authorization import EndpointAuthorization
from agent_commons.identity_crypto import identity_fingerprint
from agent_commons.main import app
from agent_commons.remote_models import RemoteAgentReference
from agent_commons.remote_resolution import _store_reference, _validate_card_url


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


CARD_URL = "https://directory.example/nova/card.json"
NEW_CARD_URL = "https://destination.example/nova/card.json"
ENDPOINT = "https://nova.example/a2a"
RESOLVE = "/api/v1/agents/remote/resolve"
RELATIONSHIPS = "/api/v1/agents/relationships"
DISCOVERY = "/api/v1/agents/discovery/remote"


def _root_lineage(public_key: str) -> dict:
    return {
        "format": "agent-commons-identity-lineage",
        "version": 2,
        "root_fingerprint": identity_fingerprint(public_key),
        "root_public_key_multibase": public_key,
        "current_public_key_multibase": public_key,
        "sequence": 0,
        "recovery_policy": None,
        "recovery_policies": [],
        "transitions": [],
    }


def _proof(
    signer: Ed25519PrivateKey,
    lineage: dict,
    *,
    card_url: str = CARD_URL,
    endpoint: str = ENDPOINT,
    counter: int = 1,
    issued_at: int | None = None,
    duration: int = 3600,
) -> dict:
    issued_at = int(datetime.now(UTC).timestamp()) if issued_at is None else issued_at
    payload = EndpointAuthorization(
        version=1,
        root_fingerprint=lineage["root_fingerprint"],
        controller_public_key_multibase=lineage["current_public_key_multibase"],
        identity_sequence=lineage["sequence"],
        location_sequence=counter,
        card_url=card_url,
        a2a_url=endpoint,
        transport="JSONRPC",
        issued_at=issued_at,
        expires_at=issued_at + duration,
    )
    return {
        "authorization": payload.model_dump(),
        "signature_multibase": _signature(signer, payload.signing_payload()),
    }


def _card(
    lineage: dict | None = None,
    proof: dict | None = None,
    *,
    endpoint: str = ENDPOINT,
) -> A2AAgentCard:
    capabilities: dict = {"streaming": True}
    if lineage is not None:
        params = {
            "rootFingerprint": lineage["root_fingerprint"],
            "currentControllerPublicKey": lineage["current_public_key_multibase"],
            "identitySequence": lineage["sequence"],
            "lineage": lineage,
        }
        uri = SOVEREIGN_IDENTITY_EXTENSION_URI
        if proof is not None:
            params["endpointAuthorization"] = proof
            uri = SOVEREIGN_ENDPOINT_EXTENSION_URI
        capabilities["extensions"] = [{"uri": uri, "required": False, "params": params}]
    return A2AAgentCard.model_validate(
        {
            "protocolVersion": "0.3.0",
            "name": "nova",
            "description": "Remote research agent",
            "url": endpoint,
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


def _resolve(client: TestClient, headers: dict, card_url: str = CARD_URL):
    return client.post(RESOLVE, headers=headers, json={"agent_card_url": card_url})


def test_sovereign_endpoint_authorization_and_move(monkeypatch) -> None:
    signer, key = _keypair()
    lineage = _root_lineage(key)
    cards = {CARD_URL: _card(lineage)}
    monkeypatch.setattr(remote_resolution, "_fetch_agent_card", cards.__getitem__)

    with TestClient(app) as client:
        headers = _register(client)
        assert _resolve(client, headers).status_code == 422
        assert client.get("/api/v1/agents/remote", headers=headers).json() == []

        initial = _card(lineage, _proof(signer, lineage))
        cards[CARD_URL] = initial
        first = _resolve(client, headers)
        assert first.status_code == 200
        remote = first.json()
        assert remote["identity_verified"] is True
        assert remote["lineage_verified"] is True
        assert remote["endpoint_authorization_status"] == "authorized"
        assert remote["lineage"]["version"] == 2
        follow = client.post(
            RELATIONSHIPS, headers=headers, json={"remote_reference_id": remote["id"]}
        )
        assert follow.status_code == 201

        cards[NEW_CARD_URL] = _card(lineage)
        assert _resolve(client, headers, NEW_CARD_URL).status_code == 422
        cards[NEW_CARD_URL] = initial
        assert _resolve(client, headers, NEW_CARD_URL).status_code == 422
        assert client.get(RELATIONSHIPS, headers=headers).json()[0]["remote_card_url"] == CARD_URL

        new_endpoint = "https://destination.example/a2a"
        cards[NEW_CARD_URL] = _card(
            lineage,
            _proof(signer, lineage, card_url=NEW_CARD_URL, endpoint=new_endpoint, counter=2),
            endpoint=new_endpoint,
        )
        moved = _resolve(client, headers, NEW_CARD_URL)
        assert moved.status_code == 200
        assert moved.json()["id"] == remote["id"]
        assert moved.json()["card_url"] == NEW_CARD_URL
        assert moved.json()["a2a_url"] == new_endpoint
        listed = client.get(RELATIONSHIPS, headers=headers).json()
        assert listed[0]["id"] == follow.json()["id"]
        assert listed[0]["remote_a2a_url"] == new_endpoint
        assert _resolve(client, headers).status_code == 409


@pytest.mark.parametrize(
    "invalid",
    [
        "wrong_signer",
        "malformed",
        "wrong_endpoint",
        "wrong_transport",
        "wrong_root",
        "wrong_sequence",
        "missing",
        "expired",
    ],
)
def test_invalid_endpoint_authorization_is_rejected(monkeypatch, invalid: str) -> None:
    signer, key = _keypair()
    lineage = _root_lineage(key)
    other_signer, other_key = _keypair()
    proof = _proof(other_signer if invalid == "wrong_signer" else signer, lineage)
    card = _card(lineage, proof)
    params = card.capabilities.extensions[0].params
    match invalid:
        case "malformed":
            params["endpointAuthorization"]["signature_multibase"] = "z0"
        case "wrong_endpoint":
            card.url = "https://not-authorized.example/a2a"
        case "wrong_transport":
            card.preferredTransport = "GRPC"
        case "wrong_root":
            params["rootFingerprint"] = identity_fingerprint(other_key)
        case "wrong_sequence":
            params["identitySequence"] = 1
        case "missing":
            del params["endpointAuthorization"]
        case "expired":
            params["endpointAuthorization"] = _proof(
                signer, lineage, issued_at=int(datetime.now(UTC).timestamp()) - 7200
            )
    monkeypatch.setattr(remote_resolution, "_fetch_agent_card", lambda _: card)
    with TestClient(app) as client:
        headers = _register(client)
        assert _resolve(client, headers).status_code == 422
        assert client.get("/api/v1/agents/remote", headers=headers).json() == []


def test_refresh_replay_conflict_expiry_and_renewal(monkeypatch) -> None:
    signer, key = _keypair()
    lineage = _root_lineage(key)
    cards = {CARD_URL: _card(lineage, _proof(signer, lineage))}
    monkeypatch.setattr(remote_resolution, "_fetch_agent_card", cards.__getitem__)
    with TestClient(app) as client:
        headers = _register(client)
        first = _resolve(client, headers).json()
        refresh = f"/api/v1/agents/remote/{first['id']}/refresh"
        follow = client.post(
            RELATIONSHIPS, headers=headers, json={"remote_reference_id": first["id"]}
        )
        assert follow.status_code == 201
        assert client.post(refresh, headers=headers).json() == first

        endpoint = "https://nova.example/updated"
        cards[CARD_URL] = _card(
            lineage, _proof(signer, lineage, endpoint=endpoint), endpoint=endpoint
        )
        assert client.post(refresh, headers=headers).status_code == 409

        with SessionLocal() as db:
            reference = db.scalar(select(RemoteAgentReference))
            reference.location_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            db.commit()

        hidden = client.get(RELATIONSHIPS, headers=headers).json()[0]
        assert hidden["remote_endpoint_authorization_status"] == "expired"
        assert hidden["remote_lineage_verified"] is True
        assert hidden["remote_identity_verified"] is False
        assert hidden["remote_card_url"] is None and hidden["remote_a2a_url"] is None
        assert client.get(f"{DISCOVERY}?verified_only=true", headers=headers).json()["agents"] == []
        pending = client.get(DISCOVERY, headers=headers).json()["agents"][0]
        assert pending["endpoint_authorization_status"] == "expired"
        assert pending["a2a_url"] is None
        assert client.post(refresh, headers=headers).status_code == 409

        cards[CARD_URL] = _card(
            lineage,
            _proof(signer, lineage, endpoint=endpoint, counter=2),
            endpoint=endpoint,
        )
        renewed = client.post(refresh, headers=headers)
        assert renewed.status_code == 200
        assert renewed.json()["id"] == first["id"]
        assert renewed.json()["a2a_url"] == endpoint


@pytest.mark.parametrize("transition", ["rotation", "recovery"])
def test_publisher_and_reference_follow_controller_transition(monkeypatch, transition) -> None:
    cards = {}
    monkeypatch.setattr(remote_resolution, "_fetch_agent_card", cards.__getitem__)
    with TestClient(app) as client:
        publisher, root = _register_and_bind(client)
        resolver = _register(client)
        card_path = "/api/v1/agents/lineage-v2-atlas/agent-card"
        lineage_path = "/api/v1/agents/me/identity/lineage"

        def publish(proof: dict):
            configured = client.put(
                "/api/v1/agents/me/profile",
                headers=publisher,
                json={
                    "metadata": {
                        "a2a": {
                            "url": ENDPOINT,
                            "publishSovereignIdentity": True,
                            "endpointAuthorization": proof,
                        }
                    }
                },
            )
            assert configured.status_code == 200
            return client.get(card_path)

        lineage = client.get(lineage_path, headers=publisher).json()
        old_proof = _proof(root, lineage, counter=7)
        first_card = publish(old_proof)
        assert first_card.status_code == 200
        assert [item["uri"] for item in first_card.json()["capabilities"]["extensions"]] == [
            SOVEREIGN_ENDPOINT_EXTENSION_URI
        ]
        cards[CARD_URL] = A2AAgentCard.model_validate(first_card.json())
        first = _resolve(client, resolver).json()
        follow = client.post(
            RELATIONSHIPS, headers=resolver, json={"remote_reference_id": first["id"]}
        ).json()
        next_signer, next_key = _keypair()

        if transition == "rotation":
            path = "/api/v1/agents/me/identity/rotation"
            challenge = client.post(
                f"{path}/challenge",
                headers=publisher,
                json={"new_public_key_multibase": next_key},
            ).json()
            completed = client.post(
                f"{path}/complete",
                headers=publisher,
                json={
                    "challenge_id": challenge["challenge_id"],
                    "previous_signature_multibase": _signature(root, challenge["payload"]),
                    "new_signature_multibase": _signature(next_signer, challenge["payload"]),
                },
            )
            assert completed.status_code == 200
        else:
            recovery_signer, recovery_key = _keypair()
            _set_policy(client, publisher, root, recovery_signer, recovery_key)
            _recover(client, publisher, recovery_signer, next_signer, next_key)

        assert client.get(card_path).status_code == 409
        new_lineage = client.get(lineage_path, headers=publisher).json()
        assert publish(_proof(root, new_lineage)).status_code == 409
        next_card = publish(_proof(next_signer, new_lineage))
        assert next_card.status_code == 200
        old_card = cards[CARD_URL]
        cards[CARD_URL] = A2AAgentCard.model_validate(next_card.json())
        refresh = f"/api/v1/agents/remote/{first['id']}/refresh"
        updated = client.post(refresh, headers=resolver)
        assert updated.status_code == 200
        assert updated.json()["id"] == first["id"]
        assert updated.json()["identity_sequence"] == 1
        assert client.get(RELATIONSHIPS, headers=resolver).json()[0]["id"] == follow["id"]
        cards[CARD_URL] = old_card
        assert client.post(refresh, headers=resolver).status_code == 409


def test_concurrent_refresh_cannot_overwrite_newer_authorization() -> None:
    signer, key = _keypair()
    lineage = _root_lineage(key)
    initial = _card(lineage, _proof(signer, lineage))
    older = _card(lineage, _proof(signer, lineage, counter=2))
    newer = _card(lineage, _proof(signer, lineage, counter=3))
    ready = Event()
    backend_pids = []

    def write_older(reference_id):
        with SessionLocal() as db:
            # Refresh has already loaded a potentially stale ORM object.
            reference = db.get(RemoteAgentReference, reference_id)
            backend_pids.append(db.scalar(text("SELECT pg_backend_pid()")))
            ready.set()
            with pytest.raises(HTTPException) as conflict:
                _store_reference(CARD_URL, older, db, expected_id=reference.id)
            assert conflict.value.status_code == 409

    with ThreadPoolExecutor(max_workers=1) as pool, SessionLocal() as writer:
        reference_id = _store_reference(CARD_URL, initial, writer).id
        writer.scalar(select(RemoteAgentReference).with_for_update())
        task = pool.submit(write_older, reference_id)
        try:
            assert ready.wait(5)
            with SessionLocal() as observer:
                deadline = monotonic() + 5
                while not observer.scalar(
                    text("SELECT pg_blocking_pids(:pid)"), {"pid": backend_pids[0]}
                ):
                    assert monotonic() < deadline, "Older request did not wait for PostgreSQL lock"
                    sleep(0.01)
            _store_reference(CARD_URL, newer, writer)
        finally:
            writer.rollback()
        task.result(timeout=5)

    with SessionLocal() as db:
        assert db.get(RemoteAgentReference, reference_id).location_sequence == 3


def test_concurrent_first_bindings_recheck_unique_root(monkeypatch) -> None:
    signer, key = _keypair()
    lineage = _root_lineage(key)
    barrier = Barrier(2)
    locked_reference = remote_resolution._locked_reference

    def wait_for_both_reads(*args):
        reference = locked_reference(*args)
        if reference is None:
            barrier.wait(timeout=5)
        return reference

    def bind(url, counter):
        with SessionLocal() as db:
            try:
                card = _card(lineage, _proof(signer, lineage, card_url=url, counter=counter))
                return _store_reference(url, card, db).id
            except HTTPException as exc:
                assert counter == 1 and exc.status_code == 409
                return None

    monkeypatch.setattr(remote_resolution, "_locked_reference", wait_for_both_reads)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(bind, CARD_URL, 1)
        second = pool.submit(bind, NEW_CARD_URL, 2)
        accepted = second.result(timeout=10)
        assert first.result(timeout=10) in (None, accepted)
    with SessionLocal() as db:
        rows = db.scalars(select(RemoteAgentReference)).all()
        assert len(rows) == 1
        assert (rows[0].id, rows[0].card_url, rows[0].location_sequence) == (
            accepted,
            NEW_CARD_URL,
            2,
        )


def test_legacy_follow_is_preserved_and_reauthorized_in_place(monkeypatch) -> None:
    signer, key = _keypair()
    lineage = _root_lineage(key)
    card = _card(lineage)
    monkeypatch.setattr(remote_resolution, "_fetch_agent_card", lambda _: card)
    with SessionLocal() as db:
        legacy = RemoteAgentReference(
            card_url=CARD_URL,
            a2a_url=ENDPOINT,
            name=card.name,
            preferred_transport="JSONRPC",
            root_fingerprint=lineage["root_fingerprint"],
            current_controller_public_key=key,
            identity_sequence=0,
            identity_verified=True,
            lineage=lineage,
            card_snapshot=card.model_dump(mode="json"),
        )
        db.add(legacy)
        db.commit()
        reference_id = str(legacy.id)

    with TestClient(app) as client:
        headers = _register(client)
        follow = client.post(
            RELATIONSHIPS, headers=headers, json={"remote_reference_id": reference_id}
        ).json()
        assert follow["remote_endpoint_authorization_status"] == "pending"
        assert follow["remote_a2a_url"] is None
        assert client.get(f"{DISCOVERY}?verified_only=true", headers=headers).json()["agents"] == []
        assert _resolve(client, headers).status_code == 422

        card = _card(lineage, _proof(signer, lineage))
        assert _resolve(client, headers).json()["id"] == reference_id
        updated = client.get(RELATIONSHIPS, headers=headers).json()[0]
        assert updated["id"] == follow["id"]
        assert updated["remote_a2a_url"] == ENDPOINT
        assert updated["remote_identity_verified"] is True
