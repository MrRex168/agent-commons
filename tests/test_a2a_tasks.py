import asyncio
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text, update

from agent_commons import a2a_tasks
from agent_commons.a2a_task_models import A2ATask
from agent_commons.config import A2ATaskPeer, settings
from agent_commons.db import Base, SessionLocal, engine
from agent_commons.main import app

TOKEN = "a2a_" + "A" * 43
OTHER_TOKEN = "a2a_" + "B" * 43
READ_TOKEN = "a2a_" + "C" * 43
RECIPIENT_TOKEN = "a2a_" + "D" * 43
EXPIRED_TOKEN = "a2a_" + "E" * 43
ROOT = "sha256:" + "a" * 64
OTHER_ROOT = "sha256:" + "b" * 64


def _peer(token, root, recipient_id, *, scopes=None, expired=False):
    return A2ATaskPeer(
        token_sha256=hashlib.sha256(token.encode()).hexdigest(),
        caller_root=root,
        recipient_agent_id=recipient_id,
        scopes=scopes if scopes is not None else {"message/send", "tasks/get"},
        expires_at=datetime.now(UTC) + timedelta(hours=-1 if expired else 1),
    )


@pytest.fixture
def runtime(monkeypatch):
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with TestClient(app) as core:
        r = core.post("/api/v1/agents/register", json={"name": "a2a-recipient"})
        other = core.post("/api/v1/agents/register", json={"name": "other-recipient"})
    assert r.status_code == other.status_code == 201
    recipient = UUID(r.json()["agent"]["id"])
    other_recipient = UUID(other.json()["agent"]["id"])
    monkeypatch.setattr(
        settings,
        "a2a_task_peers",
        [
            _peer(TOKEN, ROOT, recipient),
            _peer(OTHER_TOKEN, OTHER_ROOT, recipient),
            _peer(READ_TOKEN, ROOT, recipient, scopes={"tasks/get"}),
            _peer(RECIPIENT_TOKEN, ROOT, other_recipient),
            _peer(EXPIRED_TOKEN, ROOT, recipient, expired=True),
        ],
    )
    enabled = FastAPI()
    enabled.include_router(a2a_tasks.router)
    with TestClient(enabled) as client:
        yield client, r.json()["api_key"]


def _request(method, params, *, request_id="rpc-1") -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}


def _send(client, *, token=TOKEN, message_id="message-1", content="Hi 世界\nthere", request_id=1):
    parameters = {
        "message": {
            "kind": "message",
            "role": "user",
            "messageId": message_id,
            "parts": [{"kind": "text", "text": content}],
        },
        "configuration": {"acceptedOutputModes": ["text/plain"], "blocking": True},
    }
    return client.post(
        "/a2a",
        json=_request("message/send", parameters, request_id=request_id),
        headers={"Authorization": f"Bearer {token}"},
    )


def _get(client, task_id, token=TOKEN):
    return client.post(
        "/a2a",
        json=_request("tasks/get", {"id": task_id, "historyLength": 0}),
        headers={"Authorization": f"Bearer {token}"},
    )


def test_runtime_is_disabled_without_configured_peers():
    assert TestClient(app).post("/a2a").status_code == 404


def test_send_and_retrieve_completed_task(runtime):
    client, _ = runtime
    response = _send(client)
    assert response.status_code == 200
    result = response.json()
    assert result["id"] == 1 and result["jsonrpc"] == "2.0"
    task = result["result"]
    assert task["kind"] == "task"
    assert task["status"] == {"state": "completed"}
    assert UUID(task["contextId"])
    assert UUID(task["artifacts"][0]["artifactId"])
    assert task["artifacts"][0]["parts"] == [{"kind": "text", "text": "bytes: 15\nwords: 3"}]
    assert _get(client, task["id"], READ_TOKEN).json()["result"] == task


def test_peer_credentials_and_scopes(runtime):
    client, core_api_key = runtime
    assert client.post("/a2a").status_code == 401
    for token in (core_api_key, EXPIRED_TOKEN, "a2a_" + "Z" * 43):
        response = _send(client, token=token)
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"
    assert _send(client, token=READ_TOKEN).status_code == 403
    with TestClient(app) as core:
        assert (
            core.get("/api/v1/agents/me", headers={"Authorization": f"Bearer {TOKEN}"}).status_code
            == 401
        )


def test_task_is_private_to_caller_and_recipient(runtime):
    client, _ = runtime
    task = _send(client).json()["result"]
    missing = _get(client, str(uuid4())).json()
    assert missing["error"]["code"] == -32001
    assert _get(client, task["id"], OTHER_TOKEN).json() == missing
    assert _get(client, task["id"], RECIPIENT_TOKEN).json() == missing
    for token in (OTHER_TOKEN, RECIPIENT_TOKEN):
        assert _send(client, token=token).json()["result"]["id"] != task["id"]


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b"{", -32700),
        (b'{"id": 1, "id": 2}', -32700),
        (b'{"id": NaN}', -32700),
        pytest.param(b"[" * 100 + b"]" * 100, -32600, id="nested-array"),
        (b"[]", -32600),
        (b'{"jsonrpc":"2.0","id":true,"method":"tasks/get"}', -32600),
        (b'{"jsonrpc":"2.0","id":"\\ud800","method":"tasks/get"}', -32600),
        (b'{"jsonrpc":"2.0","method":"message/send"}', -32600),
        (b'{"jsonrpc":"1.0","id":1,"method":"tasks/get"}', -32600),
        (b'{"jsonrpc":"2.0","id":1,"method":"tasks/cancel"}', -32601),
        (b'{"jsonrpc":"2.0","id":1,"method":"message/send","params":{}}', -32602),
        (b'{"jsonrpc":"2.0","id":1,"method":"message/send","params":[]}', -32602),
    ],
)
def test_jsonrpc_errors(runtime, raw, code):
    client, _ = runtime
    r = client.post(
        "/a2a",
        content=raw,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
    )
    assert r.status_code == 200
    assert r.json()["error"]["code"] == code
    assert r.json()["id"] == (1 if code in (-32601, -32602) else None)


def test_conflicting_retry_is_rejected_without_changing_task(runtime):
    client, _ = runtime
    initial = _send(client).json()["result"]
    retry = _send(client, request_id="retry").json()
    assert retry["id"] == "retry" and retry["result"] == initial
    conflict = _send(client, content="different").json()
    assert conflict["error"]["code"] == -32010
    assert _get(client, initial["id"]).json()["result"] == initial


def test_duplicate_submissions_are_atomic(runtime, monkeypatch):
    client, _ = runtime
    arrive = Barrier(4)
    send_task = a2a_tasks._send_task

    def simultaneous_send(peer, params):
        arrive.wait(timeout=5)
        return send_task(peer, params)

    monkeypatch.setattr(a2a_tasks, "_send_task", simultaneous_send)
    with ThreadPoolExecutor(max_workers=4) as pool:
        requests = [pool.submit(_send, client, request_id=i) for i in range(4)]
        responses = [request.result(timeout=10).json() for request in requests]
    assert [result["id"] for result in responses] == list(range(4))
    assert len({result["result"]["id"] for result in responses}) == 1
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(A2ATask)) == 1


def test_task_cap_expiry_and_cleanup(runtime, monkeypatch):
    client, _ = runtime
    monkeypatch.setattr(a2a_tasks, "MAX_RETAINED_TASKS", 1)
    initial = _send(client).json()["result"]
    assert _send(client).json()["result"] == initial
    assert _send(client, message_id="new").json()["error"]["code"] == -32011
    with SessionLocal.begin() as db:
        db.execute(update(A2ATask).values(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    assert _get(client, initial["id"]).json()["error"]["code"] == -32001
    renewed = _send(client).json()["result"]
    assert renewed["id"] != initial["id"]
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(A2ATask)) == 1


def test_body_and_text_bounds(runtime):
    client, _ = runtime
    assert _send(client, content="é" * 2048).status_code == 200
    assert _send(client, content="é" * 2049).json()["error"]["code"] == -32602
    assert _send(client, content="a" * a2a_tasks.MAX_REQUEST_BYTES).status_code == 413
    assert (
        client.post("/a2a", content=b"{}", headers={"Authorization": f"Bearer {TOKEN}"}).status_code
        == 415
    )


def test_streamed_body_size_and_deadline(runtime, monkeypatch):
    client, _ = runtime
    monkeypatch.setattr(a2a_tasks, "BODY_TIMEOUT_SECONDS", 0.025)

    async def oversized():
        yield b" " * a2a_tasks.MAX_REQUEST_BYTES
        yield b" "

    async def slow():
        await asyncio.sleep(0.1)
        yield json.dumps({}).encode()

    async def test_streams():
        transport = httpx.ASGITransport(app=client.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as streaming:
            headers = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
            assert (
                await streaming.post("/a2a", content=oversized(), headers=headers)
            ).status_code == 413
            assert (
                await streaming.post("/a2a", content=slow(), headers=headers)
            ).status_code == 408

    asyncio.run(test_streams())


def test_concurrency_gate_and_database_lock_timeout(runtime):
    client, _ = runtime
    gate = a2a_tasks.REQUEST_SLOTS
    for _ in range(8):
        assert gate.acquire(blocking=False)
    try:
        assert _send(client).status_code == 503
    finally:
        for _ in range(8):
            gate.release()
    with SessionLocal.begin() as db:
        db.execute(text("LOCK TABLE a2a_tasks IN SHARE ROW EXCLUSIVE MODE"))
        assert _send(client).status_code == 503
    assert _send(client).json()["result"]["status"]["state"] == "completed"
