from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from agent_commons.lineage import export_identity_lineage
from agent_commons.models import Agent, AgentCryptographicIdentity
from agent_commons.rotation import ensure_key_state

SOVEREIGN_IDENTITY_EXTENSION_URI = "urn:agent-commons:extension:sovereign-identity:v1"


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
