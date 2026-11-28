"""Rehearse the v0.5 schema upgrade and a cold restore with real peer operations."""

import hashlib
import os
import subprocess
from contextlib import ExitStack
from uuid import uuid4

import httpx
import psycopg
import pytest
from psycopg.types.json import Jsonb
from pydantic import SecretStr

from agent_commons.a2a_tasks import _completed_task
from agent_commons.client import CommonsClient
from scripts.agent_collaboration_live import (
    approve_and_enroll,
    finish,
    local_dispatch,
    task_arguments,
)
from scripts.agent_internet_live import (
    AGENTS,
    FOLLOWS,
    card_path,
    follow,
    grant,
    publish,
    register,
    request,
    resolve,
    rpc,
    send_message,
)
from scripts.agent_internet_live_support import Server, _driver_url, isolated_databases, make_tls
from scripts.collaboration_live_evidence import SUMMARY


def backup_tool(database, *command, source=None, destination=None):
    # Pass the disposable role credentials in the child environment, outside argv and logs.
    subprocess.run(
        command,
        env={
            "PATH": os.environ["PATH"],
            "PGHOST": database.host,
            "PGPORT": str(database.port or 5432),
            "PGUSER": database.username,
            "PGPASSWORD": database.password,
            "PGDATABASE": database.database,
        },
        stdin=source,
        stdout=destination if destination is not None else subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
        timeout=30,
    )


def restore_database(source, destination, dump):
    try:
        with dump.open("xb") as archive:
            backup_tool(
                source,
                "pg_dump",
                "--format=custom",
                "--no-owner",
                "--no-acl",
                destination=archive,
            )
        with dump.open("rb") as archive:
            backup_tool(
                destination,
                "pg_restore",
                "--dbname",
                destination.database,
                "--exit-on-error",
                "--single-transaction",
                "--no-owner",
                "--no-acl",
                source=archive,
            )
    finally:
        dump.unlink(missing_ok=True)


def seed_v05_task(node, caller, recipient, result):
    with psycopg.connect(_driver_url(node.database)) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0014_a2a_tasks",
        )
        db.execute(
            "INSERT INTO a2a_tasks "
            "(id, caller_root, recipient_agent_id, message_id, request_digest, result, expires_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, now() + interval '1 day')",
            (
                result["id"],
                caller.root,
                recipient.agent_id,
                uuid4().hex,
                hashlib.sha256(b"test message").hexdigest(),
                Jsonb(result),
            ),
        )


def get_task(client, node, token, identifier):
    return request(
        client,
        "POST",
        node,
        "/a2a",
        token=token,
        body=rpc("tasks/get", {"id": str(identifier)}),
    )["result"]


def assert_drained(node):
    with psycopg.connect(_driver_url(node.database)) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0020_collaboration_history",
        )
        assert db.execute(
            "SELECT count(*) FROM a2a_tasks WHERE state IN ('submitted', 'working')"
        ).fetchone() == (0,)


@pytest.mark.skipif(
    os.environ.get("AGENT_COMMONS_PREVIEW_RESTORE") != "1",
    reason="Requires disposable PostgreSQL and pg_dump/pg_restore on PATH",
)
def test_preview_backup_restore(tmp_path):
    admin_dsn = SecretStr(os.environ["AGENT_COMMONS_DEMO_DATABASE_URL"])
    cert, key, tls = make_tls(tmp_path)
    labels = ("atlas", "source")
    with (
        isolated_databases(admin_dsn.get_secret_value(), labels) as source,
        isolated_databases(admin_dsn.get_secret_value(), labels) as restored,
        isolated_databases(admin_dsn.get_secret_value(), ("atlas",)) as baseline,
        ExitStack() as resources,
        httpx.Client(verify=tls, trust_env=False, follow_redirects=False, timeout=10) as http,
    ):
        nodes = {
            name: Server.provision(resources, db, revision="0014_a2a_tasks")
            for name, db in source.items()
        }
        atlas_node, nova_node = (nodes[name] for name in labels)
        cards = [atlas_node.url + card_path("atlas"), nova_node.url + card_path("nova")]
        for node in nodes.values():
            node.start(http, cert, key, cards)
        atlas = register(http, atlas_node, "atlas")
        nova = register(http, nova_node, "nova")
        publish(http, atlas_node, atlas, 1)
        publish(http, nova_node, nova, 1)
        existing = resolve(http, atlas_node, atlas, nova_node, "nova")
        relationship = follow(http, atlas_node, atlas, existing)
        legacy = _completed_task(uuid4(), "test message", 12)
        seed_v05_task(atlas_node, nova, atlas, legacy)
        legacy_token, legacy_peer = grant(nova, atlas)
        skills = {nova.agent_id: [SUMMARY]}

        for node in nodes.values():
            node.stop()
        restore_database(atlas_node.database, baseline["atlas"], tmp_path / "baseline.dump")
        with psycopg.connect(_driver_url(baseline["atlas"])) as db:
            assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (
                "0014_a2a_tasks",
            )
            assert db.execute("SELECT result FROM a2a_tasks").fetchone() == (legacy,)
            assert db.execute("SELECT count(*) FROM remote_agent_relationships").fetchone() == (1,)
        baseline_atlas = Server(atlas_node.listener, baseline["atlas"])
        baseline_atlas.upgrade()
        resources.callback(baseline_atlas.stop)
        baseline_atlas.start(http, cert, key, cards, peers=[legacy_peer])
        assert get_task(http, baseline_atlas, legacy_token, legacy["id"]) == legacy
        baseline_atlas.stop()

        for node in nodes.values():
            node.upgrade()
        atlas_node.start(http, cert, key, cards, peers=[legacy_peer])
        nova_node.start(http, cert, key, cards, collaboration=skills)
        assert get_task(http, atlas_node, legacy_token, legacy["id"]) == legacy

        with (
            local_dispatch(nodes, cert),
            CommonsClient(
                atlas_node.url,
                api_key=atlas.api_key,
                transport=httpx.HTTPTransport(verify=tls, trust_env=False),
            ) as sdk,
        ):
            peer = approve_and_enroll(
                http,
                sdk,
                atlas,
                nova,
                nova_node,
                existing,
                SUMMARY,
                atlas_node,
                skills,
            )
            submitted = sdk.delegate(**task_arguments(peer, SUMMARY, {"values": [2, 3, -1]}))
            assert submitted.status == "submitted"
            completed = finish(sdk, submitted, peer, nova_node, skills)
            history = sdk.list_collaboration_history()
            assert len(history.records) == 1 and history.records[0].artifact_digest
            identities = [
                request(http, "GET", node, f"{AGENTS}/me/identity/lineage", token=agent.api_key)
                for node, agent in ((atlas_node, atlas), (nova_node, nova))
            ]
            for name, node in nodes.items():
                # This fixture is the only writer. Finish the submitted work before shutdown.
                node.stop()
                assert_drained(node)
                restore_database(node.database, restored[name], tmp_path / f"{name}.dump")

            atlas_recovered, nova_recovered = (
                Server(nodes[name].listener, restored[name]) for name in labels
            )
            for node in (atlas_recovered, nova_recovered):
                resources.callback(node.stop)
                node.upgrade()  # HEAD restores must also accept the normal startup check.
                assert_drained(node)
            atlas_recovered.start(http, cert, key, cards, peers=[legacy_peer])
            nova_recovered.start(http, cert, key, cards, collaboration=skills)
            assert [
                request(http, "GET", node, f"{AGENTS}/me/identity/lineage", token=agent.api_key)
                for node, agent in ((atlas_recovered, atlas), (nova_recovered, nova))
            ] == identities
            (followed,) = request(http, "GET", atlas_recovered, FOLLOWS, token=atlas.api_key)
            assert followed["id"] == relationship["id"]
            assert followed["remote_reference_id"] == existing["id"]
            assert followed["remote_root_fingerprint"] == nova.root
            assert get_task(http, atlas_recovered, legacy_token, legacy["id"]) == legacy
            assert sdk.list_collaboration_history() == history
            # Current task lookup exercises the original API key, grant, binding and result.
            assert sdk.get_task_status(completed.reference, grant=peer) == completed
            reply = request(
                http,
                "POST",
                atlas_recovered,
                "/a2a",
                token=legacy_token,
                body=send_message(uuid4().hex, "recovered"),
            )
            assert reply["result"]["status"]["state"] == "completed"
            queued = sdk.delegate(**task_arguments(peer, SUMMARY, {"values": [7]}))
            fresh = finish(sdk, queued, peer, nova_recovered, skills)
            assert fresh.artifact == {"count": 1, "minimum": 7, "maximum": 7, "total": 7}
