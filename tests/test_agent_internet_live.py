import json
import os
import re
from pathlib import Path
from uuid import UUID

import psycopg
import pytest

from scripts import agent_internet_live as demo


@pytest.fixture
def admin_dsn() -> str:
    dsn = os.environ.get("AGENT_COMMONS_DEMO_DATABASE_URL")
    if not dsn:
        pytest.skip("Requires a disposable local PostgreSQL admin connection")
    return dsn


@pytest.fixture
def resources(monkeypatch):
    processes = []
    directories = []
    start = demo.Server.start
    make_tls = demo.make_tls

    def record_process(self, *args, **kwargs):
        try:
            start(self, *args, **kwargs)
        finally:
            if self.process is not None:
                processes.append(self.process)

    def record_directory(directory: Path):
        directories.append(directory)
        return make_tls(directory)

    monkeypatch.setattr(demo.Server, "start", record_process)
    monkeypatch.setattr(demo, "make_tls", record_directory)
    yield
    assert processes and all(process.poll() is not None for process in processes)
    assert len(directories) == 1 and not directories[0].exists()


def test_live_exchange_and_migration(admin_dsn, resources):
    evidence = demo.run(admin_dsn)
    assert set(evidence) == {
        "schema_version",
        "topology",
        "nodes",
        "identities",
        "exchanges",
        "migration",
        "checks",
    }
    assert evidence["schema_version"] == 1
    assert evidence["topology"] == "isolated-loopback-https"
    assert evidence["nodes"] == ["atlas", "nova-source", "nova-target"]
    assert set(evidence["identities"]) == {"atlas", "nova"}
    assert all(
        re.fullmatch(r"sha256:[0-9a-f]{64}", root) for root in evidence["identities"].values()
    )
    assert len(set(evidence["identities"].values())) == 2
    assert [
        (item["phase"], item["source"], item["destination"]) for item in evidence["exchanges"]
    ] == [
        ("before", "atlas", "nova-source"),
        ("before", "nova-source", "atlas"),
        ("after", "atlas", "nova-target"),
    ]
    for item in evidence["exchanges"]:
        assert set(item) == {"phase", "source", "destination", "task_id", "state", "retrieved"}
        assert UUID(item["task_id"])
        assert item["state"] == "completed" and item["retrieved"] is True
    migration = evidence["migration"]
    assert migration == {
        "root_fingerprint": evidence["identities"]["nova"],
        "reference_id_before": str(UUID(migration["reference_id_after"])),
        "reference_id_after": str(UUID(migration["reference_id_before"])),
        "follow_id_before": str(UUID(migration["follow_id_after"])),
        "follow_id_after": str(UUID(migration["follow_id_before"])),
        "reverse_follow_id": str(UUID(migration["reverse_follow_id"])),
        "location_sequence_before": 1,
        "location_sequence_after": 2,
        "endpoint_authorization_before": "authorized",
        "endpoint_authorization_after": "authorized",
        "old_task_endpoint_retired": True,
        "exclusive_control_verified": False,
    }
    assert evidence["checks"] == dict.fromkeys(
        {
            "database_isolation",
            "bad_credentials_rejected",
            "wrong_peer_rejected",
            "expired_credential_rejected",
            "invalid_signature_rejected",
            "stale_location_rejected",
            "idempotent_retry",
            "conflicting_retry_rejected",
            "follow_alone_denied",
            "moved_location_requires_new_enrollment",
            "cleanup_completed",
        },
        True,
    )

    if path := os.environ.get("AGENT_COMMONS_DEMO_EVIDENCE_PATH"):
        Path(path).write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")


def test_failed_demo_cleans_up_live_server_and_database(admin_dsn, resources, monkeypatch):
    database_names = []

    def fail(client, nodes, cert, key):
        database_names.extend(node.database.database for node in nodes.values())
        nodes["atlas"].start(client, cert, key, [])
        raise RuntimeError("Intentional integration failure")

    monkeypatch.setattr(demo, "demonstrate", fail)
    with pytest.raises(RuntimeError, match="Intentional integration failure"):
        demo.run(admin_dsn)

    with psycopg.connect(admin_dsn) as db:
        assert not db.execute(
            "SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname = ANY(%s)) OR "
            "EXISTS(SELECT 1 FROM pg_roles WHERE rolname = ANY(%s))",
            (database_names, database_names),
        ).fetchone()[0]
