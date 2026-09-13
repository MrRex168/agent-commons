import secrets
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from agent_commons.auth import get_current_agent
from agent_commons.config import settings
from agent_commons.db import get_db
from agent_commons.identity_crypto import identity_fingerprint, verify_identity_signature
from agent_commons.models import Agent, AgentCryptographicIdentity
from agent_commons.recovery_models import (
    AgentRecoveryChallenge,
    AgentRecoveryPolicy,
    AgentRecoveryPolicyChallenge,
    AgentRecoveryTransition,
)
from agent_commons.rotation import ensure_key_state
from agent_commons.rotation_models import AgentIdentityKeyState

router = APIRouter(prefix="/agents", tags=["agent-recovery"])
RECOVERY_CHALLENGE_TTL = timedelta(minutes=5)


class RecoveryPolicyChallengeRequest(BaseModel):
    recovery_public_key_multibase: str = Field(min_length=2, max_length=128)


class RecoveryPolicyChallengeResponse(BaseModel):
    challenge_id: uuid.UUID
    root_fingerprint: str
    revision: int
    payload: str
    expires_at: datetime


class RecoveryPolicyCompleteRequest(BaseModel):
    challenge_id: uuid.UUID
    active_signature_multibase: str = Field(min_length=2, max_length=256)
    recovery_signature_multibase: str = Field(min_length=2, max_length=256)


class RecoveryPolicyResponse(BaseModel):
    root_fingerprint: str
    recovery_public_key_multibase: str
    recovery_fingerprint: str
    revision: int
    updated_at: datetime


class RecoveryChallengeRequest(BaseModel):
    new_public_key_multibase: str = Field(min_length=2, max_length=128)


class RecoveryChallengeResponse(BaseModel):
    challenge_id: uuid.UUID
    root_fingerprint: str
    sequence: int
    policy_revision: int
    payload: str
    expires_at: datetime


class RecoveryCompleteRequest(BaseModel):
    challenge_id: uuid.UUID
    recovery_signature_multibase: str = Field(min_length=2, max_length=256)
    new_signature_multibase: str = Field(min_length=2, max_length=256)


class RecoveryResult(BaseModel):
    root_fingerprint: str
    current_public_key_multibase: str
    sequence: int
    policy_revision: int


class RecoveryTransitionResponse(BaseModel):
    sequence: int
    policy_revision: int
    previous_public_key_multibase: str
    new_public_key_multibase: str
    recovery_public_key_multibase: str
    payload: str
    recovery_signature_multibase: str
    new_signature_multibase: str
    created_at: datetime


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _policy_payload(
    root_fingerprint: str,
    current_key: str,
    identity_sequence: int,
    recovery_key: str,
    revision: int,
    nonce: str,
    issued_at: datetime,
    expires_at: datetime,
) -> str:
    audience = settings.api_url.rstrip("/")
    return "\n".join(
        [
            "agent-commons/recovery-policy/v1",
            f"audience:{audience}",
            "operation:set-recovery-policy",
            f"nonce:{nonce}",
            f"identity:{root_fingerprint}",
            f"identity_sequence:{identity_sequence}",
            f"current_key:{current_key}",
            f"policy_revision:{revision}",
            f"recovery_key:{recovery_key}",
            f"issued_at:{_iso(issued_at)}",
            f"expires_at:{_iso(expires_at)}",
        ]
    )


def _recovery_payload(
    root_fingerprint: str,
    recovery_fingerprint: str,
    previous_key: str,
    new_key: str,
    sequence: int,
    policy_revision: int,
    nonce: str,
    issued_at: datetime,
    expires_at: datetime,
) -> str:
    audience = settings.api_url.rstrip("/")
    return "\n".join(
        [
            "agent-commons/key-recovery/v1",
            f"audience:{audience}",
            "operation:recover",
            f"nonce:{nonce}",
            f"identity:{root_fingerprint}",
            f"sequence:{sequence}",
            f"policy_revision:{policy_revision}",
            f"recovery_key:{recovery_fingerprint}",
            f"revoked_key:{previous_key}",
            f"new_key:{new_key}",
            f"issued_at:{_iso(issued_at)}",
            f"expires_at:{_iso(expires_at)}",
        ]
    )


def _identity_and_state(
    agent: Agent,
    db: Session,
) -> tuple[AgentCryptographicIdentity, AgentIdentityKeyState]:
    identity = db.get(AgentCryptographicIdentity, agent.id)
    if identity is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cryptographic identity not bound",
        )
    return identity, ensure_key_state(identity, db)


def _ensure_key_available(public_key_multibase: str, agent_id: uuid.UUID, db: Session) -> None:
    claimed_root = db.scalar(
        select(AgentCryptographicIdentity).where(
            AgentCryptographicIdentity.public_key_multibase == public_key_multibase
        )
    )
    if claimed_root is not None and claimed_root.agent_id != agent_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Key is already a root key for another agent",
        )

    claimed_active = db.scalar(
        select(AgentIdentityKeyState).where(
            AgentIdentityKeyState.current_public_key_multibase == public_key_multibase
        )
    )
    if claimed_active is not None and claimed_active.agent_id != agent_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Key is already active for another agent",
        )


@router.post(
    "/me/identity/recovery-policy/challenge",
    response_model=RecoveryPolicyChallengeResponse,
)
def create_recovery_policy_challenge(
    request: RecoveryPolicyChallengeRequest,
    agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> RecoveryPolicyChallengeResponse:
    _, key_state = _identity_and_state(agent, db)
    try:
        identity_fingerprint(request.recovery_public_key_multibase)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    if request.recovery_public_key_multibase == key_state.current_public_key_multibase:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Recovery key must differ from the current active key",
        )
    _ensure_key_available(request.recovery_public_key_multibase, agent.id, db)

    current = db.get(AgentRecoveryPolicy, agent.id)
    revision = 1 if current is None else current.revision + 1
    issued_at = datetime.now(UTC)
    expires_at = issued_at + RECOVERY_CHALLENGE_TTL
    challenge = AgentRecoveryPolicyChallenge(
        agent_id=agent.id,
        root_fingerprint=key_state.root_fingerprint,
        current_public_key_multibase=key_state.current_public_key_multibase,
        proposed_recovery_public_key_multibase=request.recovery_public_key_multibase,
        identity_sequence=key_state.sequence,
        revision=revision,
        payload=_policy_payload(
            key_state.root_fingerprint,
            key_state.current_public_key_multibase,
            key_state.sequence,
            request.recovery_public_key_multibase,
            revision,
            secrets.token_urlsafe(32),
            issued_at,
            expires_at,
        ),
        issued_at=issued_at,
        expires_at=expires_at,
    )
    db.add(challenge)
    db.commit()
    db.refresh(challenge)
    return RecoveryPolicyChallengeResponse(
        challenge_id=challenge.id,
        root_fingerprint=challenge.root_fingerprint,
        revision=challenge.revision,
        payload=challenge.payload,
        expires_at=challenge.expires_at,
    )


@router.post(
    "/me/identity/recovery-policy/complete",
    response_model=RecoveryPolicyResponse,
)
def complete_recovery_policy(
    request: RecoveryPolicyCompleteRequest,
    agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> RecoveryPolicyResponse:
    _, key_state = _identity_and_state(agent, db)
    challenge = db.get(AgentRecoveryPolicyChallenge, request.challenge_id)
    if challenge is None or challenge.agent_id != agent.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Recovery policy challenge not found",
        )
    if challenge.consumed_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Recovery policy challenge has already been consumed",
        )

    now = datetime.now(UTC)
    if challenge.expires_at <= now:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Recovery policy challenge has expired",
        )

    current = db.get(AgentRecoveryPolicy, agent.id)
    expected_revision = 1 if current is None else current.revision + 1
    if challenge.revision != expected_revision:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Recovery policy challenge is stale",
        )
    if challenge.identity_sequence != key_state.sequence:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Identity sequence changed after policy challenge issuance",
        )
    if challenge.current_public_key_multibase != key_state.current_public_key_multibase:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Active identity key changed after policy challenge issuance",
        )

    try:
        active_valid = verify_identity_signature(
            challenge.current_public_key_multibase,
            challenge.payload,
            request.active_signature_multibase,
        )
        recovery_valid = verify_identity_signature(
            challenge.proposed_recovery_public_key_multibase,
            challenge.payload,
            request.recovery_signature_multibase,
        )
        recovery_fingerprint = identity_fingerprint(
            challenge.proposed_recovery_public_key_multibase
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    if not active_valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid active-key authorization",
        )
    if not recovery_valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid recovery-key possession proof",
        )

    _ensure_key_available(challenge.proposed_recovery_public_key_multibase, agent.id, db)
    if current is None:
        current = AgentRecoveryPolicy(
            agent_id=agent.id,
            recovery_public_key_multibase=challenge.proposed_recovery_public_key_multibase,
            recovery_fingerprint=recovery_fingerprint,
            revision=challenge.revision,
            statement_payload=challenge.payload,
            active_signature_multibase=request.active_signature_multibase,
            recovery_signature_multibase=request.recovery_signature_multibase,
            updated_at=now,
        )
        db.add(current)
    else:
        current.recovery_public_key_multibase = challenge.proposed_recovery_public_key_multibase
        current.recovery_fingerprint = recovery_fingerprint
        current.revision = challenge.revision
        current.statement_payload = challenge.payload
        current.active_signature_multibase = request.active_signature_multibase
        current.recovery_signature_multibase = request.recovery_signature_multibase
        current.updated_at = now

    challenge.consumed_at = now
    db.commit()
    return RecoveryPolicyResponse(
        root_fingerprint=key_state.root_fingerprint,
        recovery_public_key_multibase=current.recovery_public_key_multibase,
        recovery_fingerprint=current.recovery_fingerprint,
        revision=current.revision,
        updated_at=current.updated_at,
    )


@router.get(
    "/me/identity/recovery-policy",
    response_model=RecoveryPolicyResponse,
)
def get_recovery_policy(
    agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> RecoveryPolicyResponse:
    _, key_state = _identity_and_state(agent, db)
    policy = db.get(AgentRecoveryPolicy, agent.id)
    if policy is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Recovery policy not configured",
        )
    return RecoveryPolicyResponse(
        root_fingerprint=key_state.root_fingerprint,
        recovery_public_key_multibase=policy.recovery_public_key_multibase,
        recovery_fingerprint=policy.recovery_fingerprint,
        revision=policy.revision,
        updated_at=policy.updated_at,
    )


@router.post(
    "/me/identity/recovery/challenge",
    response_model=RecoveryChallengeResponse,
)
def create_recovery_challenge(
    request: RecoveryChallengeRequest,
    agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> RecoveryChallengeResponse:
    _, key_state = _identity_and_state(agent, db)
    policy = db.get(AgentRecoveryPolicy, agent.id)
    if policy is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Recovery policy not configured",
        )

    try:
        identity_fingerprint(request.new_public_key_multibase)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    if request.new_public_key_multibase == key_state.current_public_key_multibase:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="New key must differ from current active key",
        )
    if request.new_public_key_multibase == policy.recovery_public_key_multibase:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="New active key must differ from recovery key",
        )
    _ensure_key_available(request.new_public_key_multibase, agent.id, db)

    issued_at = datetime.now(UTC)
    expires_at = issued_at + RECOVERY_CHALLENGE_TTL
    challenge = AgentRecoveryChallenge(
        agent_id=agent.id,
        root_fingerprint=key_state.root_fingerprint,
        recovery_public_key_multibase=policy.recovery_public_key_multibase,
        previous_public_key_multibase=key_state.current_public_key_multibase,
        new_public_key_multibase=request.new_public_key_multibase,
        sequence=key_state.sequence + 1,
        policy_revision=policy.revision,
        payload=_recovery_payload(
            key_state.root_fingerprint,
            policy.recovery_fingerprint,
            key_state.current_public_key_multibase,
            request.new_public_key_multibase,
            key_state.sequence + 1,
            policy.revision,
            secrets.token_urlsafe(32),
            issued_at,
            expires_at,
        ),
        issued_at=issued_at,
        expires_at=expires_at,
    )
    db.add(challenge)
    db.commit()
    db.refresh(challenge)
    return RecoveryChallengeResponse(
        challenge_id=challenge.id,
        root_fingerprint=challenge.root_fingerprint,
        sequence=challenge.sequence,
        policy_revision=challenge.policy_revision,
        payload=challenge.payload,
        expires_at=challenge.expires_at,
    )


@router.post(
    "/me/identity/recovery/complete",
    response_model=RecoveryResult,
)
def complete_recovery(
    request: RecoveryCompleteRequest,
    agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> RecoveryResult:
    _, key_state = _identity_and_state(agent, db)
    policy = db.get(AgentRecoveryPolicy, agent.id)
    if policy is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Recovery policy not configured",
        )

    challenge = db.get(AgentRecoveryChallenge, request.challenge_id)
    if challenge is None or challenge.agent_id != agent.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Recovery challenge not found",
        )
    if challenge.consumed_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Recovery challenge has already been consumed",
        )

    now = datetime.now(UTC)
    if challenge.expires_at <= now:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Recovery challenge has expired",
        )
    if challenge.sequence != key_state.sequence + 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Recovery challenge is stale",
        )
    if challenge.policy_revision != policy.revision:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Recovery policy changed after challenge issuance",
        )
    if challenge.previous_public_key_multibase != key_state.current_public_key_multibase:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Active key changed after recovery challenge issuance",
        )
    if challenge.recovery_public_key_multibase != policy.recovery_public_key_multibase:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Recovery key changed after challenge issuance",
        )

    try:
        recovery_valid = verify_identity_signature(
            policy.recovery_public_key_multibase,
            challenge.payload,
            request.recovery_signature_multibase,
        )
        new_valid = verify_identity_signature(
            challenge.new_public_key_multibase,
            challenge.payload,
            request.new_signature_multibase,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    if not recovery_valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid recovery-key authorization",
        )
    if not new_valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid new-key possession proof",
        )

    _ensure_key_available(challenge.new_public_key_multibase, agent.id, db)
    db.add(
        AgentRecoveryTransition(
            agent_id=agent.id,
            sequence=challenge.sequence,
            policy_revision=policy.revision,
            previous_public_key_multibase=challenge.previous_public_key_multibase,
            new_public_key_multibase=challenge.new_public_key_multibase,
            recovery_public_key_multibase=policy.recovery_public_key_multibase,
            payload=challenge.payload,
            recovery_signature_multibase=request.recovery_signature_multibase,
            new_signature_multibase=request.new_signature_multibase,
            created_at=now,
        )
    )
    key_state.current_public_key_multibase = challenge.new_public_key_multibase
    key_state.sequence = challenge.sequence
    challenge.consumed_at = now
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Recovery transition could not be committed",
        ) from exc

    return RecoveryResult(
        root_fingerprint=key_state.root_fingerprint,
        current_public_key_multibase=key_state.current_public_key_multibase,
        sequence=key_state.sequence,
        policy_revision=policy.revision,
    )


@router.get(
    "/me/identity/recovery/history",
    response_model=list[RecoveryTransitionResponse],
)
def get_recovery_history(
    agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> list[RecoveryTransitionResponse]:
    items = db.scalars(
        select(AgentRecoveryTransition)
        .where(AgentRecoveryTransition.agent_id == agent.id)
        .order_by(AgentRecoveryTransition.sequence)
    ).all()
    return [
        RecoveryTransitionResponse(
            sequence=item.sequence,
            policy_revision=item.policy_revision,
            previous_public_key_multibase=item.previous_public_key_multibase,
            new_public_key_multibase=item.new_public_key_multibase,
            recovery_public_key_multibase=item.recovery_public_key_multibase,
            payload=item.payload,
            recovery_signature_multibase=item.recovery_signature_multibase,
            new_signature_multibase=item.new_signature_multibase,
            created_at=item.created_at,
        )
        for item in items
    ]
