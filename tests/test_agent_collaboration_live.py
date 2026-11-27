import json
import os
from pathlib import Path

import psycopg
import pytest
from pydantic import SecretStr, ValidationError

from agent_commons import outbound_https
from scripts import agent_collaboration_live as demo
from scripts.collaboration_live_evidence import MAX_EVIDENCE_BYTES, Evidence, FailedReport


@pytest.fixture
def admin_dsn():
    if not (dsn := os.environ.get("AGENT_COMMONS_DEMO_DATABASE_URL")):
        pytest.skip("Requires a disposable local PostgreSQL admin connection")
    return SecretStr(dsn)


@pytest.fixture
def resources(monkeypatch):
    processes = []
    directories = []
    database_names = []
    start = demo.Server.start
    make_tls = demo.make_tls

    def record_process(self, *args, **kwargs):
        try:
            start(self, *args, **kwargs)
        finally:
            if self.process is not None:
                processes.append(self.process)
                database_names.append(self.database.database)

    def record_directory(directory: Path):
        directories.append(directory)
        return make_tls(directory)

    monkeypatch.setattr(demo.Server, "start", record_process)
    monkeypatch.setattr(demo, "make_tls", record_directory)
    yield database_names
    assert processes and all(process.poll() is not None for process in processes)
    assert directories and all(not directory.exists() for directory in directories)


def assert_database_cleanup(dsn, names):
    with psycopg.connect(dsn) as db:
        assert not db.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = ANY(%s)) OR "
            "EXISTS (SELECT 1 FROM pg_roles WHERE rolname = ANY(%s))",
            (names, names),
        ).fetchone()[0]


def run_demo(dsn):
    try:
        return demo.run(dsn)
    except demo.TypedTaskError as exc:
        pytest.fail(f"Demo task failed: {exc.reason}", pytrace=False)
    except Exception as exc:
        detail = str(exc) if type(exc) is RuntimeError else type(exc).__name__
        pytest.fail(f"Demo failed: {detail}", pytrace=False)


def test_typed_collaboration_repeats_with_sanitized_evidence(admin_dsn, resources):
    dsn = admin_dsn.get_secret_value()
    first = run_demo(dsn)
    latest = run_demo(dsn)
    assert first.identities != latest.identities
    assert len(set(resources)) == 8
    assert_database_cleanup(dsn, resources)
    assert [row.origin for row in latest.tasks] == ["nova-source", "orion", "nova-target"]
    assert [row.state for row in latest.tasks] == ["completed"] * 3
    assert set(latest.checks.model_dump().values()) == {"pass"}
    assert latest.migration.location_before == 1 and latest.migration.location_after == 2

    payload = latest.json_report()
    if path := os.environ.get("AGENT_COMMONS_COLLABORATION_EVIDENCE_PATH"):
        Path(path).write_text(payload, encoding="utf-8")
    assert len(payload.encode()) < MAX_EVIDENCE_BYTES
    assert Evidence.model_validate_json(payload) == latest
    for private in (
        "api_key",
        "postgresql",
        "127.0.0.1",
        "BEGIN PRIVATE KEY",
        "a2c_",
        "Synthetic analysis",
        '"values"',
        '"markdown"',
    ):
        assert private not in payload
    tampered = json.loads(payload)
    tampered["tasks"][0]["input"] = {"values": [2, 3, -1]}
    with pytest.raises(ValidationError):
        Evidence.model_validate(tampered)

    # The demo's in-process exception was scoped to its context.
    with pytest.raises(outbound_https.DestinationError):
        outbound_https.request("https://127.0.0.1:9000/a2a", response_limit=500)


def test_failed_collaboration_removes_process_roles_and_tls(admin_dsn, resources, monkeypatch):
    names = []

    def fail(client, nodes, cert, key, tls):
        names.extend(node.database.database for node in nodes.values())
        nodes["atlas"].start(client, cert, key, [])
        raise RuntimeError("Intentional collaboration failure")

    monkeypatch.setattr(demo, "demonstrate", fail)
    dsn = admin_dsn.get_secret_value()
    with pytest.raises(RuntimeError, match="Intentional collaboration failure"):
        demo.run(dsn)
    assert len(names) == 4
    assert_database_cleanup(dsn, names)


def test_cli_emits_sanitized_failure_without_existing_success(tmp_path, monkeypatch, capsys):
    def fail(_dsn):
        raise RuntimeError("sensitive fixture value")

    path = tmp_path / "failed.json"
    monkeypatch.setenv("AGENT_COMMONS_DEMO_DATABASE_URL", "unused")
    monkeypatch.setattr(demo, "run", fail)
    monkeypatch.setattr("sys.argv", ["demo", "--evidence", str(path)])
    with pytest.raises(SystemExit, match="Collaboration demo failed: RuntimeError"):
        demo.main()
    assert FailedReport.model_validate_json(path.read_bytes()).outcome == "fail"
    assert "sensitive fixture value" not in capsys.readouterr().out
