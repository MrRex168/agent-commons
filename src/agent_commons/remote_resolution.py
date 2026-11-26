from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, HttpUrl
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from agent_commons import outbound_https
from agent_commons.a2a import A2A_PROTOCOL_VERSION, A2AAgentCard
from agent_commons.a2a_identity import (
    SOVEREIGN_EXTENSION_PREFIX,
)
from agent_commons.auth import get_current_agent
from agent_commons.config import settings
from agent_commons.db import get_db
from agent_commons.endpoint_authorization import (
    SOVEREIGN_ENDPOINT_EXTENSION_URI,
    SignedEndpointAuthorization,
)
from agent_commons.lineage import PortableIdentityLineage, verify_lineage
from agent_commons.models import Agent
from agent_commons.preview_limits import consume, shard
from agent_commons.remote_models import EndpointStatus, RemoteAgentReference

router = APIRouter(prefix="/agents/remote", tags=["remote-agents"])
MAX_AGENT_CARD_BYTES = 512 * 1024


class RemoteAgentResolveRequest(BaseModel):
    agent_card_url: HttpUrl


class RemoteAgentReferenceProfile(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    card_url: str | None
    name: str
    description: str | None
    a2a_url: str | None
    preferred_transport: str | None
    root_fingerprint: str | None
    current_controller_public_key: str | None
    identity_sequence: int | None
    identity_verified: bool
    lineage_verified: bool
    endpoint_authorization_status: EndpointStatus
    endpoint_authorization_expires_at: datetime | None
    cached_card_url: str
    cached_a2a_url: str
    lineage: dict[str, Any] | None
    card_snapshot: dict[str, Any]
    resolved_at: datetime
    created_at: datetime
    updated_at: datetime


def _profile(reference: RemoteAgentReference) -> RemoteAgentReferenceProfile:
    endpoint_status = reference.endpoint_status()
    routable = endpoint_status in ("authorized", "non_sovereign")
    return RemoteAgentReferenceProfile(
        id=reference.id,
        card_url=reference.card_url if routable else None,
        a2a_url=reference.a2a_url if routable else None,
        preferred_transport=reference.preferred_transport if routable else None,
        name=reference.name,
        description=reference.description,
        root_fingerprint=reference.root_fingerprint,
        current_controller_public_key=reference.current_controller_public_key,
        identity_sequence=reference.identity_sequence,
        identity_verified=endpoint_status == "authorized",
        lineage_verified=reference.identity_verified,
        endpoint_authorization_status=endpoint_status,
        endpoint_authorization_expires_at=reference.location_expires_at,
        cached_card_url=reference.card_url,
        cached_a2a_url=reference.a2a_url,
        lineage=reference.lineage,
        card_snapshot=reference.card_snapshot,
        resolved_at=reference.resolved_at,
        created_at=reference.created_at,
        updated_at=reference.updated_at,
    )


_validate_card_url = outbound_https.validate_destination


def _request_card(url: str) -> bytes:
    return outbound_https.request(url, response_limit=MAX_AGENT_CARD_BYTES)


def _fetch_agent_card(url: str) -> A2AAgentCard:
    try:
        raw = _request_card(url)
    except outbound_https.ResponseTooLarge:
        raise HTTPException(
            status_code=413, detail="Remote Agent Card exceeds the maximum allowed size"
        ) from None
    except outbound_https.TransferError:
        raise HTTPException(502, "Remote Agent Card could not be fetched") from None

    try:
        card = A2AAgentCard.model_validate(json.loads(raw))
    except (ValueError, TypeError, RecursionError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Remote Agent Card is invalid",
        ) from None
    if card.protocolVersion != A2A_PROTOCOL_VERSION:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Remote Agent Card must use A2A {A2A_PROTOCOL_VERSION}",
        )
    return card


def _verified_identity(
    card_url: str,
    card: A2AAgentCard,
) -> tuple[PortableIdentityLineage | None, SignedEndpointAuthorization | None]:
    matching = [
        item
        for item in card.capabilities.extensions or []
        if item.uri.startswith(SOVEREIGN_EXTENSION_PREFIX)
    ]
    if not matching:
        return None, None
    try:
        if len(matching) != 1 or matching[0].uri != SOVEREIGN_ENDPOINT_EXTENSION_URI:
            raise ValueError("Sovereign Agent Cards require one v2 endpoint authorization")
        params = matching[0].params
        if not isinstance(params, dict):
            raise ValueError("Malformed sovereign identity extension")

        lineage = PortableIdentityLineage.model_validate(params["lineage"])
        verified = verify_lineage(lineage)
        if (
            params["rootFingerprint"] != verified.root_fingerprint
            or params["currentControllerPublicKey"] != verified.current_public_key_multibase
            or type(params["identitySequence"]) is not int
            or params["identitySequence"] != verified.sequence
        ):
            raise ValueError("Sovereign identity extension does not match verified lineage")

        # HTTPX must send the same URL that is compared against the signature.
        if str(httpx.URL(card_url)) != card_url:
            raise ValueError("Agent Card URL is not canonical for fetching")
        signed = SignedEndpointAuthorization.model_validate(params["endpointAuthorization"])
        if signed.authorization.card_url != card_url:
            raise ValueError("Endpoint authorization does not match the fetched Card URL")
        signed.verify(
            root=verified.root_fingerprint,
            controller=verified.current_public_key_multibase,
            identity_sequence=verified.sequence,
            card_url=card_url,
            a2a_url=card.url,
            transport=card.preferredTransport,
            now=datetime.now(UTC),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail="Invalid sovereign identity or endpoint authorization",
        ) from exc
    return lineage, signed


def _assert_identity_progression(
    reference: RemoteAgentReference,
    root: str | None,
    controller: str | None,
    sequence: int | None,
) -> None:
    """Reject identity downgrade, rollback, and same-sequence controller forks."""
    if (reference.identity_verified or reference.root_fingerprint is not None) and root is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Verified remote identity cannot be downgraded to an unverified Agent Card",
        )
    if reference.root_fingerprint is not None and reference.root_fingerprint != root:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Remote Agent Card presents a different sovereign root identity",
        )
    if not reference.identity_verified:
        return
    if reference.identity_sequence is None or sequence is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Verified remote identity is missing an identity sequence",
        )
    if sequence < reference.identity_sequence:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Remote identity rollback rejected",
        )
    if (
        sequence == reference.identity_sequence
        and reference.current_controller_public_key != controller
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Conflicting remote controller at the same identity sequence",
        )


def _locked_reference(
    db: Session, card_url: str, root: str | None, expected_id: uuid.UUID | None
) -> RemoteAgentReference | None:
    matches = RemoteAgentReference.card_url == card_url
    if root is not None:
        matches = or_(matches, RemoteAgentReference.root_fingerprint == root)
    rows = db.scalars(
        select(RemoteAgentReference)
        .where(matches)
        .order_by(RemoteAgentReference.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    if len(rows) > 1:
        raise HTTPException(status_code=409, detail="Agent Card URL and sovereign root conflict")
    reference = rows[0] if rows else None
    if expected_id is not None and (
        reference is None or reference.id != expected_id or reference.card_url != card_url
    ):
        raise HTTPException(status_code=409, detail="Remote reference moved during refresh")
    return reference


def _accept_location(
    reference: RemoteAgentReference,
    signed: SignedEndpointAuthorization,
    now: datetime,
) -> bool:
    claim = signed.authorization
    try:
        claim.verify_time(now)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if (
        reference.location_sequence is None
        or claim.identity_sequence != reference.identity_sequence
        or claim.location_sequence > reference.location_sequence
    ):
        return True
    if (
        claim.location_sequence == reference.location_sequence
        and claim.digest() == reference.location_digest
    ):
        return False
    raise HTTPException(status_code=409, detail="Stale or conflicting endpoint authorization")


def _store_reference(
    card_url: str,
    card: A2AAgentCard,
    db: Session,
    *,
    expected_id: uuid.UUID | None = None,
) -> RemoteAgentReference:
    if (
        len(card_url) > 500
        or len(card.name) > 200
        or len(card.preferredTransport) > 32
        or len(card.url) > 500
    ):
        raise HTTPException(status_code=422, detail="Remote Agent Card fields exceed size limits")
    lineage, signed = _verified_identity(card_url, card)
    root = lineage.root_fingerprint if lineage else None
    controller = lineage.current_public_key_multibase if lineage else None
    sequence = lineage.sequence if lineage else None
    claim = signed.authorization if signed else None
    for attempt in range(2):
        reference = _locked_reference(db, card_url, root, expected_id)
        now = datetime.now(UTC)
        if reference is not None:
            _assert_identity_progression(reference, root, controller, sequence)
            if signed and not _accept_location(reference, signed, now):
                return reference
        else:
            reference = RemoteAgentReference()
            db.add(reference)
            if signed:
                _accept_location(reference, signed, now)

        reference.card_url = card_url
        reference.name = card.name
        reference.description = card.description
        reference.a2a_url = claim.a2a_url if claim else card.url
        reference.preferred_transport = card.preferredTransport
        reference.root_fingerprint = root
        reference.current_controller_public_key = controller
        reference.identity_sequence = sequence
        reference.identity_verified = root is not None
        reference.lineage = lineage.model_dump(mode="json") if lineage else None
        reference.location_sequence = claim.location_sequence if claim else None
        reference.location_digest = claim.digest() if claim else None
        reference.location_expires_at = claim.expiration() if claim else None
        reference.card_snapshot = card.model_dump(mode="json")
        reference.resolved_at = now
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
            if constraint not in (
                "ix_remote_agent_references_card_url",
                "ix_remote_agent_references_root_fingerprint",
            ):
                raise
            if attempt == 0:
                continue
            raise HTTPException(
                status_code=409, detail="Concurrent remote reference update"
            ) from exc
        return reference
    raise AssertionError("Remote reference write did not complete")


def _admit_resolution(agent: Agent) -> None:
    if settings.preview_limits:
        consume(
            f"resolution-caller:{shard(str(agent.id))}",
            settings.preview_resolutions_per_caller,
            60,
        )
        consume("resolution", settings.preview_resolutions_per_minute, 60)


@router.post("/resolve", response_model=RemoteAgentReferenceProfile)
def resolve_remote_agent(
    payload: RemoteAgentResolveRequest,
    _agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> RemoteAgentReferenceProfile:
    _admit_resolution(_agent)
    card_url = str(payload.agent_card_url)
    try:
        card = _fetch_agent_card(card_url)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    reference = _store_reference(card_url, card, db)
    return _profile(reference)


@router.get("", response_model=list[RemoteAgentReferenceProfile])
def list_remote_agents(
    _agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> list[RemoteAgentReferenceProfile]:
    query = select(RemoteAgentReference).order_by(
        RemoteAgentReference.name,
        RemoteAgentReference.id,
    )
    references = db.scalars(query).all()
    return [_profile(item) for item in references]


@router.get("/{reference_id}", response_model=RemoteAgentReferenceProfile)
def get_remote_agent(
    reference_id: uuid.UUID,
    _agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> RemoteAgentReferenceProfile:
    reference = db.get(RemoteAgentReference, reference_id)
    if reference is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Remote agent not found",
        )
    return _profile(reference)


@router.post("/{reference_id}/refresh", response_model=RemoteAgentReferenceProfile)
def refresh_remote_agent(
    reference_id: uuid.UUID,
    _agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> RemoteAgentReferenceProfile:
    """Re-fetch a known Agent Card while enforcing sovereign identity continuity."""
    _admit_resolution(_agent)
    reference = db.get(RemoteAgentReference, reference_id)
    if reference is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Remote agent not found",
        )
    card_url = reference.card_url
    try:
        card = _fetch_agent_card(card_url)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return _profile(_store_reference(card_url, card, db, expected_id=reference_id))
