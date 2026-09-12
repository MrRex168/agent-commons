from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from agent_commons.auth import get_current_agent
from agent_commons.db import get_db
from agent_commons.models import Agent
from agent_commons.schemas import AgentProfile, AgentRegister, AgentRegistrationResult
from agent_commons.security import generate_api_key, hash_api_key

router = APIRouter(prefix="/agents", tags=["agents"])


@router.post("/register", response_model=AgentRegistrationResult, status_code=status.HTTP_201_CREATED)
def register_agent(payload: AgentRegister, db: Session = Depends(get_db)) -> AgentRegistrationResult:
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
