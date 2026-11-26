import json
import os
import socket
import stat
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from test_lineage_v2 import _keypair, _signature
from test_onboarding import transport_for
from test_remote_agent_resolution import CARD_URL, _card, _proof, _root_lineage

from agent_commons import a2a_tasks, cli, peer_enrollment
from agent_commons.client import CommonsClient, CommonsError
from agent_commons.config import A2ATaskPeer, Settings, settings
from agent_commons.db import Base, SessionLocal, engine
from agent_commons.main import app
from agent_commons.peer_enrollment import approve_peer, change_grant
from agent_commons.peer_models import PeerApproval, PeerGrant
from agent_commons.peer_protocol import EnrollmentChallenge
from agent_commons.remote_models import RemoteAgentReference
from agent_commons.remote_resolution import _store_reference
from agent_commons.security import hash_api_key
from scripts.agent_internet_live_support import _stop, make_tls

AUDIENCE = "https://receiver.example"
SCOPES = ["message/send", "tasks/get"]
TASK = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "message/send",
    "params": {
        "message": {
            "kind": "message",
            "role": "user",
            "messageId": "grant-test",
            "parts": [{"kind": "text", "text": "hello world"}],
        }
    },
}


def worker():
    preview = FastAPI()
    preview.include_router(peer_enrollment.router)
    preview.include_router(a2a_tasks.router)
    return TestClient(preview, base_url=AUDIENCE)


@pytest.fixture
def enrollment(monkeypatch):
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(settings, "a2a_database_grants", True)
    monkeypatch.setattr(settings, "api_url", AUDIENCE)
    monkeypatch.setattr(settings, "a2a_task_peers", [])
    with TestClient(app) as core:
        recipient = UUID(
            core.post("/api/v1/agents/register", json={"name": "atlas"}).json()["agent"]["id"]
        )
    key, public = _keypair()
    lineage = _root_lineage(public)
    root = lineage["root_fingerprint"]
    with SessionLocal.begin() as db:
        record = _store_reference(CARD_URL, _card(lineage, _proof(key, lineage)), db)
        reference_id = record.id
    with SessionLocal.begin() as db:
        approval_id = approve_peer(
            db,
            reference_id=reference_id,
            expected_root=root,
            recipient=recipient,
            scopes=SCOPES,
        )
    with worker() as http:
        yield http, approval_id, key, root, recipient, reference_id


def _path(approval_id, action):
    return f"/api/v1/peers/{approval_id}/{action}"


def challenge(http, approval_id):
    response = http.post(_path(approval_id, "challenge"))
    assert response.status_code == 200
    return EnrollmentChallenge.model_validate(response.json())


def redeem(http, approval_id, key, signed):
    return http.post(
        _path(approval_id, "redeem"),
        json={
            "challenge_id": str(signed.challenge_id),
            "signature_multibase": _signature(key, signed.signing_payload()),
        },
    )


def enrolled(http, approval_id, key):
    response = redeem(http, approval_id, key, challenge(http, approval_id))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def call(http, token, body=TASK):
    return http.post("/a2a", json=body, headers={"Authorization": f"Bearer {token}"})


def test_successful_enrollment_stores_only_digest_and_requires_approval(enrollment):
    http, approval_id, key, root, recipient, _ = enrollment
    signed = challenge(http, approval_id)
    assert signed == challenge(http, approval_id)
    assert signed.caller_root == root and signed.recipient_agent_id == recipient
    with SessionLocal() as db:
        assert db.scalars(select(PeerGrant)).all() == []
    assert http.post(_path(uuid4(), "challenge")).status_code == 403
    assert redeem(http, approval_id, _keypair()[0], signed).status_code == 403
    response = redeem(http, approval_id, key, signed)
    assert response.status_code == 200
    grant = response.json()
    assert redeem(http, approval_id, key, signed).status_code == 403
    assert http.post(_path(approval_id, "challenge")).status_code == 403
    with SessionLocal() as db:
        row = db.get(PeerGrant, UUID(grant["grant_id"]))
        assert row.token_digest == hash_api_key(grant["token"])
        assert row.token_digest != grant["token"]
        assert row.expires_at - row.issued_at == timedelta(hours=1)
    assert call(http, grant["token"]).json()["result"]["status"]["state"] == "completed"


@pytest.mark.parametrize("field", ["caller_root", "recipient_agent_id", "audience", "scopes"])
def test_tampered_challenge_is_rejected(enrollment, field):
    http, approval_id, key, *_ = enrollment
    signed = challenge(http, approval_id).model_copy(
        update={
            field: {
                "caller_root": "sha256:" + "1" * 64,
                "recipient_agent_id": uuid4(),
                "audience": "https://other.example",
                "scopes": ["tasks/get"],
            }[field]
        }
    )
    assert redeem(http, approval_id, key, signed).status_code == 403


def test_https_audience_and_expiry_are_enforced(enrollment):
    http, approval_id, key, *_ = enrollment
    assert (
        http.post(_path(approval_id, "challenge"), headers={"Host": "other.example"}).status_code
        == 403
    )
    assert http.post(f"http://receiver.example{_path(approval_id, 'challenge')}").status_code == 403
    signed = challenge(http, approval_id)
    with SessionLocal.begin() as db:
        db.get(PeerApproval, approval_id).challenge_expires_at = datetime.now(UTC) - timedelta(
            seconds=1
        )
    assert redeem(http, approval_id, key, signed).status_code == 403
    assert challenge(http, approval_id).challenge_id != signed.challenge_id


def test_operator_must_supply_expected_sovereign_root_and_valid_permissions(enrollment):
    _, _, _, root, recipient, reference_id = enrollment
    with SessionLocal() as db:
        with pytest.raises(HTTPException, match="expected root"):
            approve_peer(
                db,
                reference_id=reference_id,
                expected_root="sha256:" + "a" * 64,
                recipient=recipient,
                scopes=SCOPES,
            )
        with pytest.raises(ValueError, match="recipient"):
            approve_peer(
                db, reference_id=reference_id, expected_root=root, recipient=uuid4(), scopes=SCOPES
            )
        with pytest.raises(ValueError, match="lifetime"):
            approve_peer(
                db,
                reference_id=reference_id,
                expected_root=root,
                recipient=recipient,
                scopes=SCOPES,
                lifetime_hours=25,
            )
        ordinary = _store_reference("https://regular.example/card", _card(), db)
        with pytest.raises(HTTPException, match="expected root"):
            approve_peer(
                db, reference_id=ordinary.id, expected_root=root, recipient=recipient, scopes=SCOPES
            )


def test_redemption_rechecks_stored_controller_lineage(enrollment):
    http, approval_id, key, _, _, reference_id = enrollment
    signed = challenge(http, approval_id)
    with SessionLocal.begin() as db:
        reference = db.get(RemoteAgentReference, reference_id)
        reference.current_controller_public_key = _keypair()[1]
    assert redeem(http, approval_id, key, signed).status_code == 403


def test_concurrent_redemption_creates_one_grant(enrollment):
    http, approval_id, key, *_ = enrollment
    signed = challenge(http, approval_id)
    barrier = Barrier(2)

    def submit():
        barrier.wait(timeout=5)
        return redeem(http, approval_id, key, signed).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(submit) for _ in range(2)]
        assert sorted(result.result(timeout=10) for result in results) == [200, 403]
    with SessionLocal() as db:
        assert len(db.scalars(select(PeerGrant)).all()) == 1


def test_rotation_overlap_revocation_and_task_ownership_across_workers(enrollment):
    http, approval_id, key, *_ = enrollment
    first = enrolled(http, approval_id, key)
    old_id = UUID(first["grant_id"])
    original = call(http, first["token"]).json()["result"]
    with SessionLocal.begin() as db:
        assert change_grant(db, old_id, rotate=True) == approval_id
    second = enrolled(http, approval_id, key)
    second_id = UUID(second["grant_id"])

    with worker() as other:
        assert call(other, second["token"]).json()["result"] == original
        params = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tasks/get",
            "params": {"id": original["id"]},
        }
        assert call(other, first["token"], params).json()["result"] == original
        assert call(other, second["token"], params).json()["result"] == original
        with SessionLocal.begin() as db:
            old, new = db.get(PeerGrant, old_id), db.get(PeerGrant, second_id)
            assert timedelta(0) < old.expires_at - new.issued_at <= timedelta(minutes=5)
            change_grant(db, old_id, rotate=False)
        assert call(http, first["token"]).status_code == 401
        assert call(other, first["token"]).status_code == 401
        with SessionLocal.begin() as db:
            new = db.get(PeerGrant, second_id)
            new.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        assert call(other, second["token"]).status_code == 401


def test_database_failure_never_falls_back_to_static_grants(enrollment, monkeypatch):
    http, _, _, root, recipient, _ = enrollment
    token = "a2a_" + "a" * 43
    peer = A2ATaskPeer(
        token_sha256=hash_api_key(token),
        caller_root=root,
        recipient_agent_id=recipient,
        scopes=set(SCOPES),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    monkeypatch.setattr(settings, "a2a_task_peers", [peer])

    def failed():
        raise OperationalError("SELECT", {}, RuntimeError("private backend failure"))

    monkeypatch.setattr(peer_enrollment, "SessionLocal", failed)
    response = call(http, token)
    assert response.status_code == 503
    assert "private" not in response.text
    assert token not in response.text
    monkeypatch.setattr(settings, "a2a_database_grants", False)
    assert call(http, token).status_code == 200


def test_preview_configuration_rejects_static_mix_and_insecure_audience(enrollment, monkeypatch):
    _, _, _, root, recipient, _ = enrollment
    monkeypatch.delenv("AGENT_COMMONS_A2A_TASK_PEERS", raising=False)
    peer = A2ATaskPeer(
        token_sha256="a" * 64,
        caller_root=root,
        recipient_agent_id=recipient,
        scopes=set(SCOPES),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    with pytest.raises(ValidationError, match="cannot be mixed"):
        Settings(a2a_database_grants=True, api_url=AUDIENCE, a2a_task_peers=[peer])
    with pytest.raises(ValidationError, match="canonical HTTPS"):
        Settings(a2a_database_grants=True, api_url="http://receiver.example")
    with pytest.raises(ValidationError, match="invite-only"):
        Settings(a2a_database_grants=True, api_url=AUDIENCE, registration_admission="open")
    assert TestClient(app).post("/a2a").status_code == 404


def test_cli_saves_separate_private_grant_without_printing_token(
    enrollment,
    tmp_path,
    monkeypatch,
    capsys,
):
    http, approval_id, key, root, recipient, _ = enrollment
    monkeypatch.setattr(
        cli,
        "CommonsClient",
        lambda url: CommonsClient(url, transport=transport_for(http)),
    )
    config = tmp_path / "agent"
    config.mkdir(mode=0o700)
    with cli._directory(config) as directory:
        cli._write(directory, "controller.key", key.private_bytes_raw())
        cli._save(directory, cli.LocalAccount(origin="https://caller.example", name="nova"))
    cli.run(
        cli._parser().parse_args(
            [
                "--config",
                str(config),
                "peer-enroll",
                "--url",
                AUDIENCE,
                "--approval",
                str(approval_id),
                "--caller-root",
                root,
                "--recipient",
                str(recipient),
                "--scope",
                "message/send",
                "--scope",
                "tasks/get",
            ]
        )
    )
    (path,) = config.glob("peer-*.json")
    secret = json.loads(path.read_text())["token"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert secret not in capsys.readouterr().out
    assert call(http, secret).status_code == 200


def test_sdk_rejects_wrong_expected_scope_before_signing(enrollment):
    http, approval_id, key, root, recipient, _ = enrollment
    seen = []
    with CommonsClient(AUDIENCE, transport=transport_for(http, seen)) as client:
        with pytest.raises(CommonsError, match="expected approval"):
            client.enroll_peer(
                key,
                approval_id=approval_id,
                caller_root=root,
                recipient_agent_id=recipient,
                scopes=["message/send"],
            )
    assert len(seen) == 1 and seen[0].url.path.endswith("/challenge")


def test_read_only_grant_cannot_submit_tasks(enrollment):
    http, _, key, root, recipient, reference_id = enrollment
    with SessionLocal.begin() as db:
        approval_id = approve_peer(
            db,
            reference_id=reference_id,
            expected_root=root,
            recipient=recipient,
            scopes=["tasks/get"],
        )
    grant = enrolled(http, approval_id, key)
    assert call(http, grant["token"]).status_code == 403


def test_revocation_is_visible_to_two_server_processes(enrollment, tmp_path):
    http, approval_id, key, *_ = enrollment
    grant = enrolled(http, approval_id, key)
    cert, private, tls = make_tls(tmp_path)
    env = {
        "PATH": os.environ["PATH"],
        "AGENT_COMMONS_DATABASE_URL": engine.url.render_as_string(hide_password=False),
        "AGENT_COMMONS_API_URL": AUDIENCE,
        "AGENT_COMMONS_A2A_DATABASE_GRANTS": "true",
        "AGENT_COMMONS_REGISTRATION_ADMISSION": "invite",
        "AGENT_COMMONS_A2A_TASK_PEERS": "[]",
    }
    with ExitStack() as resources:
        origins = []
        client = resources.enter_context(
            httpx.Client(
                verify=tls, trust_env=False, timeout=2, headers={"Host": "receiver.example"}
            )
        )
        for _ in range(2):
            listener = resources.enter_context(socket.socket())
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            origins.append(f"https://127.0.0.1:{listener.getsockname()[1]}")
            fd = listener.fileno()
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "agent_commons.main:app",
                    "--fd",
                    str(fd),
                    "--ssl-certfile",
                    str(cert),
                    "--ssl-keyfile",
                    str(private),
                    "--no-access-log",
                ],
                env=env,
                pass_fds=(fd,),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            resources.callback(_stop, process)
        for origin in origins:
            for _ in range(50):
                try:
                    if client.get(origin + "/api/v1/health", timeout=0.25).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            else:
                pytest.fail("Peer grant worker did not start", pytrace=False)

        headers = {"Authorization": "Bearer " + grant["token"]}
        for origin in origins:
            response = client.post(origin + "/a2a", json=TASK, headers=headers)
            assert response.status_code == 200 and response.json().get("result")
        with SessionLocal.begin() as db:
            change_grant(db, UUID(grant["grant_id"]), rotate=False)
        for origin in origins:
            assert client.post(origin + "/a2a", json=TASK, headers=headers).status_code == 401
