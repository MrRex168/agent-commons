from __future__ import annotations

import ipaddress
import json
import socket
import uuid
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, HttpUrl
from sqlalchemy import select
from sqlalchemy.orm import Session

from agent_commons.a2a import A2A_PROTOCOL_VERSION, A2AAgentCard
from agent_commons.a2a_identity import SOVEREIGN_IDENTITY_EXTENSION_URI
from agent_commons.auth import get_current_agent
from agent_commons.db import get_db
from agent_commons.lineage import PortableIdentityLineage, verify_lineage
from agent_commons.models import Agent
from agent_commons.remote_models import RemoteAgentReference

router = APIRouter(prefix="/agents/remote", tags=["remote-agents"])
MAX_AGENT_CARD_BYTES = 512 * 1024


class RemoteAgentResolveRequest(BaseModel):
    agent_card_url: HttpUrl


class RemoteAgentReferenceProfile(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    card_url: str
    name: str
    description: str | None
    a2a_url: str
    preferred_transport: str
    root_fingerprint: str | None
    current_controller_public_key: str | None
    identity_sequence: int | None
    identity_verified: bool
    lineage: dict[str, Any] | None
    card_snapshot: dict[str, Any]
    resolved_at: datetime
    created_at: datetime
    updated_at: datetime


def _host_addresses(hostname: str) -> set[str]:
    try:
        results = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
        return {item[4][0] for item in results}
    except socket.gaierror as exc:
        raise ValueError("Agent Card hostname could not be resolved") from exc


def _is_public_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    return address.is_global


def _validate_card_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError("Remote Agent Card URL must use HTTPS")
    if not parsed.hostname or parsed.username or parsed.password:
        raise ValueError(
            "Remote Agent Card URL must contain a public hostname without credentials"
        )
    hostname = parsed.hostname.lower().rstrip(".")
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise ValueError("Localhost Agent Card URLs are not allowed")

    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        addresses = _host_addresses(hostname)
    else:
        addresses = {str(literal)}

    if not addresses or any(not _is_public_address(item) for item in addresses):
        raise ValueError(
            "Remote Agent Card hostname must resolve only to public IP addresses"
        )


def _read_limited_response(response: httpx.Response) -> bytes:
    declared_size = response.headers.get("Content-Length")
    if declared_size is not None:
        try:
            if int(declared_size) > MAX_AGENT_CARD_BYTES:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail="Remote Agent Card exceeds the maximum allowed size",
                )
        except ValueError:
            pass

    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes():
        total += len(chunk)
        if total > MAX_AGENT_CARD_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="Remote Agent Card exceeds the maximum allowed size",
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _fetch_agent_card(url: str) -> A2AAgentCard:
    _validate_card_url(url)
    try:
        with httpx.Client(timeout=5.0, follow_redirects=False) as client:
            with client.stream(
                "GET",
                url,
                headers={"Accept": "application/json"},
            ) as response:
                response.raise_for_status()
                raw = _read_limited_response(response)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Remote Agent Card could not be fetched",
        ) from exc

    try:
        card = A2AAgentCard.model_validate(json.loads(raw))
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Remote Agent Card is invalid: {exc}",
        ) from exc
    if card.protocolVersion != A2A_PROTOCOL_VERSION:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Remote Agent Card must use A2A {A2A_PROTOCOL_VERSION}",
        )
    return card


def _verified_identity(
    card: A2AAgentCard,
) -> tuple[str | None, str | None, int | None, dict[str, Any] | None]:
    extensions = card.capabilities.extensions or []
    matching = [
        item for item in extensions if item.uri == SOVEREIGN_IDENTITY_EXTENSION_URI
    ]
    if not matching:
        return None, None, None, None
    if len(matching) != 1 or not isinstance(matching[0].params, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Agent Commons sovereign identity extension is malformed",
        )

    params = matching[0].params
    try:
        root_fingerprint = str(params["rootFingerprint"])
        controller = str(params["currentControllerPublicKey"])
        sequence = int(params["identitySequence"])
        lineage = PortableIdentityLineage.model_validate(params["lineage"])
        verification = verify_lineage(lineage)
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Agent Commons sovereign identity proof is invalid: {exc}",
        ) from exc

    if (
        verification.root_fingerprint != root_fingerprint
        or verification.current_public_key_multibase != controller
        or verification.sequence != sequence
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Agent Commons identity extension does not match its verified lineage",
        )
    return root_fingerprint, controller, sequence, lineage.model_dump(mode="json")


def _assert_identity_progression(
    reference: RemoteAgentReference,
    root: str | None,
    controller: str | None,
    sequence: int | None,
) -> None:
    """Reject identity downgrade, rollback, and same-sequence controller forks."""
    if reference.identity_verified and root is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Verified remote identity cannot be downgraded to an unverified Agent Card",
        )
    if not reference.identity_verified or root is None:
        return
    if reference.root_fingerprint != root:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Remote Agent Card presents a different sovereign root identity",
        )
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


def _store_reference(
    card_url: str,
    card: A2AAgentCard,
    db: Session,
) -> RemoteAgentReference:
    root, controller, sequence, lineage = _verified_identity(card)
    by_url = db.scalar(
        select(RemoteAgentReference).where(RemoteAgentReference.card_url == card_url)
    )
    by_root = None
    if root is not None:
        by_root = db.scalar(
            select(RemoteAgentReference).where(
                RemoteAgentReference.root_fingerprint == root
            )
        )
    if by_url is not None and by_url.root_fingerprint not in (None, root):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Agent Card URL now presents a different sovereign identity",
        )
    if by_url is not None and by_root is not None and by_url.id != by_root.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Remote sovereign identity is already tracked by another reference",
        )

    reference = by_root or by_url
    if reference is not None:
        _assert_identity_progression(reference, root, controller, sequence)
    if reference is None:
        reference = RemoteAgentReference(
            card_url=card_url,
            name=card.name,
            a2a_url=card.url,
        )
        db.add(reference)

    reference.card_url = card_url
    reference.name = card.name
    reference.description = card.description
    reference.a2a_url = card.url
    reference.preferred_transport = card.preferredTransport
    reference.root_fingerprint = root
    reference.current_controller_public_key = controller
    reference.identity_sequence = sequence
    reference.identity_verified = root is not None
    reference.lineage = lineage
    reference.card_snapshot = card.model_dump(mode="json")
    reference.resolved_at = datetime.now(UTC)
    db.commit()
    db.refresh(reference)
    return reference


@router.post("/resolve", response_model=RemoteAgentReferenceProfile)
def resolve_remote_agent(
    payload: RemoteAgentResolveRequest,
    _agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> RemoteAgentReferenceProfile:
    card_url = str(payload.agent_card_url)
    try:
        card = _fetch_agent_card(card_url)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    reference = _store_reference(card_url, card, db)
    return RemoteAgentReferenceProfile.model_validate(reference)


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
    return [RemoteAgentReferenceProfile.model_validate(item) for item in references]


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
    return RemoteAgentReferenceProfile.model_validate(reference)


@router.post("/{reference_id}/refresh", response_model=RemoteAgentReferenceProfile)
def refresh_remote_agent(
    reference_id: uuid.UUID,
    _agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> RemoteAgentReferenceProfile:
    """Re-fetch a known Agent Card while enforcing sovereign identity continuity."""
    reference = db.get(RemoteAgentReference, reference_id)
    if reference is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Remote agent not found",
        )
    try:
        card = _fetch_agent_card(reference.card_url)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    refreshed = _store_reference(reference.card_url, card, db)
    return RemoteAgentReferenceProfile.model_validate(refreshed)
