import json
import os
import signal
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import test_peer_grants
from sqlalchemy import func, select
from test_lineage_v2 import _keypair
from test_peer_grants import AUDIENCE, _path, call, enrolled, redeem
from test_remote_agent_resolution import _card, _proof, _root_lineage

from agent_commons import typed_tasks, typed_worker
from agent_commons.a2a_task_models import A2ATask
from agent_commons.config import settings
from agent_commons.db import SessionLocal, engine
from agent_commons.peer_enrollment import approve_peer, change_grant
from agent_commons.peer_protocol import SkillEnrollmentChallenge
from agent_commons.remote_resolution import _store_reference

SKILLS = ["analysis.summary@1", "report.markdown@1"]
METHODS = ["message/send", "tasks/get", "tasks/cancel"]
enrolled_fixture = test_peer_grants.enrollment


def enroll_typed(http, approval, key):
    challenge = http.post(_path(approval, "challenge"))
    assert challenge.status_code == 200
    result = redeem(http, approval, key, SkillEnrollmentChallenge.model_validate(challenge.json()))
    assert result.status_code == 200
    return result.json()


@pytest.fixture
def executing(enrolled_fixture, monkeypatch):
    http, _, key, root, recipient, reference = enrolled_fixture
    monkeypatch.setattr(settings, "collaboration_execution", True)
    monkeypatch.setattr(settings, "preview_limits", True)
    monkeypatch.setattr(settings, "preview_tasks_per_second", 100)
    monkeypatch.setattr(settings, "preview_task_rate_per_grant", 100)
    monkeypatch.setattr(settings, "preview_task_burst_per_grant", 32)
    monkeypatch.setattr(settings, "collaboration_skills", {recipient: SKILLS})
    with SessionLocal.begin() as db:
        approval = approve_peer(
            db,
            reference_id=reference,
            expected_root=root,
            recipient=recipient,
            scopes=METHODS,
            skills=[SKILLS[0]],
        )
    grant = enroll_typed(http, approval, key)
    return http, grant, recipient


def request(method, params):
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}


def submission(**changes):
    return request(
        "message/send",
        {
            "message": {
                "kind": "message",
                "role": "user",
                "messageId": uuid4().hex,
                "parts": [
                    {
                        "kind": "data",
                        "data": {
                            "skill": SKILLS[0],
                            "input": {"values": [2, 3, -1]},
                            "deadline": (datetime.now(UTC) + timedelta(seconds=15)).isoformat(),
                            **changes,
                        },
                    }
                ],
            },
        },
    )


def rpc(config, body):
    return call(config[0], config[1]["token"], body)


def submit(config, body=None):
    response = rpc(config, body or submission())
    assert response.status_code == 200
    return response.json()["result"]


def read(config, task):
    return rpc(config, request("tasks/get", {"id": task["id"]})).json()["result"]


def test_typed_execute_and_replay(executing):
    body = submission()
    initial = submit(executing, body)
    assert initial["status"]["state"] == "submitted"
    subprocess.run(
        [sys.executable, "-m", "agent_commons.typed_worker", "--once"],
        env={
            "PATH": os.environ["PATH"],
            "AGENT_COMMONS_DATABASE_URL": engine.url.render_as_string(hide_password=False),
            "AGENT_COMMONS_API_URL": AUDIENCE,
            "AGENT_COMMONS_A2A_DATABASE_GRANTS": "true",
            "AGENT_COMMONS_PREVIEW_LIMITS": "true",
            "AGENT_COMMONS_REGISTRATION_ADMISSION": "invite",
            "AGENT_COMMONS_COLLABORATION_EXECUTION": "true",
            "AGENT_COMMONS_COLLABORATION_SKILLS": json.dumps({str(executing[2]): SKILLS}),
        },
        capture_output=True,
        check=True,
        timeout=10,
    )
    result = read(executing, initial)
    assert result["status"]["state"] == "completed"
    assert result["artifacts"][0]["parts"] == [
        {
            "kind": "data",
            "data": {"count": 3, "total": 4, "minimum": -1, "maximum": 3},
        }
    ]
    assert submit(executing, body) == result
    body["params"]["message"]["parts"][0]["data"]["input"]["values"] = [5]
    assert rpc(executing, body).json()["error"]["code"] == -32010
    with SessionLocal() as db:
        task = db.get(A2ATask, UUID(initial["id"]))
        assert task.task_input is None and task.attempt is None


def test_concurrent_duplicate_and_claim(executing):
    body = submission()
    with ThreadPoolExecutor(3) as pool:
        results = list(pool.map(lambda _: submit(executing, body), range(3)))
        claims = list(pool.map(lambda _: typed_tasks.claim(), range(3)))
    assert len({result["id"] for result in results}) == 1
    assert sum(claim is not None for claim in claims) == 1
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(A2ATask)) == 1


def test_unauthorized_schema_exhaustion_and_deadline(executing, monkeypatch):
    assert rpc(executing, submission(input={"values": ["bad"]})).json()["error"]["code"] == -32602
    denied = rpc(
        executing,
        submission(
            skill=SKILLS[1],
            input={
                "title": "x",
                "summary": {"count": 1, "total": 2, "minimum": 2, "maximum": 2},
            },
        ),
    )
    assert denied.status_code == 403
    assert (
        rpc(
            executing,
            submission(
                deadline=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
            ),
        ).json()["error"]["code"]
        == -32602
    )
    monkeypatch.setattr(typed_tasks, "MAX_ACTIVE", 1)
    submit(executing)
    assert rpc(executing, submission()).json()["error"]["code"] == -32011


def test_cancellation_crash_deadline_and_stale_finish(executing):
    queued = submit(executing)
    canceled = rpc(executing, request("tasks/cancel", {"id": queued["id"]})).json()["result"]
    assert canceled["status"]["state"] == "canceled"
    assert typed_tasks.claim() is None
    running = submit(executing)
    claim = typed_tasks.claim()
    assert claim is not None
    assert read(executing, running)["status"]["state"] == "working"
    rpc(executing, request("tasks/cancel", {"id": running["id"]})).raise_for_status()
    assert not typed_tasks.current(claim) and not typed_tasks.finish(claim, None)
    assert read(executing, running)["status"]["state"] == "canceled"
    abandoned = submit(executing)
    claim = typed_tasks.claim()
    with SessionLocal.begin() as db:
        item = db.get(A2ATask, UUID(abandoned["id"]))
        item.deadline = datetime.now(UTC) - timedelta(seconds=1)
    typed_tasks.sweep()
    assert read(executing, abandoned)["status"]["state"] == "failed"
    assert not typed_tasks.finish(claim, {"count": 1, "total": 1, "minimum": 1, "maximum": 1})
    with SessionLocal.begin() as db:
        db.get(A2ATask, UUID(abandoned["id"])).expires_at = datetime.now(UTC)
    typed_tasks.sweep()
    assert (
        rpc(executing, request("tasks/get", {"id": abandoned["id"]})).json()["error"]["code"]
        == -32001
    )


def test_revocation_before_claim(executing):
    task = submit(executing)
    with SessionLocal.begin() as db:
        change_grant(db, UUID(executing[1]["grant_id"]), rotate=False)
    assert typed_tasks.claim() is None
    with SessionLocal() as db:
        assert db.get(A2ATask, UUID(task["id"])).state == "rejected"


def test_bad_output_and_execution_deadline(executing):
    task = submit(executing)
    claim = typed_tasks.claim()
    assert typed_worker.execute(replace(claim, deadline=datetime.now(UTC))) is None
    assert typed_tasks.finish(claim, {"count": "bad"})
    assert read(executing, task)["status"]["state"] == "failed"


def test_cross_owner_and_legacy_isolation(executing):
    task = submit(executing)
    http, _, recipient = executing
    key, public = _keypair()
    lineage = _root_lineage(public)
    url = "https://other.example/agent-card"
    with SessionLocal() as db:
        reference_id = _store_reference(
            url, _card(lineage, _proof(key, lineage, card_url=url)), db
        ).id
    fields = dict(
        reference_id=reference_id,
        expected_root=lineage["root_fingerprint"],
        recipient=recipient,
    )
    with SessionLocal.begin() as db:
        approval = approve_peer(db, **fields, scopes=METHODS, skills=[SKILLS[0]])
    grant = enroll_typed(http, approval, key)
    for method in ("tasks/get", "tasks/cancel"):
        assert (
            call(http, grant["token"], request(method, {"id": task["id"]})).json()["error"]["code"]
            == -32001
        )
    with SessionLocal.begin() as db:
        approval = approve_peer(db, **fields, scopes=["tasks/get"])
    legacy = enrolled(http, approval, key)
    assert (
        call(http, legacy["token"], request("tasks/get", {"id": task["id"]})).json()["error"][
            "code"
        ]
        == -32001
    )


@pytest.mark.parametrize(
    "operation",
    [
        "os.open('/etc/passwd', os.O_RDONLY)",
        "socket.socket()",
        "os.fork()",
        "os.execv('/bin/true', ['/bin/true'])",
    ],
)
def test_seccomp_denies_files_network_and_process_creation(operation):
    runner = repr(str(typed_worker.RUNNER))
    code = f"import os, socket, runpy; runpy.run_path({runner})['isolate'](); {operation}"
    proc = subprocess.run(
        [sys.executable, "-I", "-S", "-c", code],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={},
        timeout=3,
        check=False,
    )
    assert proc.returncode == -signal.SIGSYS


def test_report_handler_emits_structured_markup():
    claim = typed_tasks.Claim(
        uuid4(),
        uuid4(),
        SKILLS[1],
        {
            "title": "<script>unsafe</script>",
            "summary": {"count": 1, "total": 2, "minimum": 2, "maximum": 2},
        },
        datetime.now(UTC) + timedelta(seconds=3),
    )
    result = typed_worker.execute(claim, lambda _: True)
    assert result and "<script>" not in result["markdown"]


def test_revocation_after_claim_before_launch(executing):
    task = submit(executing)
    claimed = typed_tasks.claim()
    with SessionLocal.begin() as db:
        change_grant(db, UUID(executing[1]["grant_id"]), rotate=False)
    assert not typed_tasks.authorize_start(claimed)
    assert not typed_tasks.finish(claimed, None)
    with SessionLocal() as db:
        assert db.get(A2ATask, UUID(task["id"])).state == "rejected"


def test_typed_requests_share_preview_quota(executing, monkeypatch):
    monkeypatch.setattr(settings, "preview_tasks_per_second", 1)
    submit(executing)
    assert rpc(executing, submission()).status_code == 429
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(A2ATask)) == 1


def test_seccomp_child_enforces_memory_limit():
    runner = repr(str(typed_worker.RUNNER))
    code = f"""import os, runpy
runpy.run_path({runner})['isolate']()
try:
    bytearray(256 * 1024 * 1024)
except MemoryError:
    os._exit(0)
os._exit(1)
"""
    assert (
        subprocess.run(
            [sys.executable, "-I", "-S", "-c", code],
            stderr=subprocess.DEVNULL,
            env={},
            timeout=3,
            check=False,
        ).returncode
        == 0
    )


def test_execution_migration_preserves_legacy_rows_on_downgrade():
    from importlib import import_module

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    migration = import_module("migrations.versions.0019_typed_task_execution")
    schema = f"task_execution_migration_{uuid4().hex}"
    with engine.connect() as db, db.begin() as transaction:
        db.exec_driver_sql(f"CREATE SCHEMA {schema}")
        db.exec_driver_sql(f"SET LOCAL search_path TO {schema}, pg_catalog")
        db.exec_driver_sql("CREATE TABLE a2a_tasks (id integer PRIMARY KEY)")
        db.exec_driver_sql("INSERT INTO a2a_tasks VALUES (1)")
        with Operations.context(MigrationContext.configure(db)):
            migration.upgrade()
            db.exec_driver_sql(
                "INSERT INTO a2a_tasks (id, skill, state) "
                "VALUES (2, 'analysis.summary@1', 'submitted')"
            )
            migration.downgrade()
        assert db.exec_driver_sql("SELECT id FROM a2a_tasks").scalars().all() == [1]
        transaction.rollback()


def test_typed_database_failure_creates_no_reservation(executing, monkeypatch):
    from types import SimpleNamespace

    from sqlalchemy.exc import OperationalError

    def unavailable():
        raise OperationalError("private diagnostic", None, None)

    monkeypatch.setattr(typed_tasks, "SessionLocal", SimpleNamespace(begin=unavailable))
    response = rpc(executing, submission())
    assert response.status_code == 503
    assert "private diagnostic" not in response.text
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(A2ATask)) == 0


def test_seccomp_child_enforces_cpu_limit():
    runner = repr(str(typed_worker.RUNNER))
    code = f"import runpy\nrunpy.run_path({runner})['isolate']()\nwhile True: pass\n"
    proc = subprocess.run(
        [sys.executable, "-I", "-S", "-c", code],
        stderr=subprocess.DEVNULL,
        env={},
        timeout=6,
        check=False,
    )
    assert proc.returncode == -signal.SIGKILL
