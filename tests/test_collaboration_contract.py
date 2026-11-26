import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from agent_commons.collaboration import (
    SkillRegistry,
    registry,
    validate_typed_output,
    validate_typed_request,
)


def test_local_registry_validates_distinct_bounded_contracts():
    local = SkillRegistry()
    skill = registry.get("analysis.summary@1")
    local.register(skill)
    assert local.get(skill.key).input_model.model_json_schema()["type"] == "object"
    for bad in (
        skill,
        replace(skill, key="unsafe@0"),
        replace(skill, key="analysis.summary@2", max_input_bytes=99_000),
    ):
        with pytest.raises(ValueError):
            local.register(bad)
    report = validate_typed_output("report.markdown@1", {"markdown": "# Report"})
    assert report["markdown"] == "# Report"
    with pytest.raises(ValueError):
        validate_typed_output("report.markdown@1", {"markdown": "♥" * 4000})


def test_typed_contract_canonical_digest_and_schema_validation():
    now = datetime(2026, 11, 26, tzinfo=UTC)
    data = {
        "message_id": "fixed-id",
        "skill": "analysis.summary@1",
        "deadline": (now + timedelta(seconds=10)).isoformat(),
        "input": {"values": [1, 2]},
    }
    request, digest = validate_typed_request(json.dumps(data).encode(), now=now)
    data["deadline"] = "2026-11-26T08:00:10+08:00"
    assert validate_typed_request(json.dumps(data).encode(), now=now)[1] == digest
    assert request.skill == "analysis.summary@1"
    for change in (
        {"input": {"values": ["1"]}},
        {"input": {"values": [True]}},
        {"input": {"values": [1], "command": "echo"}},
        {"skill": "analysis.summary@2"},
        {"deadline": now.isoformat()},
        {"message_id": ""},
    ):
        with pytest.raises(ValueError, match="Invalid typed"):
            validate_typed_request(json.dumps({**data, **change}).encode(), now=now)
    with pytest.raises(ValueError):
        validate_typed_request(b'{"input":{},"input":{}}', now=now)


def test_bound_typed_grant_keeps_skills_and_cannot_dispatch_text():
    from uuid import uuid4

    from agent_commons.client import CommonsClient, CommonsError
    from agent_commons.outbound_a2a import BoundGrant, DestinationApproval
    from agent_commons.peer_protocol import SkillIssuedGrant

    root = "sha256:" + "a" * 64
    recipient = uuid4()
    expiry = datetime.now(UTC) + timedelta(minutes=10)
    grant = SkillIssuedGrant(
        version=2,
        skills=["analysis.summary@1"],
        token="a2c_" + "x" * 43,
        expires_at=expiry,
        grant_id=uuid4(),
        audience="https://receiver.example",
        caller_root=root,
        recipient_agent_id=recipient,
        scopes=["message/send"],
    )
    binding = BoundGrant(
        grant=grant,
        destination=DestinationApproval(
            reference_id=uuid4(),
            peer_root=root,
            controller="zcontroller",
            endpoint="https://receiver.example/a2a",
            recipient_agent_id=recipient,
        ),
        home_origin="https://home.example",
        expires_at=expiry,
        identity_sequence=0,
        location_sequence=1,
    )
    raw = binding.model_dump(mode="json")
    assert raw["grant"]["skills"] == grant.skills
    raw["grant"]["token"] = grant.token.get_secret_value()
    loaded = BoundGrant.model_validate(raw)
    assert isinstance(loaded.grant, SkillIssuedGrant)
    with CommonsClient(binding.home_origin) as client:
        with pytest.raises(CommonsError, match="execution is unavailable"):
            client.send_task(loaded, "sample", message_id="legacy")


def test_migration_isolates_typed_issuance_and_preserves_legacy_on_downgrade():
    from importlib import import_module
    from uuid import uuid4

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy.exc import DBAPIError

    from agent_commons.db import engine

    migration = import_module("migrations.versions.0018_collaboration_skill_scopes")
    # Transactional schema on the disposable test engine. Keep existing fixture
    # tables and the Alembic version untouched.
    schema = f"skill_migration_{uuid4().hex}"
    with engine.connect() as db, db.begin() as transaction:
        db.exec_driver_sql(f"CREATE SCHEMA {schema}")
        db.exec_driver_sql(f"SET LOCAL search_path TO {schema}, pg_catalog")
        db.exec_driver_sql("CREATE TABLE peer_approvals (id integer PRIMARY KEY)")
        db.exec_driver_sql(
            "CREATE TABLE peer_grants (id integer PRIMARY KEY, approval_id integer NOT NULL "
            "REFERENCES peer_approvals(id) ON DELETE CASCADE)"
        )
        db.exec_driver_sql("INSERT INTO peer_approvals VALUES (1)")
        db.exec_driver_sql("INSERT INTO peer_grants VALUES (1, 1)")
        with Operations.context(MigrationContext.configure(db)):
            migration.upgrade()
            db.exec_driver_sql("INSERT INTO peer_approvals VALUES (2, '[\"analysis.summary@1\"]')")
            with pytest.raises(DBAPIError, match="Grant skill permissions"):
                with db.begin_nested():
                    db.exec_driver_sql("INSERT INTO peer_grants (id, approval_id) VALUES (2, 2)")
            db.exec_driver_sql("INSERT INTO peer_grants VALUES (2, 2, '[\"analysis.summary@1\"]')")
            migration.downgrade()
        assert db.exec_driver_sql("SELECT id FROM peer_grants").scalars().all() == [1]
        assert db.exec_driver_sql("SELECT id FROM peer_approvals").scalars().all() == [1]
        transaction.rollback()
