import hashlib
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
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
