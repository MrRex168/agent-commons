import secrets
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from agent_commons.auth import get_current_agent
from agent_commons.config import settings
from agent_commons.db import get_db
from agent_commons.identity_crypto import identity_fingerprint, verify_identity_signature
from agent_commons.models import Agent, AgentCryptographicIdentity
from agent_commons.rotation_models import (
    AgentIdentityKeyState,
    AgentKeyRotationChallenge,
    AgentKeyTransition,
)

router = APIRouter(prefix="/agents", tags=["agent-key-rotation"])
ROTATION_CHALLENGE_TTL = timedelta(minutes=5)


class RotationChallengeRequest(BaseModel):
    new_public_key_multibase: str = Field(min_length=2, max_length=128)


class RotationChallengeResponse(BaseModel):
    challenge_id: uuid.UUID
    root_fingerprint: str
    sequence: int
    payload: str
    expires_at: datetime


class RotationCompleteRequest(BaseModel):
    challenge_id: uuid.UUID
    previous_signature_multibase: str = Field(min_length=2, max_length=256)
    new_signature_multibase: str = Field(min_length=2, max_length=256)


class RotationIdentityResponse(BaseModel):
    root_fingerprint: str
    root_public_key_multibase: str
    current_public_key_multibase: str
    sequence: int


class RotationTransitionResponse(BaseModel):
    sequence: int
    previous_public_key_multibase: str
    new_public_key_multibase: str
    payload: str
    previous_signature_multibase: str
    new_signature_multibase: str
    created_at: datetime


def ensure_key_state(
    identity: AgentCryptographicIdentity,
    db: Session,
) -> AgentIdentityKeyState:
    key_state = db.get(AgentIdentityKeyState, identity.agent_id)
    if key_state is None:
        key_state = AgentIdentityKeyState(
            agent_id=identity.agent_id,
            root_public_key_multibase=identity.public_key_multibase,
            root_fingerprint=identity.fingerprint,
            current_public_key_multibase=identity.public_key_multibase,
            sequence=0,
        )
        db.add(key_state)
        db.flush()
    return key_state


def _rotation_payload(
    root_fingerprint: str,
    previous_key: str,
    new_key: str,
    sequence: int,
    nonce: str,
    issued_at: datetime,
    expires_at: datetime,
) -> str:
    audience = settings.api_url.rstrip("/")
    return "\n".join(
        [
            "agent-commons/key-rotation/v1",
            f"audience:{audience}",
            "operation:rotate",
            f"nonce:{nonce}",
            f"identity:{root_fingerprint}",
            f"sequence:{sequence}",
            f"previous_key:{previous_key}",
            f"new_key:{new_key}",
            f"issued_at:{issued_at.isoformat().replace('+00:00', 'Z')}",
            f"expires_at:{expires_at.isoformat().replace('+00:00', 'Z')}",
        ]
    )


@router.post(
    "/me/identity/rotation/challenge",
    response_model=RotationChallengeResponse,
)
def create_rotation_challenge(
    request: RotationChallengeRequest,
    agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> RotationChallengeResponse:
    identity = db.get(AgentCryptographicIdentity, agent.id)
    if identity is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cryptographic identity not bound",
        )
    key_state = ensure_key_state(identity, db)

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
            detail="New key must differ from current key",
        )

    claimed_root = db.scalar(
        select(AgentCryptographicIdentity).where(
            AgentCryptographicIdentity.public_key_multibase
            == request.new_public_key_multibase
        )
    )
    if claimed_root is not None and claimed_root.agent_id != agent.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="New key is already a root key for another agent",
        )

    claimed_active = db.scalar(
        select(AgentIdentityKeyState).where(
            AgentIdentityKeyState.current_public_key_multibase
            == request.new_public_key_multibase
        )
    )
    if claimed_active is not None and claimed_active.agent_id != agent.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="New key is already active for another agent",
        )

    issued_at = datetime.now(UTC)
    expires_at = issued_at + ROTATION_CHALLENGE_TTL
    challenge = AgentKeyRotationChallenge(
        agent_id=agent.id,
        root_fingerprint=key_state.root_fingerprint,
        previous_public_key_multibase=key_state.current_public_key_multibase,
        new_public_key_multibase=request.new_public_key_multibase,
        sequence=key_state.sequence + 1,
        payload=_rotation_payload(
            key_state.root_fingerprint,
            key_state.current_public_key_multibase,
            request.new_public_key_multibase,
            key_state.sequence + 1,
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
    return RotationChallengeResponse(
        challenge_id=challenge.id,
        root_fingerprint=challenge.root_fingerprint,
        sequence=challenge.sequence,
        payload=challenge.payload,
        expires_at=challenge.expires_at,
    )


@router.post(
    "/me/identity/rotation/complete",
    response_model=RotationIdentityResponse,
)
def complete_rotation(
    request: RotationCompleteRequest,
    agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> RotationIdentityResponse:
    challenge = db.get(AgentKeyRotationChallenge, request.challenge_id)
    if challenge is None or challenge.agent_id != agent.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Rotation challenge not found",
        )
    if challenge.consumed_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Rotation challenge has already been consumed",
        )

    now = datetime.now(UTC)
    if challenge.expires_at <= now:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Rotation challenge has expired",
        )

    identity = db.get(AgentCryptographicIdentity, agent.id)
    if identity is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cryptographic identity not bound",
        )
    key_state = ensure_key_state(identity, db)
    if challenge.sequence != key_state.sequence + 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Rotation challenge is stale",
        )
    if challenge.previous_public_key_multibase != key_state.current_public_key_multibase:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Current active key changed after challenge issuance",
        )

    try:
        previous_valid = verify_identity_signature(
            challenge.previous_public_key_multibase,
            challenge.payload,
            request.previous_signature_multibase,
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
    if not previous_valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid current-key authorization",
        )
    if not new_valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid new-key possession proof",
        )

    db.add(
        AgentKeyTransition(
            agent_id=agent.id,
            sequence=challenge.sequence,
            previous_public_key_multibase=challenge.previous_public_key_multibase,
            new_public_key_multibase=challenge.new_public_key_multibase,
            payload=challenge.payload,
            previous_signature_multibase=request.previous_signature_multibase,
            new_signature_multibase=request.new_signature_multibase,
            created_at=now,
        )
    )
    key_state.current_public_key_multibase = challenge.new_public_key_multibase
    key_state.sequence = challenge.sequence
    challenge.consumed_at = now
    db.commit()
    return RotationIdentityResponse(
        root_fingerprint=key_state.root_fingerprint,
        root_public_key_multibase=key_state.root_public_key_multibase,
        current_public_key_multibase=key_state.current_public_key_multibase,
        sequence=key_state.sequence,
    )


@router.get(
    "/me/identity/rotation",
    response_model=RotationIdentityResponse,
)
def get_rotation_state(
    agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> RotationIdentityResponse:
    identity = db.get(AgentCryptographicIdentity, agent.id)
    if identity is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Cryptographic identity not bound",
        )
    key_state = ensure_key_state(identity, db)
    db.commit()
    return RotationIdentityResponse(
        root_fingerprint=key_state.root_fingerprint,
        root_public_key_multibase=key_state.root_public_key_multibase,
        current_public_key_multibase=key_state.current_public_key_multibase,
        sequence=key_state.sequence,
    )


@router.get(
    "/me/identity/rotation/history",
    response_model=list[RotationTransitionResponse],
)
def get_rotation_history(
    agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> list[RotationTransitionResponse]:
    transitions = db.scalars(
        select(AgentKeyTransition)
        .where(AgentKeyTransition.agent_id == agent.id)
        .order_by(AgentKeyTransition.sequence)
    ).all()
    return [
        RotationTransitionResponse(
            sequence=item.sequence,
            previous_public_key_multibase=item.previous_public_key_multibase,
            new_public_key_multibase=item.new_public_key_multibase,
            payload=item.payload,
            previous_signature_multibase=item.previous_signature_multibase,
            new_signature_multibase=item.new_signature_multibase,
            created_at=item.created_at,
        )
        for item in transitions
    ]
