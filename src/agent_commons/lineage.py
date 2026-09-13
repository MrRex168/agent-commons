from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from agent_commons.auth import get_current_agent
from agent_commons.db import get_db
from agent_commons.identity_crypto import identity_fingerprint, verify_identity_signature
from agent_commons.models import Agent, AgentCryptographicIdentity
from agent_commons.recovery_models import AgentRecoveryPolicy, AgentRecoveryTransition
from agent_commons.rotation import ensure_key_state
from agent_commons.rotation_models import AgentKeyTransition

router = APIRouter(prefix="/agents", tags=["identity-lineage"])


class RecoveryPolicyEvidence(BaseModel):
    revision: int = Field(ge=1)
    identity_sequence: int = Field(ge=0)
    current_public_key_multibase: str
    recovery_public_key_multibase: str
    payload: str
    active_signature_multibase: str
    recovery_signature_multibase: str


class LineageTransition(BaseModel):
    type: Literal["rotation", "recovery"]
    sequence: int = Field(ge=1)
    previous_public_key_multibase: str
    new_public_key_multibase: str
    payload: str
    previous_signature_multibase: str | None = None
    recovery_public_key_multibase: str | None = None
    recovery_signature_multibase: str | None = None
    new_signature_multibase: str
    policy_revision: int | None = None


class PortableIdentityLineage(BaseModel):
    format: Literal["agent-commons-identity-lineage"] = "agent-commons-identity-lineage"
    version: Literal[1] = 1
    root_fingerprint: str
    root_public_key_multibase: str
    current_public_key_multibase: str
    sequence: int = Field(ge=0)
    recovery_policy: RecoveryPolicyEvidence | None = None
    transitions: list[LineageTransition]


class IdentityLineageVerification(BaseModel):
    valid: bool
    root_fingerprint: str
    current_public_key_multibase: str
    sequence: int
    recovery_policy_revision: int | None


def _field(payload: str, name: str) -> str:
    prefix = f"{name}:"
    for line in payload.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :]
    raise ValueError(f"Lineage payload is missing {name}")


def _int_field(payload: str, name: str) -> int:
    try:
        return int(_field(payload, name))
    except ValueError as exc:
        raise ValueError(f"Lineage payload has invalid {name}") from exc


def _verify_policy(
    lineage: PortableIdentityLineage,
    policy: RecoveryPolicyEvidence,
) -> None:
    if _field(policy.payload, "identity") != lineage.root_fingerprint:
        raise ValueError("Recovery policy root identity mismatch")
    if _int_field(policy.payload, "identity_sequence") != policy.identity_sequence:
        raise ValueError("Recovery policy identity sequence mismatch")
    if _int_field(policy.payload, "policy_revision") != policy.revision:
        raise ValueError("Recovery policy revision mismatch")
    if _field(policy.payload, "current_key") != policy.current_public_key_multibase:
        raise ValueError("Recovery policy active key mismatch")
    if _field(policy.payload, "recovery_key") != policy.recovery_public_key_multibase:
        raise ValueError("Recovery policy recovery key mismatch")
    if not verify_identity_signature(
        policy.current_public_key_multibase,
        policy.payload,
        policy.active_signature_multibase,
    ):
        raise ValueError("Invalid recovery policy active-key signature")
    if not verify_identity_signature(
        policy.recovery_public_key_multibase,
        policy.payload,
        policy.recovery_signature_multibase,
    ):
        raise ValueError("Invalid recovery policy possession proof")


def verify_lineage(lineage: PortableIdentityLineage) -> IdentityLineageVerification:
    if identity_fingerprint(lineage.root_public_key_multibase) != lineage.root_fingerprint:
        raise ValueError("Root fingerprint does not match root public key")

    policy = lineage.recovery_policy
    if policy is not None:
        _verify_policy(lineage, policy)

    current_key = lineage.root_public_key_multibase
    current_sequence = 0
    for transition in sorted(lineage.transitions, key=lambda item: item.sequence):
        if transition.sequence != current_sequence + 1:
            raise ValueError("Identity transition sequence is not contiguous")
        if transition.previous_public_key_multibase != current_key:
            raise ValueError("Identity transition previous key mismatch")
        if _field(transition.payload, "identity") != lineage.root_fingerprint:
            raise ValueError("Identity transition root mismatch")
        if _int_field(transition.payload, "sequence") != transition.sequence:
            raise ValueError("Identity transition sequence mismatch")
        if _field(transition.payload, "new_key") != transition.new_public_key_multibase:
            raise ValueError("Identity transition new key mismatch")

        if transition.type == "rotation":
            if not transition.previous_signature_multibase:
                raise ValueError("Rotation transition missing previous-key signature")
            if _field(transition.payload, "previous_key") != current_key:
                raise ValueError("Rotation transition previous key payload mismatch")
            if not verify_identity_signature(
                current_key,
                transition.payload,
                transition.previous_signature_multibase,
            ):
                raise ValueError("Invalid rotation authorization")
        else:
            if policy is None or transition.policy_revision != policy.revision:
                raise ValueError("Recovery transition lacks portable policy evidence")
            if policy.identity_sequence > current_sequence:
                raise ValueError("Recovery policy was established after transition")
            if transition.recovery_public_key_multibase != policy.recovery_public_key_multibase:
                raise ValueError("Recovery transition uses wrong recovery key")
            if not transition.recovery_signature_multibase:
                raise ValueError("Recovery transition missing recovery signature")
            if _field(transition.payload, "revoked_key") != current_key:
                raise ValueError("Recovery transition revoked key mismatch")
            if _int_field(transition.payload, "policy_revision") != policy.revision:
                raise ValueError("Recovery transition policy revision mismatch")
            if not verify_identity_signature(
                policy.recovery_public_key_multibase,
                transition.payload,
                transition.recovery_signature_multibase,
            ):
                raise ValueError("Invalid recovery authorization")

        if not verify_identity_signature(
            transition.new_public_key_multibase,
            transition.payload,
            transition.new_signature_multibase,
        ):
            raise ValueError("Invalid new-key possession proof")
        current_key = transition.new_public_key_multibase
        current_sequence = transition.sequence

    if current_sequence != lineage.sequence:
        raise ValueError("Lineage sequence does not match verified history")
    if current_key != lineage.current_public_key_multibase:
        raise ValueError("Current public key does not match verified history")

    return IdentityLineageVerification(
        valid=True,
        root_fingerprint=lineage.root_fingerprint,
        current_public_key_multibase=current_key,
        sequence=current_sequence,
        recovery_policy_revision=policy.revision if policy else None,
    )


@router.get("/me/identity/lineage", response_model=PortableIdentityLineage)
def export_identity_lineage(
    agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> PortableIdentityLineage:
    identity = db.get(AgentCryptographicIdentity, agent.id)
    if identity is None:
        raise HTTPException(status_code=404, detail="Cryptographic identity not bound")
    key_state = ensure_key_state(identity, db)
    policy = db.get(AgentRecoveryPolicy, agent.id)
    rotations = db.scalars(
        select(AgentKeyTransition).where(AgentKeyTransition.agent_id == agent.id)
    ).all()
    recoveries = db.scalars(
        select(AgentRecoveryTransition).where(AgentRecoveryTransition.agent_id == agent.id)
    ).all()

    if recoveries and policy is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Recovery history cannot be exported without recovery policy evidence",
        )
    if policy is not None and any(item.policy_revision != policy.revision for item in recoveries):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Historical recovery policy evidence is not retained by lineage v1",
        )

    transitions = [
        LineageTransition(
            type="rotation",
            sequence=item.sequence,
            previous_public_key_multibase=item.previous_public_key_multibase,
            new_public_key_multibase=item.new_public_key_multibase,
            payload=item.payload,
            previous_signature_multibase=item.previous_signature_multibase,
            new_signature_multibase=item.new_signature_multibase,
        )
        for item in rotations
    ] + [
        LineageTransition(
            type="recovery",
            sequence=item.sequence,
            previous_public_key_multibase=item.previous_public_key_multibase,
            new_public_key_multibase=item.new_public_key_multibase,
            payload=item.payload,
            recovery_public_key_multibase=item.recovery_public_key_multibase,
            recovery_signature_multibase=item.recovery_signature_multibase,
            new_signature_multibase=item.new_signature_multibase,
            policy_revision=item.policy_revision,
        )
        for item in recoveries
    ]
    transitions.sort(key=lambda item: item.sequence)

    policy_evidence = None
    if policy is not None:
        policy_evidence = RecoveryPolicyEvidence(
            revision=policy.revision,
            identity_sequence=_int_field(policy.statement_payload, "identity_sequence"),
            current_public_key_multibase=_field(policy.statement_payload, "current_key"),
            recovery_public_key_multibase=policy.recovery_public_key_multibase,
            payload=policy.statement_payload,
            active_signature_multibase=policy.active_signature_multibase,
            recovery_signature_multibase=policy.recovery_signature_multibase,
        )

    result = PortableIdentityLineage(
        root_fingerprint=key_state.root_fingerprint,
        root_public_key_multibase=key_state.root_public_key_multibase,
        current_public_key_multibase=key_state.current_public_key_multibase,
        sequence=key_state.sequence,
        recovery_policy=policy_evidence,
        transitions=transitions,
    )
    db.commit()
    return result


@router.post("/identity/lineage/verify", response_model=IdentityLineageVerification)
def verify_identity_lineage(
    lineage: PortableIdentityLineage,
) -> IdentityLineageVerification:
    try:
        return verify_lineage(lineage)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
