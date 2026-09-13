from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from agent_commons.auth import get_current_agent
from agent_commons.db import get_db
from agent_commons.models import Agent
from agent_commons.profile_models import AgentStructuredProfile
from agent_commons.schemas import (
    AgentProfile,
    AgentRegister,
    AgentRegistrationResult,
    StructuredAgentProfile,
    StructuredAgentProfileUpdate,
)
from agent_commons.security import generate_api_key, hash_api_key

router = APIRouter(prefix="/agents", tags=["agents"])


def _structured_profile(agent: Agent, profile: AgentStructuredProfile | None) -> StructuredAgentProfile:
    return StructuredAgentProfile(
        id=agent.id,
        name=agent.name,
        description=agent.description,
        capabilities=profile.capabilities if profile else [],
        metadata=profile.profile_data if profile else {},
        model_provider=profile.model_provider if profile else None,
        model_name=profile.model_name if profile else None,
        runtime=profile.runtime if profile else None,
        created_at=agent.created_at,
        last_seen_at=agent.last_seen_at,
    )


@router.post(
    "/register",
    response_model=AgentRegistrationResult,
    status_code=status.HTTP_201_CREATED,
)
def register_agent(
    payload: AgentRegister,
    db: Session = Depends(get_db),
) -> AgentRegistrationResult:
    api_key = generate_api_key()
    agent = Agent(
        name=payload.name,
        description=payload.description,
        capabilities=payload.capabilities,
        api_key_hash=hash_api_key(api_key),
    )
    db.add(agent)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Agent name already exists",
        ) from exc

    db.refresh(agent)
    return AgentRegistrationResult(agent=AgentProfile.model_validate(agent), api_key=api_key)


@router.get("/me", response_model=AgentProfile)
def get_my_identity(agent: Agent = Depends(get_current_agent)) -> AgentProfile:
    return AgentProfile.model_validate(agent)


@router.get("/me/profile", response_model=StructuredAgentProfile)
def get_my_structured_profile(
    agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> StructuredAgentProfile:
    profile = db.get(AgentStructuredProfile, agent.id)
    return _structured_profile(agent, profile)


@router.put("/me/profile", response_model=StructuredAgentProfile)
def update_my_structured_profile(
    payload: StructuredAgentProfileUpdate,
    agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> StructuredAgentProfile:
    profile = db.get(AgentStructuredProfile, agent.id)
    if profile is None:
        profile = AgentStructuredProfile(agent_id=agent.id, capabilities=[], profile_data={})
        db.add(profile)

    values = payload.model_dump(exclude_unset=True)
    if "description" in values:
        agent.description = values.pop("description")
    if "metadata" in values:
        profile.profile_data = values.pop("metadata") or {}
    if "capabilities" in values:
        values["capabilities"] = values["capabilities"] or []
    for field, value in values.items():
        setattr(profile, field, value)

    db.add(agent)
    db.commit()
    db.refresh(agent)
    db.refresh(profile)
    return _structured_profile(agent, profile)


@router.get("/{agent_name}/profile", response_model=StructuredAgentProfile)
def get_public_structured_profile(
    agent_name: str,
    db: Session = Depends(get_db),
) -> StructuredAgentProfile:
    agent = db.scalar(select(Agent).where(Agent.name == agent_name))
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    profile = db.get(AgentStructuredProfile, agent.id)
    return _structured_profile(agent, profile)
