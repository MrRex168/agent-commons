import hashlib
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from jsonschema import validate as validate_schema

from scripts.staging import migrate, validate

SCHEMA = Path(__file__).resolve().parents[1] / "docs/open-agent-network/staging/report.schema.json"


def check_report(data):
    report = json.loads(data)
    validate_schema(report, json.loads(SCHEMA.read_text()))
    return report


def test_missing_manifest_fails_without_marking_remote_checks_passed(monkeypatch, tmp_path, capsys):
    path = tmp_path / "private-name-not-for-report"
    monkeypatch.setattr(sys, "argv", ["validate", "smoke", "--manifest", str(path)])
    assert validate.main() == 1
    output = capsys.readouterr()
    report = check_report(output.out)
    assert report["checks"]["preflight"] == "fail"
    assert {value for key, value in report["checks"].items() if key != "preflight"} == {
        "not_executed"
    }
    assert path.name not in output.out + output.err


@pytest.mark.parametrize("retained,groups,result", [(1024, 0, 0), (1025, 1, 1)])
def test_storage_audit_verdicts(monkeypatch, tmp_path, capsys, retained, groups, result):
    path = tmp_path / "storage.json"
    path.write_text(json.dumps({"retained": retained, "duplicate_groups": groups}))
    path.chmod(0o600)
    monkeypatch.setattr(sys, "argv", ["validate", "audit", "--audit-file", str(path)])
    assert validate.main() == result
    report = check_report(capsys.readouterr().out)
    expected = "pass" if result == 0 else "fail"
    assert report["checks"]["retention"] == report["checks"]["no_duplicate_tasks"] == expected
    assert report["checks"]["load"] == "not_executed"


def test_migration_challenge_rejects_wrong_audience_or_package():
    destination = "https://receiver.example"
    root = "sha256:" + "f" * 64
    package = {"envelope": {"payload": "fixture"}, "lineage": {}}
    digest = hashlib.sha256(
        json.dumps(package, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    now = datetime.now(UTC)
    expiry = (now + timedelta(minutes=4)).isoformat()
    identifier = str(uuid4())
    challenge = {
        "challenge_id": identifier,
        "fingerprint": root,
        "requested_name": "fixture",
        "expires_at": expiry,
        "payload": "\n".join(
            [
                "agent-commons/lineage-migration-challenge/v1",
                f"audience:{destination}",
                "operation:migrate-lineage",
                "nonce:fixture",
                f"identity:{root}",
                "current_key:zcontroller",
                f"package_digest:{digest}",
                "requested_name:fixture",
                f"issued_at:{now.isoformat()}",
                f"expires_at:{expiry}",
            ]
        ),
    }
    assert (
        migrate.check_challenge(challenge, package, destination, root, "zcontroller", "fixture")
        == identifier
    )
    with pytest.raises(validate.InputError):
        migrate.check_challenge(
            challenge, package, "https://unexpected.example", root, "zcontroller", "fixture"
        )
    with pytest.raises(validate.InputError):
        migrate.check_challenge(challenge, {}, destination, root, "zcontroller", "fixture")


def test_p95_counts_tail_without_interpolation():
    assert validate.p95([]) is None
    assert validate.p95(range(1, 21)) == 19
    assert validate.p95([1, 1, 800]) == 800


def test_operator_attestation_cannot_mark_network_workflow_passed(monkeypatch, tmp_path, capsys):
    path = tmp_path / "operator.json"
    path.write_text(json.dumps(dict.fromkeys(validate.OPERATOR_CHECKS, "pass")))
    path.chmod(0o600)
    monkeypatch.setattr(sys, "argv", ["validate", "operator", "--audit-file", str(path)])
    assert validate.main() == 0
    report = check_report(capsys.readouterr().out)
    assert report["checks"]["egress"] == "pass"
    assert report["checks"]["send_get"] == report["checks"]["load"] == "not_executed"


def test_quota_measurement_has_fixed_numeric_fields(monkeypatch, tmp_path, capsys):
    path = tmp_path / "quota.json"
    path.write_text(
        json.dumps(
            {
                "bucket": {"mean_ms": 0.5, "calls": 27},
                "grant": {"mean_ms": 0.3, "calls": 30},
            }
        )
    )
    path.chmod(0o600)
    monkeypatch.setattr(sys, "argv", ["validate", "quota-stats", "--audit-file", str(path)])
    assert validate.main() == 0
    report = check_report(capsys.readouterr().out)
    assert report["metrics"]["quota_db_timing"]["grant"]["calls"] == 30
    assert report["checks"]["quota_db_timing"] == "pass"
    assert report["checks"]["load"] == "not_executed"


def test_functional_schedule_preserves_capacity_schedule():
    functional = [list(validate._offsets(i, True)) for i in range(5)]
    assert functional == [[0, 30], [6, 36], [12, 42], [18, 48], [24, 54]]
    for i in range(5):
        capacity = list(validate._offsets(i, False))
        assert capacity == [second + i / 5 for second in range(60)]


@pytest.fixture
def workload_fixture(monkeypatch):
    saved = {}
    options = SimpleNamespace(resolver_429=False, receiver_429=0, latency=50, leak=False)

    class Client(validate.MeasuredClient):
        def __init__(self, peer):
            super().__init__("https://home.example", api_key="local-fixture-key")
            self.index = peer[0]

        def _verified_destination(self, approved):
            self.refresh_ms.append(10)
            return SimpleNamespace(
                identity_sequence=1,
                location_sequence=1,
                expiration=lambda: datetime.now(UTC) + timedelta(minutes=10),
            )

        def record(self):
            self.refresh_ms.append(10)
            self.call_ms.append(options.latency)
            self.rpc_ms.append(40)
            self.success_ms.append(options.latency)

        def send_task(self, grant, text, *, message_id):
            if options.resolver_429:
                self.refresh_ms.append(10)
                self.call_ms.append(15)
                self.refresh_status[429] = self.refresh_status.get(429, 0) + 1
                raise validate.CommonsError("Rejected")
            if options.receiver_429:
                options.receiver_429 -= 1
                self.refresh_ms.append(10)
                self.call_ms.append(50)
                self.rpc_ms.append(40)
                self.quota_reply_ms.append(40)
                raise validate.CommonsError("Rejected", 429)
            self.record()
            if message_id not in saved:
                saved[message_id] = (
                    self.index,
                    SimpleNamespace(id=uuid4(), bytes=len(text), words=len(text.split())),
                )
            return saved[message_id][1]

        def get_task(self, grant, task_id):
            owner, task = next(item for item in saved.values() if item[1].id == task_id)
            if owner != self.index and not options.leak:
                self.refresh_ms.append(10)
                raise validate.CommonsError("Task not found")
            self.record()
            return task

    peers = [
        (
            index,
            SimpleNamespace(
                grant=SimpleNamespace(caller_root=str(index), grant_id=uuid4()),
                destination=SimpleNamespace(endpoint="https://receiver.example/a2a"),
                identity_sequence=1,
                location_sequence=1,
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
            ),
        )
        for index in range(5)
    ]
    monkeypatch.setattr(validate, "_client", Client)
    monkeypatch.setattr(validate, "sleep", lambda _: None)
    return peers, options


def test_functional_report_checks_ownership_and_separates_setup(workload_fixture):
    peers, _ = workload_fixture
    report = validate.Report("functional")
    assert validate.functional(report, peers)
    checked = check_report(json.dumps(report.data))
    assert checked["checks"]["functional"] == "pass"
    assert checked["checks"]["load"] == "not_executed"
    assert checked["checks"]["ownership"] == "pass"
    assert checked["checks"]["idempotency"] == "pass"
    assert checked["metrics"]["completed_pairs_per_peer"] == [2] * 5
    assert checked["metrics"]["setup_card_refreshes"] == 5
    assert checked["metrics"]["post_run_card_refreshes"] == 10
    assert checked["metrics"]["successful_call_p95_ms"] == 50


@pytest.mark.parametrize(
    "option,value,check",
    [
        ("resolver_429", True, "functional_quotas"),
        ("receiver_429", 1, "functional_quotas"),
        ("latency", 751, "functional_deadlines"),
        ("leak", True, "ownership"),
    ],
)
def test_functional_rejects_quota_latency_and_ownership_failures(
    workload_fixture, option, value, check
):
    peers, options = workload_fixture
    setattr(options, option, value)
    report = validate.Report("functional")
    assert not validate.functional(report, peers)
    assert report.data["checks"][check] == "fail"
    assert report.data["checks"]["load"] == "not_executed"


def test_capacity_rejects_296_pairs_even_when_latency_passes(workload_fixture):
    peers, options = workload_fixture
    options.receiver_429 = 4
    report = validate.Report("load")
    assert not validate.load(report, peers)
    assert report.data["metrics"]["completed_pairs"] == 296
    assert report.data["metrics"]["receiver_429_responses"] == 4
    assert report.data["checks"]["functional"] == "not_executed"


def test_functional_duplicate_caller_fails_setup(workload_fixture):
    peers, _ = workload_fixture
    report = validate.Report("functional")
    assert not validate.functional(report, [*peers[:4], peers[0]])
    assert report.data["checks"]["functional_setup"] == "fail"
    assert report.data["checks"]["ownership"] == "not_executed"
