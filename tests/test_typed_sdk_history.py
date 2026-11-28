import hashlib
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import test_typed_execution
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_lineage_v2 import _keypair
from test_onboarding import transport_for
from test_peer_grants import AUDIENCE, call
from test_remote_agent_resolution import _card, _proof, _root_lineage

from agent_commons import (
    client,
    collaboration_history,
    outbound_https,
    remote_resolution,
    typed_worker,
)
from agent_commons.a2a import A2AAgentExtension
from agent_commons.client import (
    CommonsClient,
    EndpointVerificationError,
    HistoryWriteError,
    OutboundResponseError,
    TypedTaskError,
)
from agent_commons.collaboration import EXTENSION_URI, advertised_skill
from agent_commons.collaboration_history_models import CollaborationHistory
from agent_commons.config import settings
from agent_commons.db import SessionLocal
from agent_commons.main import app
from agent_commons.outbound_a2a import BoundGrant, DestinationApproval
from agent_commons.peer_enrollment import change_grant
from agent_commons.peer_protocol import SkillIssuedGrant
from agent_commons.remote_resolution import _store_reference
from agent_commons.typed_client_models import CollaborationPage

executing = test_typed_execution.executing
enrolled_fixture = test_typed_execution.enrolled_fixture
HOME = "https://home.example"
CARD = AUDIENCE + "/agent-card"
SKILL = "analysis.summary@1"


@pytest.fixture
def linked(executing, enrolled_fixture, monkeypatch):
    receiver, grant_json, recipient = executing
    caller_key = enrolled_fixture[2]
    receiver_key, public = _keypair()
    lineage = _root_lineage(public)
    endpoint = AUDIENCE + "/a2a"
    card = _card(
        lineage, _proof(receiver_key, lineage, card_url=CARD, endpoint=endpoint), endpoint=endpoint
    )
    card.capabilities.extensions.append(
        A2AAgentExtension(
            uri=EXTENSION_URI,
            required=False,
            params={"version": 1, "skills": [advertised_skill(SKILL, execution_enabled=True)]},
        )
    )
    with SessionLocal() as db:
        remote_id = _store_reference(CARD, card, db).id
    remote_cards = {CARD: card}
    monkeypatch.setattr(remote_resolution, "_fetch_agent_card", remote_cards.__getitem__)
    calls = []

    def transfer(url, *, body, bearer, **kwargs):
        assert url == endpoint
        assert bearer == grant_json["token"]
        calls.append(json.loads(body)["method"])
        response = call(receiver, bearer, json.loads(body))
        if response.status_code != 200:
            raise outbound_https.UnexpectedStatus(response.status_code)
        return response.content

    monkeypatch.setattr(outbound_https, "request", transfer)
    with TestClient(app, base_url=HOME) as home:
        response = home.post("/api/v1/agents/register", json={"name": "caller-home"})
        assert response.status_code == 201
        api_key = response.json()["api_key"]
        with CommonsClient(HOME, api_key=api_key, transport=transport_for(home)) as sdk:
            with monkeypatch.context() as binding:
                binding.setattr(settings, "api_url", HOME)
                assert sdk.bind_identity(caller_key) == grant_json["caller_root"]
            grant = SkillIssuedGrant.model_validate(grant_json)
            bound = BoundGrant(
                destination=DestinationApproval(
                    reference_id=remote_id,
                    peer_root=lineage["root_fingerprint"],
                    controller=lineage["current_public_key_multibase"],
                    recipient_agent_id=recipient,
                    endpoint=endpoint,
                ),
                home_origin=HOME,
                grant=grant,
                expires_at=grant.expires_at,
                identity_sequence=lineage["sequence"],
                location_sequence=1,
            )
            yield sdk, bound, home, api_key, remote_cards, receiver_key, lineage, calls


def arguments(grant):
    return {
        "grant": grant,
        "skill": SKILL,
        "data": {"values": [2, 3, -1]},
        "message_id": uuid4().hex,
        "deadline": datetime.now(UTC) + timedelta(seconds=15),
    }


def test_typed_sdk_durable_history_replay_cancel_and_revocation(linked):
    sdk, grant, _, api_key, _, _, _, calls = linked
    assert sdk.discover_known(skill=SKILL).agents[0].root_fingerprint == grant.destination.peer_root
    assert not sdk.discover_known(skill="analysis.summary@2").agents
    params = arguments(grant)
    submitted = sdk.delegate(**params)
    assert submitted.status == "submitted"
    assert typed_worker.run_once()
    task = sdk.wait_for_task(submitted.reference, grant=grant, timeout=3)
    assert task.status == "completed"
    assert task.artifact == {"count": 3, "total": 4, "minimum": -1, "maximum": 3}
    assert sdk.delegate(**params).id == task.id
    canceled = sdk.cancel_task(sdk.delegate(**arguments(grant)).reference, grant=grant)
    assert canceled.status == "canceled"
    # A fresh process starts its own application and connection pool.
    restarted = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import json
import sys
from fastapi.testclient import TestClient
from agent_commons.main import app

with TestClient(app) as home:
    response = home.get(
        '/api/v1/agents/collaboration/history',
        headers={'Authorization': 'Bearer ' + sys.stdin.read()},
    )
    print(json.dumps(response.json()) if response.status_code == 200 else '{}')
""",
        ],
        input=api_key,
        env={
            **os.environ,
            "AGENT_COMMONS_DATABASE_URL": os.environ["AGENT_COMMONS_TEST_DATABASE_URL"],
        },
        text=True,
        capture_output=True,
        check=True,
        timeout=15,
    )
    records = CollaborationPage.model_validate_json(restarted.stdout).records
    assert len(records) == 2
    assert records[1].task_id == task.id and records[1].status == "completed"
    assert (
        records[1].artifact_digest
        == hashlib.sha256(
            json.dumps(task.artifact, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    assert records[1].execution_origin == AUDIENCE
    with SessionLocal.begin() as db:
        change_grant(db, grant.grant.grant_id, rotate=False)
    with pytest.raises(TypedTaskError) as error:
        sdk.get_task_status(task.reference, grant=grant)
    assert error.value.reason == "credential_unavailable"
    assert calls[-1] == "tasks/get"


def test_migration_stops_old_dispatch_and_preserves_history(linked):
    sdk, grant, _, _, cards, key, lineage, calls = linked
    old = sdk.delegate(**arguments(grant))
    assert typed_worker.run_once()
    assert sdk.get_task_status(old.reference, grant=grant).status == "completed"
    earlier = sdk.list_collaboration_history().records[0]
    destination = "https://moved.example"
    card_url = destination + "/agent-card"
    endpoint = destination + "/a2a"
    cards[card_url] = _card(
        lineage,
        _proof(key, lineage, card_url=card_url, endpoint=endpoint, counter=2),
        endpoint=endpoint,
    )
    assert sdk.resolve_peer(card_url).id == grant.destination.reference_id
    previous_calls = list(calls)
    with pytest.raises(EndpointVerificationError):
        sdk.get_task_status(old.reference, grant=grant)
    with pytest.raises(EndpointVerificationError):
        sdk.cancel_task(old.reference, grant=grant)
    assert calls == previous_calls
    assert sdk.list_collaboration_history().records == [earlier]


def test_history_pagination_privacy_retention_and_concurrent_terminal_updates(linked, monkeypatch):
    sdk, grant, home, api_key, _, _, _, _ = linked
    first = sdk.delegate(**arguments(grant))
    second = sdk.delegate(**arguments(grant))
    auth = {"Authorization": "Bearer " + api_key}
    path = "/api/v1/agents/collaboration/history"
    first_page = home.get(path, params={"limit": 1}, headers=auth).json()
    cursor = first_page["next_cursor"]
    assert first_page["records"][0]["task_id"] == str(second.id)
    assert sdk.list_collaboration_history(cursor=UUID(cursor)).records[0].task_id == first.id
    visitor = home.post("/api/v1/agents/register", json={"name": "visitor"}).json()["api_key"]
    outsider = {"Authorization": "Bearer " + visitor}
    assert home.get(path, headers=outsider).json()["records"] == []
    assert home.get(path, headers=outsider, params={"cursor": cursor}).status_code == 404
    observation = {
        "reference_id": str(grant.destination.reference_id),
        "caller_root": grant.grant.caller_root,
        "task": first.reference.model_dump(mode="json"),
        "status": "completed",
        "artifact_digest": "a" * 64,
    }
    assert home.post(path, json=observation, headers=outsider).status_code == 403
    with ThreadPoolExecutor(3) as pool:
        results = list(
            pool.map(lambda _: home.post(path, json=observation, headers=auth), range(3))
        )
    assert all(response.status_code == 200 for response in results)
    assert len({response.json()["id"] for response in results}) == 1
    home.post(
        path, json={**observation, "status": "working", "artifact_digest": None}, headers=auth
    )
    assert sdk.list_collaboration_history().records[1].status == "completed"
    monkeypatch.setattr(collaboration_history, "OWNER_CAP", 2)
    with pytest.raises(HistoryWriteError):
        sdk.delegate(**arguments(grant))
    with SessionLocal.begin() as db:
        first_row = db.scalar(
            select(CollaborationHistory).where(CollaborationHistory.task_id == first.id)
        )
        first_row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert len(sdk.list_collaboration_history().records) == 1
    assert sdk.delegate(**arguments(grant)).status == "submitted"


def test_typed_polling_deadline_and_invalid_artifact(linked, monkeypatch):
    sdk, grant, _, _, _, _, _, _ = linked
    task = sdk.delegate(**arguments(grant))
    clock = 0.0

    def sleep(seconds):
        nonlocal clock
        clock += seconds

    monkeypatch.setattr(client, "monotonic", lambda: clock)
    monkeypatch.setattr(client, "sleep", sleep)
    with pytest.raises(TypedTaskError) as error:
        sdk.wait_for_task(task.reference, grant=grant, timeout=1)
    assert error.value.reason == "polling_timeout"
    assert clock == 1

    def malformed(_url, *, body, **_kwargs):
        return json.dumps(
            {
                "jsonrpc": "2.0",
                "id": json.loads(body)["id"],
                "result": {
                    "kind": "task",
                    "id": str(task.id),
                    "contextId": str(task.id),
                    "status": {"state": "completed"},
                    "artifacts": [],
                },
            }
        ).encode()

    monkeypatch.setattr(outbound_https, "request", malformed)
    with pytest.raises(OutboundResponseError, match="invalid typed task or artifact"):
        sdk.get_task_status(task.reference, grant=grant)


@pytest.mark.parametrize(
    "phase",
    [
        "before_remote",
        "after_verified",
        "during_history",
        "terminal",
        "history_timeout",
        "uncertain",
    ],
)
def test_polling_deadline_and_history_boundary(linked, monkeypatch, phase):
    sdk, grant, _, _, _, _, _, calls = linked
    submitted = sdk.delegate(**arguments(grant))
    if phase == "terminal":
        assert typed_worker.run_once()
    clock = 0.0
    history_writes = 0
    monkeypatch.setattr(client, "monotonic", lambda: clock)
    transport = outbound_https.request
    decode = client.decode_task
    stream = sdk._http.stream

    def remote_request(*args, **kwargs):
        if phase == "uncertain":
            raise outbound_https.TransferError("Test connection failure")
        return transport(*args, **kwargs)

    def decode_task(*args, **kwargs):
        nonlocal clock
        decoded = decode(*args, **kwargs)
        if phase == "after_verified":
            clock = 1
        return decoded

    @contextmanager
    def home_request(method, url, **kwargs):
        nonlocal clock, history_writes
        with stream(method, url, **kwargs) as reply:
            if phase == "before_remote" and url == "agents/me/identity":
                clock = 1
            if url == "agents/collaboration/history":
                assert reply.status_code == 200
                assert kwargs["timeout"] == 2
                history_writes += 1
                if phase in ("during_history", "terminal"):
                    clock = 1
                elif phase == "history_timeout":
                    clock = 2
            yield reply

    monkeypatch.setattr(outbound_https, "request", remote_request)
    monkeypatch.setattr(client, "decode_task", decode_task)
    monkeypatch.setattr(sdk._http, "stream", home_request)
    if phase == "terminal":
        result = sdk.wait_for_task(submitted.reference, grant=grant, timeout=1)
        assert result.status == "completed"
    elif phase == "history_timeout":
        with pytest.raises(HistoryWriteError) as error:
            sdk.wait_for_task(submitted.reference, grant=grant, timeout=1)
        assert error.value.task == submitted
    else:
        with pytest.raises(TypedTaskError) as error:
            sdk.wait_for_task(submitted.reference, grant=grant, timeout=1)
        assert error.value.reason == (
            "request_uncertain" if phase == "uncertain" else "polling_timeout"
        )
    if phase in ("before_remote", "uncertain"):
        assert calls == ["message/send"]
        assert history_writes == 0
    else:
        assert calls == ["message/send", "tasks/get"]
        assert history_writes == 1


def test_history_migration_round_trip_preserves_agents():
    from importlib import import_module

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    from agent_commons.db import engine

    migration = import_module("migrations.versions.0020_collaboration_history")
    schema = f"history_migration_{uuid4().hex}"
    owner = uuid4()
    with engine.connect() as db, db.begin() as transaction:
        db.exec_driver_sql(f"CREATE SCHEMA {schema}")
        db.exec_driver_sql(f"SET LOCAL search_path TO {schema}, pg_catalog")
        db.exec_driver_sql("CREATE TABLE agents (id uuid PRIMARY KEY)")
        db.exec_driver_sql("INSERT INTO agents VALUES (%s)", (owner,))
        with Operations.context(MigrationContext.configure(db)):
            migration.upgrade()
            assert db.exec_driver_sql("SELECT count(*) FROM collaboration_history").scalar() == 0
            migration.downgrade()
            assert (
                db.exec_driver_sql("SELECT to_regclass('collaboration_history')").scalar() is None
            )
            migration.upgrade()
            assert db.exec_driver_sql("SELECT count(*) FROM collaboration_history").scalar() == 0
        assert db.exec_driver_sql("SELECT id FROM agents").scalar() == owner
        transaction.rollback()


def test_developer_guide_sdk_snippet(linked):
    sdk, grant, *_ = linked
    guide = Path(__file__).resolve().parents[1] / "docs/collaboration/developer-guide.md"
    snippet = guide.read_text(encoding="utf-8").split("<!-- tested-sdk-example -->", 1)[1]
    code = snippet.split("```python\n", 1)[1].split("```", 1)[0]
    example = {}
    exec(compile(code, str(guide), "exec"), example)
    task = example["submit_analysis"](sdk, grant)
    assert typed_worker.run_once()
    artifact, page = example["read_analysis"](sdk, grant, task)
    assert artifact == {"count": 3, "total": 4, "minimum": -1, "maximum": 3}
    assert page.records[0].task_id == task.id

    queued = example["submit_analysis"](sdk, grant)
    assert sdk.get_task_status(queued.reference, grant=grant).status == "submitted"
    assert sdk.cancel_task(queued.reference, grant=grant).status == "canceled"
