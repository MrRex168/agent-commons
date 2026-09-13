from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from agent_commons.agents import _portable_state
from agent_commons.auth import get_current_agent
from agent_commons.db import get_db
from agent_commons.freshness_models import AgentStateSequence
from agent_commons.identity_crypto import identity_fingerprint, verify_identity_signature
from agent_commons.models import Agent, AgentCryptographicIdentity
from agent_commons.schemas import (
    PortableStateSigningPayload,
    SignedPortableStateEnvelope,
    SignedPortableStateSubmission,
    SignedPortableStateVerification,
)
from agent_commons.state_serialization import (
    canonical_state_payload,
    parse_state_payload_details,
)

router = APIRouter(prefix="/agents", tags=["state-freshness"])


def _next_state_sequence(agent_id, db: Session) -> int:
    counter = db.scalar(
        select(AgentStateSequence)
        .where(AgentStateSequence.agent_id == agent_id)
        .with_for_update()
    )
    if counter is None:
        counter = AgentStateSequence(agent_id=agent_id, sequence=1)
        db.add(counter)
    else:
        counter.sequence += 1
    db.flush()
    return counter.sequence


@router.get(
    "/me/state/freshness/signing-payload",
    response_model=PortableStateSigningPayload,
)
def create_fresh_signing_payload(
    agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> PortableStateSigningPayload:
    identity = db.get(AgentCryptographicIdentity, agent.id)
    if identity is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Bind a cryptographic identity before creating signed state",
        )

    sequence = _next_state_sequence(agent.id, db)
    state = _portable_state(agent, db)
    payload = canonical_state_payload(state, identity.fingerprint, sequence)
    db.commit()
    return PortableStateSigningPayload(
        fingerprint=identity.fingerprint,
        public_key_multibase=identity.public_key_multibase,
        state_sequence=sequence,
        payload=payload,
        state=state,
    )


@router.post(
    "/me/state/freshness/signed-export",
    response_model=SignedPortableStateEnvelope,
)
def create_fresh_signed_export(
    submission: SignedPortableStateSubmission,
    agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> SignedPortableStateEnvelope:
    identity = db.get(AgentCryptographicIdentity, agent.id)
    if identity is None:
        raise HTTPException(status_code=409, detail="Cryptographic identity not bound")

    try:
        parsed = parse_state_payload_details(submission.payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if parsed.version != 2 or parsed.state_sequence is None:
        raise HTTPException(
            status_code=422,
            detail="Fresh signed export requires a version 2 state payload",
        )
    if parsed.fingerprint != identity.fingerprint:
        raise HTTPException(status_code=409, detail="Signed state identity does not match")
    if parsed.state.identity.id != agent.id or parsed.state.identity.name != agent.name:
        raise HTTPException(status_code=409, detail="Signed state local identity does not match")

    counter = db.get(AgentStateSequence, agent.id)
    if counter is None or parsed.state_sequence > counter.sequence:
        raise HTTPException(status_code=409, detail="State sequence was not issued by this server")

    try:
        valid = verify_identity_signature(
            identity.public_key_multibase,
            submission.payload,
            submission.signature_multibase,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not valid:
        raise HTTPException(status_code=401, detail="Invalid signed state signature")

    return SignedPortableStateEnvelope(
        version=2,
        fingerprint=identity.fingerprint,
        public_key_multibase=identity.public_key_multibase,
        state_sequence=parsed.state_sequence,
        payload=submission.payload,
        signature_multibase=submission.signature_multibase,
    )


@router.post(
    "/state/freshness/verify",
    response_model=SignedPortableStateVerification,
)
def verify_fresh_signed_state(
    envelope: SignedPortableStateEnvelope,
) -> SignedPortableStateVerification:
    try:
        parsed = parse_state_payload_details(envelope.payload)
        expected_fingerprint = identity_fingerprint(envelope.public_key_multibase)
        valid = verify_identity_signature(
            envelope.public_key_multibase,
            envelope.payload,
            envelope.signature_multibase,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if parsed.version != 2 or parsed.state_sequence is None:
        raise HTTPException(
            status_code=422,
            detail="Envelope does not contain freshness metadata",
        )
    if envelope.version != 2 or envelope.state_sequence != parsed.state_sequence:
        raise HTTPException(
            status_code=422,
            detail="Envelope freshness metadata does not match payload",
        )
    if (
        envelope.fingerprint != expected_fingerprint
        or parsed.fingerprint != envelope.fingerprint
    ):
        raise HTTPException(
            status_code=422,
            detail="Envelope identity does not match payload or key",
        )

    return SignedPortableStateVerification(
        valid=valid,
        fingerprint=envelope.fingerprint,
        state_sequence=parsed.state_sequence,
        state=parsed.state,
    )
