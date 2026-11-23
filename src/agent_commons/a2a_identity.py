from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from agent_commons.endpoint_authorization import SignedEndpointAuthorization
from agent_commons.lineage import export_identity_lineage
from agent_commons.models import Agent, AgentCryptographicIdentity
from agent_commons.rotation import ensure_key_state

SOVEREIGN_EXTENSION_PREFIX = "urn:agent-commons:extension:sovereign-identity:"
SOVEREIGN_IDENTITY_EXTENSION_URI = SOVEREIGN_EXTENSION_PREFIX + "v1"

SOVEREIGN_ENDPOINT_EXTENSION_URI = SOVEREIGN_EXTENSION_PREFIX + "v2"


def build_sovereign_identity_params(agent: Agent, db: Session) -> dict[str, Any]:
    identity = db.get(AgentCryptographicIdentity, agent.id)
    if identity is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Bind a cryptographic identity before publishing sovereign A2A identity",
        )

    key_state = ensure_key_state(identity, db)
    lineage = export_identity_lineage(agent=agent, db=db)
    return {
        "rootFingerprint": key_state.root_fingerprint,
        "currentControllerPublicKey": key_state.current_public_key_multibase,
        "identitySequence": key_state.sequence,
        "lineage": lineage.model_dump(mode="json"),
    }


def build_sovereign_endpoint_params(
    params: dict[str, Any], proof: Any, *, endpoint: str, transport: str
) -> dict[str, Any]:
    try:
        signed = SignedEndpointAuthorization.model_validate(proof)
        signed.verify(
            root=params["rootFingerprint"],
            controller=params["currentControllerPublicKey"],
            identity_sequence=params["identitySequence"],
            card_url=signed.authorization.card_url,
            a2a_url=endpoint,
            transport=transport,
            now=datetime.now(UTC),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Invalid or stale configured sovereign endpoint authorization",
        ) from exc
    return {**params, "endpointAuthorization": signed.model_dump(mode="json")}
