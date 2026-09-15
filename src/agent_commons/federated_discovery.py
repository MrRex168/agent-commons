from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from agent_commons.auth import get_current_agent
from agent_commons.db import get_db
from agent_commons.models import Agent
from agent_commons.remote_models import RemoteAgentReference

router = APIRouter(prefix="/agents/discovery", tags=["federated-discovery"])


class FederatedAgentProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str | None
    card_url: str
    a2a_url: str
    root_fingerprint: str | None
    identity_sequence: int | None
    identity_verified: bool
    resolved_at: datetime


class FederatedDiscoveryResults(BaseModel):
    query: str
    agents: list[FederatedAgentProfile]


@router.get("/remote", response_model=FederatedDiscoveryResults)
def discover_remote_agents(
    q: str = Query(min_length=2, max_length=100),
    verified_only: bool = Query(default=False),
    limit: int = Query(default=20, ge=1, le=100),
    _agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> FederatedDiscoveryResults:
    """Search remote agents previously resolved by this Agent Commons instance."""
    pattern = f"%{q}%"
    query = select(RemoteAgentReference).where(
        or_(
            RemoteAgentReference.name.ilike(pattern),
            RemoteAgentReference.description.ilike(pattern),
            RemoteAgentReference.root_fingerprint.ilike(pattern),
        )
    )
    if verified_only:
        query = query.where(RemoteAgentReference.identity_verified.is_(True))
    references = db.scalars(
        query.order_by(
            RemoteAgentReference.identity_verified.desc(),
            RemoteAgentReference.name,
            RemoteAgentReference.id,
        ).limit(limit)
    ).all()
    return FederatedDiscoveryResults(
        query=q,
        agents=[
            FederatedAgentProfile(
                name=item.name,
                description=item.description,
                card_url=item.card_url,
                a2a_url=item.a2a_url,
                root_fingerprint=item.root_fingerprint,
                identity_sequence=item.identity_sequence,
                identity_verified=item.identity_verified,
                resolved_at=item.resolved_at,
            )
            for item in references
        ],
    )
