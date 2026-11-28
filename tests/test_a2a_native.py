"""Native wire uses the same sovereign peer and task boundary as 0.3."""

import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from time import sleep
from uuid import UUID, uuid4

import httpx
import pytest
import test_peer_grants
from test_lineage_v2 import _keypair
from test_remote_agent_resolution import _card, _proof, _root_lineage
from test_typed_execution import SKILLS, enroll_typed, request, submission

from agent_commons import a2a_native, outbound_https, typed_tasks
from agent_commons.client import CommonsClient, EndpointVerificationError, OutboundResponseError
from agent_commons.config import settings
from agent_commons.db import SessionLocal
from agent_commons.outbound_a2a import DestinationApproval
from agent_commons.peer_enrollment import approve_peer
from agent_commons.peer_models import PeerGrant
from agent_commons.remote_resolution import _store_reference
from scripts import agent_internet_live as demo
from scripts.agent_collaboration_live import local_command, local_dispatch
from scripts.agent_internet_live_support import Server, isolated_databases, make_tls

enrollment = test_peer_grants.enrollment
SCOPES = ["message/send", "tasks/get", "tasks/cancel", "tasks/list", "a2a/1.0"]


@pytest.fixture
def native_grants(enrollment, monkeypatch):
    http, _, key, root, recipient, ref = enrollment
    for name, value in (
        ("a2a_native", True),
        ("collaboration_execution", True),
        ("preview_limits", True),
        ("preview_tasks_per_second", 100),
        ("preview_task_rate_per_grant", 100),
        ("preview_task_burst_per_grant", 32),
        ("collaboration_skills", {recipient: SKILLS}),
    ):
        monkeypatch.setattr(settings, name, value)

    def issued(key, root, ref, scopes):
        with SessionLocal.begin() as db:
            approval = approve_peer(
                db,
                reference_id=ref,
                expected_root=root,
                recipient=recipient,
                scopes=scopes,
                skills=[SKILLS[0]],
            )
        return enroll_typed(http, approval, key)

    own = issued(key, root, ref, SCOPES)
    other_key, public = _keypair()
    lineage = _root_lineage(public)
    card_url = "https://other.example/card"
    with SessionLocal.begin() as db:
        other_ref = _store_reference(
            card_url, _card(lineage, _proof(other_key, lineage, card_url=card_url)), db
        ).id
    other = issued(other_key, lineage["root_fingerprint"], other_ref, SCOPES)
    return http, own, other, lambda scopes: issued(key, root, ref, scopes)


def send(http, grant, body, *, native=True, headers=None):
    headers = {"A2A-Version": "1.0" if native else "0.3", **(headers or {})}
    if grant:
        headers["Authorization"] = "Bearer " + grant["token"]
    return http.post("/a2a", json=body, headers=headers)


def native_submit(old=None):
    old = old or submission()
    return request(
        "SendMessage", a2a_native.encode_request("message/send", deepcopy(old["params"]))
    )


def test_native_shared_idempotency_and_cancellation(native_grants):
    http, own, _, _ = native_grants
    old = submission()
    native = native_submit(old)
    data = native["params"]["message"]["parts"][0]["data"]["input"]
    data["values"] = [2.0, 3.0, -1.0]
    with ThreadPoolExecutor(4) as pool:
        responses = list(
            pool.map(
                lambda use_native: send(
                    http, own, native if use_native else old, native=use_native
                ).json()["result"],
                [True, False, True, False],
            )
        )
    ids = {(reply.get("task") or reply)["id"] for reply in responses}
    (identifier,) = ids
    claim = typed_tasks.claim()
    assert claim is not None and str(claim.id) == identifier
    assert typed_tasks.claim() is None
    id_param = {"id": identifier}
    assert send(http, own, request("GetTask", id_param)).json()["result"]["status"]["state"] == (
        "TASK_STATE_WORKING"
    )
    canceled = send(http, own, request("CancelTask", id_param)).json()["result"]
    assert canceled["status"]["state"] == "TASK_STATE_CANCELED"
    assert send(http, own, request("CancelTask", id_param)).json()["result"] == canceled
    assert not typed_tasks.finish(claim, {"count": 3, "total": 4, "minimum": -1, "maximum": 3})
    assert send(http, own, request("tasks/get", id_param), native=False).json()["result"][
        "status"
    ] == {"state": "canceled"}
    data["values"] = [3]
    assert send(http, own, native).json()["error"]["code"] == -32010


def test_native_numeric_and_version_validation(native_grants):
    http, own, _, _ = native_grants
    body = json.dumps(native_submit()).replace("[2, 3, -1]", "[2.00000000000000000001, 3, -1]")
    result = http.post(
        "/a2a",
        content=body,
        headers={
            "Authorization": "Bearer " + own["token"],
            "A2A-Version": "1.0",
            "Content-Type": "application/a2a+json",
        },
    )
    assert result.json()["error"]["code"] == -32602
    assert typed_tasks.claim() is None
    assert send(http, own, native_submit(), native=False).json()["error"]["code"] == -32601
    assert (
        send(http, own, native_submit(), headers={"A2A-Version": "2.0"}).json()["error"]["code"]
        == -32009
    )
    assert send(http, own, request("SubscribeToTask", {})).json()["error"]["code"] == -32601
    assert send(http, None, native_submit()).status_code == 401


def test_native_list_scope_and_cursor_ownership(native_grants):
    http, own, other, _ = native_grants
    first = send(http, own, native_submit()).json()["result"]["task"]
    second = send(http, own, native_submit()).json()["result"]["task"]
    params = {"pageSize": 1}
    result = send(http, own, request("ListTasks", params)).json()["result"]
    assert [task["id"] for task in result["tasks"]] == [second["id"]]
    assert result["totalSize"] == 2
    assert result["tasks"][0]["status"]["timestamp"]
    params["pageToken"] = result["nextPageToken"]
    assert params["pageToken"]
    final = send(http, own, request("ListTasks", params)).json()["result"]
    assert [task["id"] for task in final["tasks"]] == [first["id"]]
    assert final["nextPageToken"] == ""
    assert send(http, other, request("ListTasks", {})).json()["result"]["tasks"] == []
    assert send(http, other, request("ListTasks", params)).json()["error"]["code"] == -32602
    for method in ("GetTask", "CancelTask"):
        assert (
            send(http, other, request(method, {"id": first["id"]})).json()["error"]["code"]
            == -32001
        )
    assert (
        send(http, own, request("ListTasks", {"tenant": "other"})).json()["error"]["code"] == -32602
    )


def test_native_credential_and_quota_fail_closed(native_grants, monkeypatch):
    http, own, other, replace = native_grants
    with SessionLocal.begin() as db:
        db.get(PeerGrant, UUID(other["grant_id"])).expires_at = datetime.now(UTC) - timedelta(
            seconds=1
        )
        db.get(PeerGrant, UUID(own["grant_id"])).revoked_at = datetime.now(UTC)
    for grant in (own, other):
        assert send(http, grant, native_submit()).status_code == 401
    legacy = replace(["message/send", "tasks/get"])
    assert send(http, legacy, native_submit()).status_code == 403
    limited = replace(["message/send", "a2a/1.0"])
    assert send(http, limited, request("ListTasks", {})).status_code == 403
    restricted = send(
        http,
        limited,
        native_submit(
            submission(
                skill=SKILLS[1],
                input={
                    "title": "x",
                    "summary": {"count": 1, "total": 2, "minimum": 2, "maximum": 2},
                },
            )
        ),
    )
    assert restricted.status_code == 403
    granted = replace(SCOPES)
    sleep(1.05)
    monkeypatch.setattr(settings, "preview_tasks_per_second", 1)
    assert send(http, granted, native_submit()).status_code == 200
    quota = send(http, granted, native_submit())
    assert quota.status_code == 429 and quota.headers["Retry-After"]
    assert quota.json()["error"]["code"] == -32012
    assert (
        send(http, granted, request("ListTasks", {}), headers={"Host": "wrong.example"}).status_code
        == 403
    )


@pytest.mark.skipif(
    os.environ.get("AGENT_COMMONS_A2A_SDK_SPIKE") != "1",
    reason="Requires pinned upstream A2A SDK and disposable PostgreSQL",
)
def test_native_sdk_over_loopback(tmp_path, monkeypatch):
    from a2a.client import A2ACardResolver
    from a2a.client.transports.jsonrpc import JsonRpcTransport
    from a2a.types import a2a_pb2 as pb
    from google.protobuf.json_format import ParseDict

    assert version("a2a-sdk") == "1.3.1"
    dsn = os.environ["AGENT_COMMONS_DEMO_DATABASE_URL"]
    cert, key, tls = make_tls(tmp_path)
    with isolated_databases(dsn, ("atlas", "source")) as databases, ExitStack() as stack:
        http = stack.enter_context(httpx.Client(verify=tls, trust_env=False, timeout=5))
        home, receiver = [Server.provision(stack, db) for db in databases.values()]
        cards = [home.url + demo.card_path("atlas"), receiver.url + demo.card_path("nova")]
        for node in (home, receiver):
            node.start(http, cert, key, cards)
        atlas = demo.register(http, home, "atlas")
        nova = demo.register(http, receiver, "nova")
        demo.publish(http, home, atlas, 1)
        native_card = cards[1] + "/v1"
        endpoint = receiver.url + "/a2a"
        with CommonsClient(
            receiver.url, api_key=nova.api_key, transport=httpx.HTTPTransport(verify=tls)
        ) as author:
            author.publish_card(
                nova.key,
                card_url=cards[1],
                endpoint=endpoint,
                location_sequence=1,
                native_card_url=native_card,
            )
        reference = demo.resolve(http, home, atlas, receiver, "nova")
        caller = demo.resolve(http, receiver, nova, home, "atlas")
        skills = {nova.agent_id: SKILLS}
        environment = Server.environment

        def native_environment(self):
            return environment(self) | {"AGENT_COMMONS_A2A_NATIVE": "true"}

        monkeypatch.setattr(Server, "environment", native_environment)
        receiver.start(http, cert, key, cards, collaboration=skills)
        approval = UUID(
            local_command(
                receiver,
                skills,
                "agent_commons.cli",
                "peer-approve",
                "--reference",
                caller["id"],
                "--caller-root",
                atlas.root,
                "--recipient",
                nova.agent_id,
                "--skill",
                SKILLS[0],
                *[arg for scope in SCOPES for arg in ("--scope", scope)],
            ).removeprefix("Enrollment approval: ")
        )
        transport = httpx.HTTPTransport(verify=tls)
        client = stack.enter_context(
            CommonsClient(home.url, api_key=atlas.api_key, transport=transport)
        )
        stack.enter_context(local_dispatch({"home": home, "receiver": receiver}, cert))
        resolve_ip = outbound_https._resolve

        def fixture_address(url, timeout):
            if str(url) == native_card:
                return "127.0.0.1"
            return resolve_ip(url, timeout)

        monkeypatch.setattr(outbound_https, "_resolve", fixture_address)
        grant = client.enroll_dispatch_peer(
            atlas.key,
            destination=DestinationApproval(
                reference_id=UUID(reference["id"]),
                peer_root=nova.root,
                controller=reference["current_controller_public_key"],
                recipient_agent_id=UUID(nova.agent_id),
                endpoint=endpoint,
                native_card_url=native_card,
            ),
            approval_id=approval,
            caller_root=atlas.root,
            scopes=SCOPES,
            skills=[SKILLS[0]],
        )
        deadline = datetime.now(UTC) + timedelta(seconds=18)
        task = client.delegate(
            grant=grant,
            skill=SKILLS[0],
            data={"values": [2, 3, -1]},
            message_id="native-example",
            deadline=deadline,
        )
        local_command(receiver, skills, "agent_commons.typed_worker", "--once")

        async def exercise():
            async with httpx.AsyncClient(verify=tls, trust_env=False) as public:
                card = await A2ACardResolver(
                    public, receiver.url, demo.card_path("nova") + "/v1"
                ).get_agent_card()
            assert card.supported_interfaces[0].protocol_version == "1.0"
            assert card.supported_interfaces[0].url == endpoint
            assert card.security_schemes["peer"].http_auth_security_scheme.scheme == "bearer"
            async with httpx.AsyncClient(
                verify=tls,
                trust_env=False,
                headers={
                    "A2A-Version": "1.0",
                    "Authorization": "Bearer " + grant.grant.token.get_secret_value(),
                },
            ) as authenticated:
                upstream = JsonRpcTransport(authenticated, card, endpoint)
                result = await upstream.get_task(pb.GetTaskRequest(id=str(task.id)))
                assert result.status.state == pb.TASK_STATE_COMPLETED
                assert result.artifacts[0].parts[0].data.struct_value["total"] == 4
                page = await upstream.list_tasks(pb.ListTasksRequest(page_size=1))
                assert [item.id for item in page.tasks] == [str(task.id)]
                message = ParseDict(
                    {
                        "message": {
                            "messageId": "native-example",
                            "role": "ROLE_USER",
                            "parts": [
                                {
                                    "data": {
                                        "skill": SKILLS[0],
                                        "input": {"values": [2, 3, -1]},
                                        "deadline": deadline.isoformat(),
                                    }
                                }
                            ],
                        }
                    },
                    pb.SendMessageRequest(),
                )
                assert (await upstream.send_message(message)).task.id == str(task.id)
                await asyncio.sleep(1.1)
                message.message.message_id = "upstream-cancel"
                message.message.parts[0].data.struct_value["deadline"] = (
                    datetime.now(UTC) + timedelta(seconds=15)
                ).isoformat()
                pending = (await upstream.send_message(message)).task
                canceled = await upstream.cancel_task(pb.CancelTaskRequest(id=pending.id))
                assert canceled.status.state == pb.TASK_STATE_CANCELED

        asyncio.run(exercise())
        assert client.get_task_status(task.reference, grant=grant).artifact["total"] == 4
        owner_page = client.list_native_tasks(grant, page_size=1)
        assert owner_page.nextPageToken
        assert (
            client.list_native_tasks(grant, page_size=1, cursor=owner_page.nextPageToken)
            .tasks[0]
            .id
            == task.id
        )
        wire = outbound_https.request
        bearer_calls = []

        for mismatch in ("version", "epoch", "signature"):

            def corrupt(url, *, mismatch=mismatch, **kwargs):
                if kwargs.get("bearer"):
                    bearer_calls.append(url)
                raw = wire(url, **kwargs)
                if url != native_card:
                    return raw
                card = json.loads(raw)
                proof = card["capabilities"]["extensions"][0]["params"]["endpointAuthorization"]
                if mismatch == "version":
                    card["supportedInterfaces"][0]["protocolVersion"] = "0.3.0"
                elif mismatch == "epoch":
                    proof["authorization"]["location_sequence"] += 1
                elif mismatch == "signature":
                    proof["signature_multibase"] = "zFake"
                return json.dumps(card).encode()

            with monkeypatch.context() as patch:
                patch.setattr(outbound_https, "request", corrupt)
                with pytest.raises(EndpointVerificationError):
                    client.get_task_status(task.reference, grant=grant)
        assert bearer_calls == []

        wrong_recipient = grant.model_copy(
            update={
                "destination": grant.destination.model_copy(update={"recipient_agent_id": uuid4()})
            }
        )
        with monkeypatch.context() as patch:
            patch.setattr(
                outbound_https, "request", lambda url, **kw: corrupt(url, mismatch=None, **kw)
            )
            with pytest.raises(EndpointVerificationError):
                client.get_task_status(task.reference, grant=wrong_recipient)
        assert bearer_calls == []

        def invalid_reply(url, **kwargs):
            if kwargs.get("bearer"):
                bearer_calls.append(kwargs["native"])
                body = json.loads(kwargs["body"])
                return json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": {}}).encode()
            return wire(url, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(outbound_https, "request", invalid_reply)
            with pytest.raises(OutboundResponseError):
                client.get_task_status(task.reference, grant=grant)
        assert bearer_calls == [True]


def test_pagination_migration_round_trip_preserves_tasks():
    from importlib import import_module

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    from agent_commons.db import engine

    migration = import_module("migrations.versions.0021_a2a_task_pagination")
    schema = f"native_migration_{uuid4().hex}"
    identifier = uuid4()
    with engine.connect() as db, db.begin() as transaction:
        db.exec_driver_sql(f"CREATE SCHEMA {schema}")
        db.exec_driver_sql(f"SET LOCAL search_path TO {schema}, pg_catalog")
        db.exec_driver_sql(
            "CREATE TABLE a2a_tasks (id uuid PRIMARY KEY, caller_root text, "
            "recipient_agent_id uuid, expires_at timestamptz, state text)"
        )
        db.exec_driver_sql(
            "INSERT INTO a2a_tasks VALUES (%s, 'caller', %s, now() + interval '1 day', %s)",
            (identifier, uuid4(), "completed"),
        )
        with Operations.context(MigrationContext.configure(db)):
            migration.upgrade()
            assert db.exec_driver_sql("SELECT updated_at = now() FROM a2a_tasks").scalar()
            migration.downgrade()
            assert db.exec_driver_sql("SELECT id, state FROM a2a_tasks").one() == (
                identifier,
                "completed",
            )
            migration.upgrade()
        assert db.exec_driver_sql("SELECT id FROM a2a_tasks").scalar() == identifier
        transaction.rollback()
