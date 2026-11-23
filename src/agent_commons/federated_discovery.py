from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from agent_commons.auth import get_current_agent
from agent_commons.db import get_db
from agent_commons.models import Agent
from agent_commons.remote_models import EndpointStatus, RemoteAgentReference

router = APIRouter(prefix="/agents/discovery", tags=["federated-discovery"])


class FederatedAgentSkill(BaseModel):
    id: str
    name: str
    description: str
    tags: list[str]


class FederatedAgentProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str | None
    card_url: str | None
    a2a_url: str | None
    root_fingerprint: str | None
    identity_sequence: int | None
    identity_verified: bool
    lineage_verified: bool
    endpoint_authorization_status: EndpointStatus
    resolved_at: datetime
    skills: list[FederatedAgentSkill]


class FederatedDiscoveryResults(BaseModel):
    query: str
    skill: str | None
    agents: list[FederatedAgentProfile]


def _skills(reference: RemoteAgentReference) -> list[FederatedAgentSkill]:
    raw = reference.card_snapshot.get("skills", [])
    if not isinstance(raw, list):
        return []
    skills: list[FederatedAgentSkill] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            skills.append(FederatedAgentSkill.model_validate(item))
        except ValueError:
            continue
    return skills


def _skill_matches(skills: list[FederatedAgentSkill], needle: str) -> bool:
    value = needle.casefold()
    for skill in skills:
        fields: list[Any] = [skill.id, skill.name, skill.description, *skill.tags]
        if any(value in str(field).casefold() for field in fields):
            return True
    return False


def _profile(reference: RemoteAgentReference, now: datetime) -> FederatedAgentProfile:
    endpoint_status = reference.endpoint_status(now)
    routable = endpoint_status in ("authorized", "non_sovereign")
    return FederatedAgentProfile(
        name=reference.name,
        description=reference.description,
        card_url=reference.card_url if routable else None,
        a2a_url=reference.a2a_url if routable else None,
        root_fingerprint=reference.root_fingerprint,
        identity_sequence=reference.identity_sequence,
        identity_verified=endpoint_status == "authorized",
        lineage_verified=reference.identity_verified,
        endpoint_authorization_status=endpoint_status,
        resolved_at=reference.resolved_at,
        skills=_skills(reference),
    )


@router.get("/remote", response_model=FederatedDiscoveryResults)
def discover_remote_agents(
    q: str = Query(default="", max_length=100),
    skill: str | None = Query(default=None, min_length=2, max_length=100),
    verified_only: bool = Query(default=False),
    limit: int = Query(default=20, ge=1, le=100),
    _agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> FederatedDiscoveryResults:
    """Search resolved remote agents by identity metadata and A2A skills."""
    query = select(RemoteAgentReference)
    if q:
        pattern = f"%{q}%"
        query = query.where(
            or_(
                RemoteAgentReference.name.ilike(pattern),
                RemoteAgentReference.description.ilike(pattern),
                RemoteAgentReference.root_fingerprint.ilike(pattern),
            )
        )
    now = datetime.now(UTC)
    authorized = and_(
        RemoteAgentReference.identity_verified.is_(True),
        RemoteAgentReference.location_expires_at.is_not(None),
        RemoteAgentReference.location_expires_at > now,
    )
    if verified_only:
        query = query.where(authorized)

    references = db.scalars(
        query.order_by(
            authorized.desc(),
            RemoteAgentReference.name,
            RemoteAgentReference.id,
        )
    ).all()
    profiles = [_profile(item, now) for item in references]
    if skill is not None:
        profiles = [item for item in profiles if _skill_matches(item.skills, skill)]
    return FederatedDiscoveryResults(query=q, skill=skill, agents=profiles[:limit])
