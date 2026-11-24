import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scripts.agent_internet_dashboard import (
    CHECK_LABELS,
    MAX_EVIDENCE_BYTES,
    _code,
    create_app,
)


def identifier(number: int) -> str:
    return f"00000000-0000-4000-8000-{number:012d}"


@pytest.fixture
def snapshot() -> dict:
    nova_root = "sha256:" + "b" * 64
    return {
        "schema_version": 1,
        "topology": "isolated-loopback-https",
        "nodes": ["atlas", "nova-source", "nova-target"],
        "identities": {"atlas": "sha256:" + "a" * 64, "nova": nova_root},
        "exchanges": [
            {
                "phase": phase,
                "source": source,
                "destination": destination,
                "task_id": identifier(number),
                "state": "completed",
                "retrieved": True,
            }
            for number, (phase, source, destination) in enumerate(
                [
                    ("before", "atlas", "nova-source"),
                    ("before", "nova-source", "atlas"),
                    ("after", "atlas", "nova-target"),
                ],
                start=1,
            )
        ],
        "migration": {
            "root_fingerprint": nova_root,
            "reference_id_before": identifier(4),
            "reference_id_after": identifier(4),
            "follow_id_before": identifier(5),
            "follow_id_after": identifier(5),
            "reverse_follow_id": identifier(6),
            "location_sequence_before": 1,
            "location_sequence_after": 2,
            "endpoint_authorization_before": "authorized",
            "endpoint_authorization_after": "authorized",
            "old_task_endpoint_retired": True,
            "exclusive_control_verified": False,
        },
        "checks": dict.fromkeys(CHECK_LABELS, True),
    }


def show(path: Path, data: dict):
    path.write_text(json.dumps(data), encoding="utf-8")
    with TestClient(create_app(path)) as client:
        return client.get("/")


def test_dashboard_renders_only_recorded_fields(tmp_path, snapshot):
    snapshot["migration"]["location_sequence_before"] = 7
    snapshot["migration"]["location_sequence_after"] = 9
    response = show(tmp_path / "evidence.json", snapshot)
    html = response.text
    assert response.status_code == 200
    assert "All evidence checks passed" in html
    assert "recorded results from localhost HTTPS instances" in html
    assert "Exclusive control is unverified" in html
    assert "Server A → Server B" in html and "Server B → Server A" in html
    assert "Server A → Server C" in html
    assert "<code>7</code>" in html and "<code>9</code>" in html
    assert all(item["task_id"] in html for item in snapshot["exchanges"])
    assert html.count("Completed and retrieved") == 3
    assert html.count('id="exchanges"') == html.count('id="post-migration"') == 1
    assert "cache-control" in response.headers and response.headers["cache-control"] == "no-store"
    assert "default-src 'none'" in response.headers["content-security-policy"]


def test_incomplete_or_changed_evidence_does_not_claim_success(tmp_path, snapshot):
    snapshot["checks"]["cleanup_completed"] = False
    snapshot["migration"]["reference_id_after"] = identifier(9)
    snapshot["migration"]["endpoint_authorization_after"] = "expired"
    snapshot["exchanges"][2].update(state="failed", retrieved=False)
    response = show(tmp_path / "evidence.json", snapshot)
    assert response.status_code == 200
    assert "Evidence needs review" in response.text
    assert "All evidence checks passed" not in response.text
    assert "State failed · Retrieval unconfirmed" in response.text
    assert "Location update unconfirmed" in response.text
    assert "Changed" in response.text
    assert response.text.count("Completed and retrieved") == 2


@pytest.mark.parametrize(
    "change",
    [
        "unknown_field",
        "script_in_id",
        "missing_check",
        "string_boolean",
        "invalid_version",
        "duplicate_task",
        "wrong_route",
        "exclusive_claim",
    ],
)
def test_rejects_invalid_evidence_without_reflecting_values(tmp_path, snapshot, change):
    hostile = "<script>alert('private-data')</script>"
    if change == "unknown_field":
        snapshot["raw_payload"] = hostile
    elif change == "script_in_id":
        snapshot["exchanges"][0]["task_id"] = hostile
    elif change == "missing_check":
        snapshot["checks"].pop("cleanup_completed")
    elif change == "string_boolean":
        snapshot["checks"]["cleanup_completed"] = "true"
    elif change == "invalid_version":
        snapshot["schema_version"] = True
    elif change == "duplicate_task":
        snapshot["exchanges"][1]["task_id"] = snapshot["exchanges"][0]["task_id"]
    elif change == "wrong_route":
        snapshot["exchanges"][2]["destination"] = "nova-source"
    else:
        snapshot["migration"]["exclusive_control_verified"] = True
    response = show(tmp_path / "evidence.json", snapshot)
    assert response.status_code == 422
    assert "Evidence unavailable" in response.text
    assert "private-data" not in response.text
    assert "All evidence checks passed" not in response.text


@pytest.mark.parametrize("kind", ["malformed", "oversized", "duplicate_field", "fifo", "symlink"])
def test_rejects_unsafe_evidence_files(tmp_path, kind):
    path = tmp_path / "evidence.json"
    if kind == "malformed":
        path.write_bytes(b"\xff")
    elif kind == "oversized":
        path.write_bytes(b" " * (MAX_EVIDENCE_BYTES + 1))
    elif kind == "duplicate_field":
        path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        target = tmp_path / "secret"
        target.write_text("private-data", encoding="utf-8")
        path.symlink_to(target)
    with TestClient(create_app(path)) as client:
        response = client.get("/")
    assert response.status_code == 422
    assert "Evidence unavailable" in response.text
    assert "private-data" not in response.text


def test_reload_is_read_only_and_never_shows_previous_success(tmp_path, snapshot):
    evidence_path = tmp_path / "evidence.json"
    with TestClient(create_app(evidence_path)) as client:
        missing = client.get("/")
        assert missing.status_code == 404
        assert "No demo evidence" in missing.text
        evidence_path.write_text(json.dumps(snapshot), encoding="utf-8")
        assert client.get("/").status_code == 200
        evidence_path.write_text("malformed", encoding="utf-8")
        response = client.get("/")
        assert response.status_code == 422 and "All evidence checks passed" not in response.text
        assert client.post("/", json=snapshot).status_code == 405
        assert client.post("/a2a", json={}).status_code == 404
        assert client.get("/openapi.json").status_code == 404
        assert client.get("/evidence.json").status_code == 404
        assert client.get("/dashboard.css").headers["content-type"].startswith("text/css")
    assert _code('<img src=x onerror="oops">') == (
        "<code>&lt;img src=x onerror=&quot;oops&quot;&gt;</code>"
    )


def test_real_milestone_evidence():
    name = os.environ.get("AGENT_COMMONS_DASHBOARD_EVIDENCE_PATH")
    if not name:
        pytest.skip("Requires an Agent Internet Live evidence artifact")
    with TestClient(create_app(Path(name))) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert "All evidence checks passed" in response.text
    assert response.text.count("Completed and retrieved") == 3
    assert "<code>1</code>" in response.text and "<code>2</code>" in response.text
