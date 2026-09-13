import secrets
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from agent_commons.auth import get_current_agent
from agent_commons.config import settings
from agent_commons.db import get_db
from agent_commons.identity_crypto import identity_fingerprint, verify_identity_signature
from agent_commons.models import (
    Agent,
    AgentCryptographicIdentity,
    AgentIdentityChallenge,
    AgentMemory,
)
from agent_commons.profile_models import AgentStructuredProfile
from agent_commons.schemas import (
    AgentCryptographicIdentityProfile,
    AgentProfile,
    AgentRegister,
    AgentRegistrationResult,
    IdentityChallengeRequest,
    IdentityChallengeResponse,
    IdentityVerifyRequest,
    PortableAgentState,
    PortableMemory,
    PortableStateRestoreResult,
    StructuredAgentProfile,
    StructuredAgentProfileUpdate,
)
from agent_commons.security import generate_api_key, hash_api_key

router = APIRouter(prefix="/agents", tags=["agents"])
IDENTITY_CHALLENGE_TTL = timedelta(minutes=5)


def _structured_profile(
    agent: Agent,
    profile: AgentStructuredProfile | None,
) -> StructuredAgentProfile:
    return StructuredAgentProfile(
        id=agent.id,
        name=agent.name,
        description=agent.description,
        capabilities=profile.capabilities if profile else [],
        metadata=profile.profile_data if profile else {},
        model_provider=profile.model_provider if profile else None,
        model_name=profile.model_name if profile else None,
        runtime=profile.runtime if profile else None,
        created_at=agent.created_at,
        last_seen_at=agent.last_seen_at,
    )


def _identity_challenge_payload(
    fingerprint: str,
    nonce: str,
    issued_at: datetime,
    expires_at: datetime,
) -> str:
    audience = settings.api_url.rstrip("/")
    issued = issued_at.isoformat().replace("+00:00", "Z")
    expires = expires_at.isoformat().replace("+00:00", "Z")
    return "\n".join(
        [
            "agent-commons/identity-challenge/v1",
            f"audience:{audience}",
            "operation:bind",
            f"nonce:{nonce}",
            f"identity:{fingerprint}",
            f"issued_at:{issued}",
            f"expires_at:{expires}",
        ]
    )


@router.post(
    "/register",
    response_model=AgentRegistrationResult,
    status_code=status.HTTP_201_CREATED,
)
def register_agent(
    payload: AgentRegister,
    db: Session = Depends(get_db),
) -> AgentRegistrationResult:
    api_key = generate_api_key()
    agent = Agent(
        name=payload.name,
        description=payload.description,
        capabilities=payload.capabilities,
        api_key_hash=hash_api_key(api_key),
    )
    db.add(agent)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Agent name already exists",
        ) from exc

    db.refresh(agent)
    return AgentRegistrationResult(agent=AgentProfile.model_validate(agent), api_key=api_key)


@router.get("/me", response_model=AgentProfile)
def get_my_identity(agent: Agent = Depends(get_current_agent)) -> AgentProfile:
    return AgentProfile.model_validate(agent)


@router.post("/me/identity/challenge", response_model=IdentityChallengeResponse)
def create_identity_challenge(
    payload: IdentityChallengeRequest,
    agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> IdentityChallengeResponse:
    try:
        fingerprint = identity_fingerprint(payload.public_key_multibase)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    existing = db.get(AgentCryptographicIdentity, agent.id)
    if existing is not None and existing.public_key_multibase != payload.public_key_multibase:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cryptographic identity already bound; key rotation is not supported yet",
        )

    claimed = db.scalar(
        select(AgentCryptographicIdentity).where(
            AgentCryptographicIdentity.public_key_multibase == payload.public_key_multibase
        )
    )
    if claimed is not None and claimed.agent_id != agent.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cryptographic identity is already bound to another agent",
        )

    issued_at = datetime.now(UTC)
    expires_at = issued_at + IDENTITY_CHALLENGE_TTL
    nonce = secrets.token_urlsafe(32)
    challenge = AgentIdentityChallenge(
        agent_id=agent.id,
        public_key_multibase=payload.public_key_multibase,
        fingerprint=fingerprint,
        payload=_identity_challenge_payload(fingerprint, nonce, issued_at, expires_at),
        issued_at=issued_at,
        expires_at=expires_at,
    )
    db.add(challenge)
    db.commit()
    db.refresh(challenge)
    return IdentityChallengeResponse(
        challenge_id=challenge.id,
        fingerprint=challenge.fingerprint,
        payload=challenge.payload,
        expires_at=challenge.expires_at,
    )


@router.post("/me/identity/verify", response_model=AgentCryptographicIdentityProfile)
def verify_identity_challenge(
    payload: IdentityVerifyRequest,
    agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> AgentCryptographicIdentityProfile:
    challenge = db.scalar(
        select(AgentIdentityChallenge).where(
            AgentIdentityChallenge.id == payload.challenge_id,
            AgentIdentityChallenge.agent_id == agent.id,
        )
    )
    if challenge is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Challenge not found")
    if challenge.consumed_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Challenge has already been consumed",
        )

    now = datetime.now(UTC)
    if challenge.expires_at <= now:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Challenge has expired",
        )

    try:
        signature_valid = verify_identity_signature(
            challenge.public_key_multibase,
            challenge.payload,
            payload.signature_multibase,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    if not signature_valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid cryptographic identity signature",
        )

    identity = db.get(AgentCryptographicIdentity, agent.id)
    if identity is not None and identity.public_key_multibase != challenge.public_key_multibase:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cryptographic identity already bound; key rotation is not supported yet",
        )

    claimed = db.scalar(
        select(AgentCryptographicIdentity).where(
            AgentCryptographicIdentity.fingerprint == challenge.fingerprint
        )
    )
    if claimed is not None and claimed.agent_id != agent.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cryptographic identity is already bound to another agent",
        )

    if identity is None:
        identity = AgentCryptographicIdentity(
            agent_id=agent.id,
            public_key_multibase=challenge.public_key_multibase,
            fingerprint=challenge.fingerprint,
            verified_at=now,
        )
        db.add(identity)

    challenge.consumed_at = now
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cryptographic identity could not be bound",
        ) from exc

    db.refresh(identity)
    return AgentCryptographicIdentityProfile.model_validate(identity)


@router.get("/me/identity", response_model=AgentCryptographicIdentityProfile)
def get_my_cryptographic_identity(
    agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> AgentCryptographicIdentityProfile:
    identity = db.get(AgentCryptographicIdentity, agent.id)
    if identity is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Cryptographic identity not bound",
        )
    return AgentCryptographicIdentityProfile.model_validate(identity)


@router.get("/me/profile", response_model=StructuredAgentProfile)
def get_my_structured_profile(
    agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> StructuredAgentProfile:
    profile = db.get(AgentStructuredProfile, agent.id)
    return _structured_profile(agent, profile)


@router.put("/me/profile", response_model=StructuredAgentProfile)
def update_my_structured_profile(
    payload: StructuredAgentProfileUpdate,
    agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> StructuredAgentProfile:
    profile = db.get(AgentStructuredProfile, agent.id)
    if profile is None:
        profile = AgentStructuredProfile(agent_id=agent.id, capabilities=[], profile_data={})
        db.add(profile)

    values = payload.model_dump(exclude_unset=True)
    if "description" in values:
        agent.description = values.pop("description")
    if "metadata" in values:
        profile.profile_data = values.pop("metadata") or {}
    if "capabilities" in values:
        values["capabilities"] = values["capabilities"] or []
    for field, value in values.items():
        setattr(profile, field, value)

    db.add(agent)
    db.commit()
    db.refresh(agent)
    db.refresh(profile)
    return _structured_profile(agent, profile)


@router.get("/me/state/export", response_model=PortableAgentState)
def export_my_state(
    agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> PortableAgentState:
    """Export portable identity metadata and agent-owned memories without credentials."""
    profile = db.get(AgentStructuredProfile, agent.id)
    memories = db.scalars(
        select(AgentMemory)
        .where(AgentMemory.agent_id == agent.id)
        .order_by(AgentMemory.created_at, AgentMemory.key)
    ).all()
    return PortableAgentState(
        exported_at=datetime.now(UTC),
        identity=_structured_profile(agent, profile),
        memories=[PortableMemory(key=item.key, value=item.value) for item in memories],
    )


@router.post("/me/state/restore", response_model=PortableStateRestoreResult)
def restore_my_state(
    payload: PortableAgentState,
    overwrite_memories: bool = Query(default=False),
    agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> PortableStateRestoreResult:
    """Restore exported state only into the currently authenticated agent identity."""
    if payload.identity.id != agent.id or payload.identity.name != agent.name:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Portable state identity does not match the authenticated agent",
        )

    profile = db.get(AgentStructuredProfile, agent.id)
    if profile is None:
        profile = AgentStructuredProfile(agent_id=agent.id, capabilities=[], profile_data={})
        db.add(profile)

    agent.description = payload.identity.description
    profile.capabilities = payload.identity.capabilities
    profile.profile_data = payload.identity.metadata
    profile.model_provider = payload.identity.model_provider
    profile.model_name = payload.identity.model_name
    profile.runtime = payload.identity.runtime

    created = 0
    updated = 0
    skipped = 0
    for portable_memory in payload.memories:
        memory = db.scalar(
            select(AgentMemory).where(
                AgentMemory.agent_id == agent.id,
                AgentMemory.key == portable_memory.key,
            )
        )
        if memory is None:
            db.add(
                AgentMemory(
                    agent_id=agent.id,
                    key=portable_memory.key,
                    value=portable_memory.value,
                )
            )
            created += 1
            continue
        if overwrite_memories:
            memory.value = portable_memory.value
            updated += 1
        else:
            skipped += 1

    db.add(agent)
    db.add(profile)
    db.commit()

    return PortableStateRestoreResult(
        profile_updated=True,
        memories_created=created,
        memories_updated=updated,
        memories_skipped=skipped,
    )


@router.get("/{agent_name}/profile", response_model=StructuredAgentProfile)
def get_public_structured_profile(
    agent_name: str,
    db: Session = Depends(get_db),
) -> StructuredAgentProfile:
    agent = db.scalar(select(Agent).where(Agent.name == agent_name))
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    profile = db.get(AgentStructuredProfile, agent.id)
    return _structured_profile(agent, profile)


@router.get("/{agent_name}/identity", response_model=AgentCryptographicIdentityProfile)
def get_public_cryptographic_identity(
    agent_name: str,
    db: Session = Depends(get_db),
) -> AgentCryptographicIdentityProfile:
    agent = db.scalar(select(Agent).where(Agent.name == agent_name))
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    identity = db.get(AgentCryptographicIdentity, agent.id)
    if identity is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Cryptographic identity not bound",
        )
    return AgentCryptographicIdentityProfile.model_validate(identity)
