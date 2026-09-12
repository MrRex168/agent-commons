import uuid

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from agent_commons.models import Agent, Space, SpaceMembership

PUBLIC = "public"
AGENTS_ONLY = "agents_only"
PRIVATE = "private"


def is_space_member(db: Session, space_id: uuid.UUID, agent_id: uuid.UUID) -> bool:
    return (
        db.scalar(
            select(SpaceMembership.id).where(
                SpaceMembership.space_id == space_id,
                SpaceMembership.agent_id == agent_id,
            )
        )
        is not None
    )


def can_read_space(db: Session, space: Space, agent: Agent | None) -> bool:
    if space.visibility == PUBLIC:
        return True
    if agent is None:
        return False
    if space.visibility == AGENTS_ONLY:
        return True
    return is_space_member(db, space.id, agent.id)


def require_space_read(db: Session, space: Space, agent: Agent | None) -> None:
    if can_read_space(db, space, agent):
        return
    if space.visibility == AGENTS_ONLY and agent is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Agent authentication required",
        )
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Space not found")


def require_space_owner(space: Space, agent: Agent) -> None:
    if space.created_by_id != agent.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the space creator can manage private membership",
        )
