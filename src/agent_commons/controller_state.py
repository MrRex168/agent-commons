from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.orm import Session

from agent_commons.agents import _portable_state
from agent_commons.auth import get_current_agent
from agent_commons.db import get_db
from agent_commons.freshness import _next_state_sequence
from agent_commons.freshness_models import AgentStateSequence
from agent_commons.identity_crypto import verify_identity_signature
from agent_commons.lineage import (
    PortableIdentityLineage,
    export_identity_lineage,
    verify_lineage,
)
from agent_commons.models import Agent, AgentCryptographicIdentity
from agent_commons.rotation import ensure_key_state
from agent_commons.schemas import PortableAgentState

router = APIRouter(prefix="/agents", tags=["controller-signed-state"])

STATE_ENVELOPE_DOMAIN_V3 = "agent-commons/signed-state/v3"


class ControllerStateSigningPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root_fingerprint: str
    controller_public_key_multibase: str
    identity_sequence: int = Field(ge=0)
    state_sequence: int = Field(ge=1)
    lineage_digest: str
    payload: str
    state: PortableAgentState
    lineage: PortableIdentityLineage


class ControllerStateSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payload: str = Field(min_length=1, max_length=10_000_000)
    signature_multibase: str = Field(min_length=2, max_length=256)


class ControllerSignedStateEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: Literal["agent-commons-signed-state"] = "agent-commons-signed-state"
    version: Literal[3] = 3
    root_fingerprint: str
    controller_public_key_multibase: str
    identity_sequence: int = Field(ge=0)
    state_sequence: int = Field(ge=1)
    lineage_digest: str
    payload: str
    signature_multibase: str


class ControllerStateVerificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    envelope: ControllerSignedStateEnvelope
    lineage: PortableIdentityLineage


class ControllerSignedStateVerification(BaseModel):
    valid: bool
    root_fingerprint: str
    controller_public_key_multibase: str
    identity_sequence: int
    state_sequence: int
    lineage_digest: str
    state: PortableAgentState


@dataclass(frozen=True)
class ParsedControllerStatePayload:
    root_fingerprint: str
    controller_public_key_multibase: str
    identity_sequence: int
    state_sequence: int
    lineage_digest: str
    state: PortableAgentState


def _lineage_digest(lineage: PortableIdentityLineage) -> str:
    canonical = json.dumps(
        lineage.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _canonical_controller_state_payload(
    *,
    state: PortableAgentState,
    root_fingerprint: str,
    controller_public_key_multibase: str,
    identity_sequence: int,
    state_sequence: int,
    lineage_digest: str,
) -> str:
    if identity_sequence < 0:
        raise ValueError("Identity sequence must be non-negative")
    if state_sequence < 1:
        raise ValueError("State sequence must be at least 1")
    document = {
        "controller_public_key_multibase": controller_public_key_multibase,
        "format": "agent-commons-signed-state",
        "identity_sequence": identity_sequence,
        "lineage_digest": lineage_digest,
        "root_fingerprint": root_fingerprint,
        "state": state.model_dump(mode="json"),
        "state_sequence": state_sequence,
        "version": 3,
    }
    canonical = json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"{STATE_ENVELOPE_DOMAIN_V3}\n{canonical}"


def _parse_controller_state_payload(payload: str) -> ParsedControllerStatePayload:
    prefix = f"{STATE_ENVELOPE_DOMAIN_V3}\n"
    if not payload.startswith(prefix):
        raise ValueError("Invalid controller-signed state payload domain")
    try:
        document = json.loads(payload[len(prefix) :])
    except json.JSONDecodeError as exc:
        raise ValueError("Controller-signed state payload is not valid JSON") from exc
    if not isinstance(document, dict):
        raise ValueError("Controller-signed state payload must contain a JSON object")

    expected_fields = {
        "controller_public_key_multibase",
        "format",
        "identity_sequence",
        "lineage_digest",
        "root_fingerprint",
        "state",
        "state_sequence",
        "version",
    }
    if set(document) != expected_fields:
        raise ValueError("Controller-signed state payload contains unsupported fields")
    if document["format"] != "agent-commons-signed-state" or document["version"] != 3:
        raise ValueError("Unsupported controller-signed state format or version")

    identity_sequence = document["identity_sequence"]
    state_sequence = document["state_sequence"]
    if (
        not isinstance(identity_sequence, int)
        or isinstance(identity_sequence, bool)
        or identity_sequence < 0
    ):
        raise ValueError("Controller-signed identity sequence is invalid")
    if (
        not isinstance(state_sequence, int)
        or isinstance(state_sequence, bool)
        or state_sequence < 1
    ):
        raise ValueError("Controller-signed state sequence is invalid")

    for field in (
        "root_fingerprint",
        "controller_public_key_multibase",
        "lineage_digest",
    ):
        if not isinstance(document[field], str) or not document[field]:
            raise ValueError(f"Controller-signed state {field} is invalid")

    try:
        state = PortableAgentState.model_validate(document["state"])
    except ValidationError as exc:
        raise ValueError("Controller-signed payload contains invalid portable state") from exc

    expected = _canonical_controller_state_payload(
        state=state,
        root_fingerprint=document["root_fingerprint"],
        controller_public_key_multibase=document["controller_public_key_multibase"],
        identity_sequence=identity_sequence,
        state_sequence=state_sequence,
        lineage_digest=document["lineage_digest"],
    )
    if payload != expected:
        raise ValueError("Controller-signed state payload is not canonically serialized")

    return ParsedControllerStatePayload(
        root_fingerprint=document["root_fingerprint"],
        controller_public_key_multibase=document["controller_public_key_multibase"],
        identity_sequence=identity_sequence,
        state_sequence=state_sequence,
        lineage_digest=document["lineage_digest"],
        state=state,
    )


@router.get(
    "/me/state/controller/signing-payload",
    response_model=ControllerStateSigningPayload,
)
def create_controller_state_signing_payload(
    agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> ControllerStateSigningPayload:
    identity = db.get(AgentCryptographicIdentity, agent.id)
    if identity is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Bind a cryptographic identity before creating signed state",
        )

    key_state = ensure_key_state(identity, db)
    lineage = export_identity_lineage(agent, db)
    lineage_verification = verify_lineage(lineage)
    if (
        lineage_verification.current_public_key_multibase
        != key_state.current_public_key_multibase
        or lineage_verification.sequence != key_state.sequence
        or lineage_verification.root_fingerprint != key_state.root_fingerprint
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Current key state does not match portable identity lineage",
        )

    sequence = _next_state_sequence(agent.id, db)
    state = _portable_state(agent, db)
    digest = _lineage_digest(lineage)
    payload = _canonical_controller_state_payload(
        state=state,
        root_fingerprint=key_state.root_fingerprint,
        controller_public_key_multibase=key_state.current_public_key_multibase,
        identity_sequence=key_state.sequence,
        state_sequence=sequence,
        lineage_digest=digest,
    )
    db.commit()
    return ControllerStateSigningPayload(
        root_fingerprint=key_state.root_fingerprint,
        controller_public_key_multibase=key_state.current_public_key_multibase,
        identity_sequence=key_state.sequence,
        state_sequence=sequence,
        lineage_digest=digest,
        payload=payload,
        state=state,
        lineage=lineage,
    )


@router.post(
    "/me/state/controller/signed-export",
    response_model=ControllerSignedStateEnvelope,
)
def create_controller_signed_export(
    submission: ControllerStateSubmission,
    agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> ControllerSignedStateEnvelope:
    identity = db.get(AgentCryptographicIdentity, agent.id)
    if identity is None:
        raise HTTPException(status_code=409, detail="Cryptographic identity not bound")
    key_state = ensure_key_state(identity, db)

    try:
        parsed = _parse_controller_state_payload(submission.payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if parsed.root_fingerprint != key_state.root_fingerprint:
        raise HTTPException(status_code=409, detail="Signed state root identity does not match")
    if parsed.controller_public_key_multibase != key_state.current_public_key_multibase:
        raise HTTPException(status_code=409, detail="Signed state controller is not current")
    if parsed.identity_sequence != key_state.sequence:
        raise HTTPException(status_code=409, detail="Signed state identity sequence is stale")
    if parsed.state.identity.id != agent.id or parsed.state.identity.name != agent.name:
        raise HTTPException(status_code=409, detail="Signed state local identity does not match")

    lineage = export_identity_lineage(agent, db)
    if parsed.lineage_digest != _lineage_digest(lineage):
        raise HTTPException(status_code=409, detail="Signed state lineage digest is stale")

    counter = db.get(AgentStateSequence, agent.id)
    if counter is None or parsed.state_sequence > counter.sequence:
        raise HTTPException(status_code=409, detail="State sequence was not issued by this server")

    try:
        valid = verify_identity_signature(
            key_state.current_public_key_multibase,
            submission.payload,
            submission.signature_multibase,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not valid:
        raise HTTPException(status_code=401, detail="Invalid current-controller state signature")

    return ControllerSignedStateEnvelope(
        root_fingerprint=parsed.root_fingerprint,
        controller_public_key_multibase=parsed.controller_public_key_multibase,
        identity_sequence=parsed.identity_sequence,
        state_sequence=parsed.state_sequence,
        lineage_digest=parsed.lineage_digest,
        payload=submission.payload,
        signature_multibase=submission.signature_multibase,
    )


@router.post(
    "/state/controller/verify",
    response_model=ControllerSignedStateVerification,
)
def verify_controller_signed_state(
    request: ControllerStateVerificationRequest,
) -> ControllerSignedStateVerification:
    envelope = request.envelope
    try:
        parsed = _parse_controller_state_payload(envelope.payload)
        lineage_verification = verify_lineage(request.lineage)
        digest = _lineage_digest(request.lineage)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if envelope.root_fingerprint != parsed.root_fingerprint:
        raise HTTPException(status_code=422, detail="Envelope root identity does not match payload")
    if envelope.controller_public_key_multibase != parsed.controller_public_key_multibase:
        raise HTTPException(status_code=422, detail="Envelope controller key does not match payload")
    if envelope.identity_sequence != parsed.identity_sequence:
        raise HTTPException(status_code=422, detail="Envelope identity sequence does not match payload")
    if envelope.state_sequence != parsed.state_sequence:
        raise HTTPException(status_code=422, detail="Envelope state sequence does not match payload")
    if envelope.lineage_digest != parsed.lineage_digest or digest != parsed.lineage_digest:
        raise HTTPException(status_code=422, detail="Portable lineage digest does not match signed state")
    if lineage_verification.root_fingerprint != parsed.root_fingerprint:
        raise HTTPException(status_code=422, detail="Portable lineage root does not match signed state")
    if (
        lineage_verification.current_public_key_multibase
        != parsed.controller_public_key_multibase
    ):
        raise HTTPException(status_code=422, detail="Signed state key is not the verified current controller")
    if lineage_verification.sequence != parsed.identity_sequence:
        raise HTTPException(status_code=422, detail="Portable lineage sequence does not match signed state")

    try:
        valid = verify_identity_signature(
            parsed.controller_public_key_multibase,
            envelope.payload,
            envelope.signature_multibase,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not valid:
        raise HTTPException(status_code=401, detail="Invalid current-controller state signature")

    return ControllerSignedStateVerification(
        valid=True,
        root_fingerprint=parsed.root_fingerprint,
        controller_public_key_multibase=parsed.controller_public_key_multibase,
        identity_sequence=parsed.identity_sequence,
        state_sequence=parsed.state_sequence,
        lineage_digest=parsed.lineage_digest,
        state=parsed.state,
    )
