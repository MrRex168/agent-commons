import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from agent_commons.config import settings
from agent_commons.db import get_db
from agent_commons.freshness_models import AgentStateSequence
from agent_commons.identity_crypto import verify_identity_signature
from agent_commons.lineage import PortableIdentityLineage, verify_lineage
from agent_commons.migration import (
    _enforce_freshness,
    _ensure_destination_available,
    _validate_envelope,
)
from agent_commons.migration_models import AgentMigrationChallenge
from agent_commons.models import Agent, AgentCryptographicIdentity, AgentMemory
from agent_commons.profile_models import AgentStructuredProfile
from agent_commons.rotation_models import AgentIdentityKeyState
from agent_commons.schemas import (
    AgentCryptographicIdentityProfile,
    AgentProfile,
    MigrationChallengeResponse,
    MigrationResult,
    SignedPortableStateEnvelope,
)
from agent_commons.security import generate_api_key, hash_api_key

router = APIRouter(prefix="/agents", tags=["agent-migration"])
CHALLENGE_TTL = timedelta(minutes=5)


class LineageMigrationChallengeRequest(BaseModel):
    envelope: SignedPortableStateEnvelope
    lineage: PortableIdentityLineage
    requested_name: str | None = Field(
        default=None,
        min_length=3,
        max_length=80,
        pattern=r"^[a-zA-Z0-9_-]+$",
    )


class LineageMigrationCompleteRequest(BaseModel):
    challenge_id: str
    envelope: SignedPortableStateEnvelope
    lineage: PortableIdentityLineage
    signature_multibase: str = Field(min_length=2, max_length=256)


def _digest(envelope: SignedPortableStateEnvelope, lineage: PortableIdentityLineage) -> str:
    value = json.dumps(
        {
            "envelope": envelope.model_dump(mode="json"),
            "lineage": lineage.model_dump(mode="json"),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(value.encode()).hexdigest()


def _payload(
    root: str,
    current_key: str,
    package_digest: str,
    name: str,
    issued_at: datetime,
    expires_at: datetime,
) -> str:
    return "\n".join(
        [
            "agent-commons/lineage-migration-challenge/v1",
            f"audience:{settings.api_url.rstrip('/')}",
            "operation:migrate-lineage",
            f"nonce:{secrets.token_urlsafe(32)}",
            f"identity:{root}",
            f"current_key:{current_key}",
            f"package_digest:{package_digest}",
            f"requested_name:{name}",
            f"issued_at:{issued_at.isoformat().replace('+00:00', 'Z')}",
            f"expires_at:{expires_at.isoformat().replace('+00:00', 'Z')}",
        ]
    )


def _verify_package(envelope: SignedPortableStateEnvelope, lineage: PortableIdentityLineage):
    state, state_sequence = _validate_envelope(envelope)
    try:
        verified = verify_lineage(lineage)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if envelope.fingerprint != lineage.root_fingerprint:
        raise HTTPException(status_code=409, detail="State identity does not match lineage")
    if envelope.public_key_multibase != lineage.root_public_key_multibase:
        raise HTTPException(status_code=409, detail="State root key does not match lineage")
    return state, state_sequence, verified


@router.post("/migrate/lineage/challenge", response_model=MigrationChallengeResponse)
def create_lineage_migration_challenge(
    request: LineageMigrationChallengeRequest,
    db: Session = Depends(get_db),
) -> MigrationChallengeResponse:
    state, state_sequence, verified = _verify_package(request.envelope, request.lineage)
    _enforce_freshness(verified.root_fingerprint, state_sequence, db)
    name = request.requested_name or state.identity.name
    _ensure_destination_available(verified.root_fingerprint, name, db)

    issued_at = datetime.now(UTC)
    expires_at = issued_at + CHALLENGE_TTL
    package_digest = _digest(request.envelope, request.lineage)
    challenge = AgentMigrationChallenge(
        public_key_multibase=verified.current_public_key_multibase,
        fingerprint=verified.root_fingerprint,
        requested_name=name,
        envelope_digest=package_digest,
        payload=_payload(
            verified.root_fingerprint,
            verified.current_public_key_multibase,
            package_digest,
            name,
            issued_at,
            expires_at,
        ),
        issued_at=issued_at,
        expires_at=expires_at,
    )
    db.add(challenge)
    db.commit()
    db.refresh(challenge)
    return MigrationChallengeResponse(
        challenge_id=challenge.id,
        fingerprint=challenge.fingerprint,
        requested_name=challenge.requested_name,
        payload=challenge.payload,
        expires_at=challenge.expires_at,
    )


@router.post("/migrate/lineage/complete", response_model=MigrationResult)
def complete_lineage_migration(
    request: LineageMigrationCompleteRequest,
    db: Session = Depends(get_db),
) -> MigrationResult:
    challenge = db.get(AgentMigrationChallenge, request.challenge_id)
    if challenge is None:
        raise HTTPException(status_code=404, detail="Migration challenge not found")
    if challenge.consumed_at is not None:
        raise HTTPException(status_code=409, detail="Migration challenge already consumed")
    now = datetime.now(UTC)
    if challenge.expires_at <= now:
        raise HTTPException(status_code=410, detail="Migration challenge expired")

    state, state_sequence, verified = _verify_package(request.envelope, request.lineage)
    if verified.root_fingerprint != challenge.fingerprint:
        raise HTTPException(status_code=409, detail="Migration identity mismatch")
    if verified.current_public_key_multibase != challenge.public_key_multibase:
        raise HTTPException(status_code=409, detail="Migration current key mismatch")
    if _digest(request.envelope, request.lineage) != challenge.envelope_digest:
        raise HTTPException(status_code=409, detail="Migration package mismatch")

    try:
        valid = verify_identity_signature(
            verified.current_public_key_multibase,
            challenge.payload,
            request.signature_multibase,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not valid:
        raise HTTPException(status_code=401, detail="Invalid current-controller proof")

    _enforce_freshness(verified.root_fingerprint, state_sequence, db)
    _ensure_destination_available(verified.root_fingerprint, challenge.requested_name, db)
    api_key = generate_api_key()
    agent = Agent(
        name=challenge.requested_name,
        description=state.identity.description,
        capabilities=None,
        api_key_hash=hash_api_key(api_key),
    )
    db.add(agent)
    try:
        db.flush()
        identity = AgentCryptographicIdentity(
            agent_id=agent.id,
            public_key_multibase=request.lineage.root_public_key_multibase,
            fingerprint=request.lineage.root_fingerprint,
            verified_at=now,
        )
        db.add(identity)
        db.add(
            AgentIdentityKeyState(
                agent_id=agent.id,
                root_public_key_multibase=request.lineage.root_public_key_multibase,
                root_fingerprint=request.lineage.root_fingerprint,
                current_public_key_multibase=request.lineage.current_public_key_multibase,
                sequence=request.lineage.sequence,
            )
        )
        db.add(
            AgentStructuredProfile(
                agent_id=agent.id,
                capabilities=state.identity.capabilities,
                profile_data=state.identity.metadata,
                model_provider=state.identity.model_provider,
                model_name=state.identity.model_name,
                runtime=state.identity.runtime,
            )
        )
        if state_sequence is not None:
            db.add(AgentStateSequence(agent_id=agent.id, sequence=state_sequence))
        for item in state.memories:
            db.add(AgentMemory(agent_id=agent.id, key=item.key, value=item.value))
        challenge.consumed_at = now
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Destination state changed") from exc

    db.refresh(agent)
    db.refresh(identity)
    return MigrationResult(
        agent=AgentProfile.model_validate(agent),
        identity=AgentCryptographicIdentityProfile.model_validate(identity),
        api_key=api_key,
        memories_restored=len(state.memories),
        source_name=state.identity.name,
        state_sequence=state_sequence,
    )
