import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from agent_commons.config import settings
from agent_commons.db import get_db
from agent_commons.freshness_models import AgentStateSequence, ObservedStateFreshness
from agent_commons.identity_crypto import identity_fingerprint, verify_identity_signature
from agent_commons.migration_models import AgentMigrationChallenge
from agent_commons.models import Agent, AgentCryptographicIdentity, AgentMemory
from agent_commons.profile_models import AgentStructuredProfile
from agent_commons.schemas import (
    AgentCryptographicIdentityProfile,
    AgentProfile,
    MigrationChallengeRequest,
    MigrationChallengeResponse,
    MigrationCompleteRequest,
    MigrationResult,
    PortableAgentState,
    SignedPortableStateEnvelope,
)
from agent_commons.security import generate_api_key, hash_api_key
from agent_commons.state_serialization import parse_state_payload_details

router = APIRouter(prefix="/agents", tags=["agent-migration"])
MIGRATION_CHALLENGE_TTL = timedelta(minutes=5)


def _envelope_digest(envelope: SignedPortableStateEnvelope) -> str:
    canonical = json.dumps(
        envelope.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_envelope(
    envelope: SignedPortableStateEnvelope,
) -> tuple[PortableAgentState, int | None]:
    try:
        expected_fingerprint = identity_fingerprint(envelope.public_key_multibase)
        parsed = parse_state_payload_details(envelope.payload)
        valid = verify_identity_signature(
            envelope.public_key_multibase,
            envelope.payload,
            envelope.signature_multibase,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    if envelope.fingerprint != expected_fingerprint:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Envelope fingerprint does not match its public key",
        )
    if parsed.fingerprint != envelope.fingerprint:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Payload fingerprint does not match the envelope",
        )
    if parsed.version == 2:
        if envelope.version != 2 or envelope.state_sequence != parsed.state_sequence:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Envelope freshness metadata does not match the signed payload",
            )
    elif envelope.version != 1 or envelope.state_sequence is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Legacy envelope metadata does not match the signed payload",
        )
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid signed state signature",
        )
    return parsed.state, parsed.state_sequence


def _enforce_freshness(
    fingerprint: str,
    state_sequence: int | None,
    db: Session,
) -> None:
    observed = db.get(ObservedStateFreshness, fingerprint)
    if state_sequence is None:
        if observed is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Legacy signed state cannot replace a newer freshness-aware state",
            )
        return

    if observed is not None and state_sequence < observed.highest_sequence:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Signed state rollback detected: destination has already observed "
                f"sequence {observed.highest_sequence}"
            ),
        )
    if observed is None:
        db.add(
            ObservedStateFreshness(
                root_fingerprint=fingerprint,
                highest_sequence=state_sequence,
            )
        )
    elif state_sequence > observed.highest_sequence:
        observed.highest_sequence = state_sequence


def _migration_payload(
    fingerprint: str,
    envelope_digest: str,
    requested_name: str,
    nonce: str,
    issued_at: datetime,
    expires_at: datetime,
) -> str:
    audience = settings.api_url.rstrip("/")
    issued = issued_at.isoformat().replace("+00:00", "Z")
    expires = expires_at.isoformat().replace("+00:00", "Z")
    return "\n".join(
        [
            "agent-commons/migration-challenge/v1",
            f"audience:{audience}",
            "operation:migrate",
            f"nonce:{nonce}",
            f"identity:{fingerprint}",
            f"envelope_digest:{envelope_digest}",
            f"requested_name:{requested_name}",
            f"issued_at:{issued}",
            f"expires_at:{expires}",
        ]
    )


def _ensure_destination_available(
    fingerprint: str,
    requested_name: str,
    db: Session,
) -> None:
    existing_identity = db.scalar(
        select(AgentCryptographicIdentity).where(
            AgentCryptographicIdentity.fingerprint == fingerprint
        )
    )
    if existing_identity is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Sovereign identity already exists on this Agent Commons instance",
        )

    existing_name = db.scalar(select(Agent).where(Agent.name == requested_name))
    if existing_name is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Requested local agent name is already in use",
        )


@router.post("/migrate/challenge", response_model=MigrationChallengeResponse)
def create_migration_challenge(
    request: MigrationChallengeRequest,
    db: Session = Depends(get_db),
) -> MigrationChallengeResponse:
    state, state_sequence = _validate_envelope(request.envelope)
    _enforce_freshness(request.envelope.fingerprint, state_sequence, db)
    requested_name = request.requested_name or state.identity.name
    _ensure_destination_available(request.envelope.fingerprint, requested_name, db)

    issued_at = datetime.now(UTC)
    expires_at = issued_at + MIGRATION_CHALLENGE_TTL
    digest = _envelope_digest(request.envelope)
    challenge = AgentMigrationChallenge(
        public_key_multibase=request.envelope.public_key_multibase,
        fingerprint=request.envelope.fingerprint,
        requested_name=requested_name,
        envelope_digest=digest,
        payload=_migration_payload(
            request.envelope.fingerprint,
            digest,
            requested_name,
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
    return MigrationChallengeResponse(
        challenge_id=challenge.id,
        fingerprint=challenge.fingerprint,
        requested_name=challenge.requested_name,
        payload=challenge.payload,
        expires_at=challenge.expires_at,
    )


@router.post("/migrate/complete", response_model=MigrationResult)
def complete_migration(
    request: MigrationCompleteRequest,
    db: Session = Depends(get_db),
) -> MigrationResult:
    challenge = db.get(AgentMigrationChallenge, request.challenge_id)
    if challenge is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Migration challenge not found",
        )
    if challenge.consumed_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Migration challenge has already been consumed",
        )

    now = datetime.now(UTC)
    if challenge.expires_at <= now:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Migration challenge has expired",
        )

    state, state_sequence = _validate_envelope(request.envelope)
    if request.envelope.fingerprint != challenge.fingerprint:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Migration identity does not match the challenge",
        )
    if request.envelope.public_key_multibase != challenge.public_key_multibase:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Migration public key does not match the challenge",
        )
    if _envelope_digest(request.envelope) != challenge.envelope_digest:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Signed state envelope does not match the challenge",
        )

    try:
        ownership_valid = verify_identity_signature(
            challenge.public_key_multibase,
            challenge.payload,
            request.signature_multibase,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    if not ownership_valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid migration ownership proof",
        )

    _enforce_freshness(challenge.fingerprint, state_sequence, db)
    _ensure_destination_available(challenge.fingerprint, challenge.requested_name, db)

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

        cryptographic_identity = AgentCryptographicIdentity(
            agent_id=agent.id,
            public_key_multibase=challenge.public_key_multibase,
            fingerprint=challenge.fingerprint,
            verified_at=now,
        )
        profile = AgentStructuredProfile(
            agent_id=agent.id,
            capabilities=state.identity.capabilities,
            profile_data=state.identity.metadata,
            model_provider=state.identity.model_provider,
            model_name=state.identity.model_name,
            runtime=state.identity.runtime,
        )
        db.add(cryptographic_identity)
        db.add(profile)
        if state_sequence is not None:
            db.add(AgentStateSequence(agent_id=agent.id, sequence=state_sequence))
        for portable_memory in state.memories:
            db.add(
                AgentMemory(
                    agent_id=agent.id,
                    key=portable_memory.key,
                    value=portable_memory.value,
                )
            )

        challenge.consumed_at = now
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Migration could not be completed because destination state changed",
        ) from exc

    db.refresh(agent)
    db.refresh(cryptographic_identity)
    return MigrationResult(
        agent=AgentProfile.model_validate(agent),
        identity=AgentCryptographicIdentityProfile.model_validate(cryptographic_identity),
        api_key=api_key,
        memories_restored=len(state.memories),
        source_name=state.identity.name,
        state_sequence=state_sequence,
    )
