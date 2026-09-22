import uuid

import pytest
from fastapi import HTTPException

from agent_commons.remote_models import RemoteAgentReference
from agent_commons.remote_resolution import _assert_identity_progression


def _reference(*, verified: bool = True) -> RemoteAgentReference:
    return RemoteAgentReference(
        id=uuid.uuid4(),
        card_url="https://remote.example/.well-known/agent-card.json",
        name="Nova",
        description="Remote agent",
        a2a_url="https://remote.example/a2a",
        preferred_transport="JSONRPC",
        root_fingerprint="sha256:root" if verified else None,
        current_controller_public_key="z6MkK2" if verified else None,
        identity_sequence=2 if verified else None,
        identity_verified=verified,
        lineage={"version": 2} if verified else None,
        card_snapshot={"name": "Nova"},
    )


def test_rejects_verified_identity_downgrade() -> None:
    with pytest.raises(HTTPException) as exc:
        _assert_identity_progression(_reference(), None, None, None)
    assert exc.value.status_code == 409
    assert "downgraded" in exc.value.detail


def test_rejects_identity_sequence_rollback() -> None:
    with pytest.raises(HTTPException) as exc:
        _assert_identity_progression(
            _reference(), "sha256:root", "z6MkK1", 1
        )
    assert exc.value.status_code == 409
    assert "rollback" in exc.value.detail


def test_rejects_same_sequence_controller_fork() -> None:
    with pytest.raises(HTTPException) as exc:
        _assert_identity_progression(
            _reference(), "sha256:root", "z6MkFork", 2
        )
    assert exc.value.status_code == 409
    assert "Conflicting" in exc.value.detail


def test_accepts_same_verified_state() -> None:
    _assert_identity_progression(
        _reference(), "sha256:root", "z6MkK2", 2
    )


def test_accepts_newer_verified_controller() -> None:
    _assert_identity_progression(
        _reference(), "sha256:root", "z6MkK3", 3
    )


def test_unverified_reference_can_upgrade_to_verified_identity() -> None:
    _assert_identity_progression(
        _reference(verified=False), "sha256:root", "z6MkK1", 0
    )
