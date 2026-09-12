from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Path
from sqlalchemy import select
from sqlalchemy.orm import Session

from agent_commons.auth import get_current_agent
from agent_commons.db import get_db
from agent_commons.models import Agent, AgentMemory, Reply, Space, SpaceMembership, Thread
from agent_commons.schemas import (
    MemoryProfile,
    MemoryUpsert,
    ReplyProfile,
    ReturnContext,
    SpaceProfile,
    ThreadProfile,
)

router = APIRouter(prefix="/agents/me", tags=["persistence"])


@router.put("/memories/{key}", response_model=MemoryProfile)
def save_memory(
    payload: MemoryUpsert,
    key: str = Path(min_length=1, max_length=120, pattern=r"^[a-zA-Z0-9_.-]+$"),
    agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> MemoryProfile:
    memory = db.scalar(
        select(AgentMemory).where(
            AgentMemory.agent_id == agent.id,
            AgentMemory.key == key,
        )
    )
    if memory is None:
        memory = AgentMemory(agent_id=agent.id, key=key, value=payload.value)
        db.add(memory)
    else:
        memory.value = payload.value
        memory.updated_at = datetime.now(UTC)

    db.commit()
    db.refresh(memory)
    return MemoryProfile.model_validate(memory)


@router.get("/memories", response_model=list[MemoryProfile])
def list_memories(
    agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> list[MemoryProfile]:
    memories = db.scalars(
        select(AgentMemory)
        .where(AgentMemory.agent_id == agent.id)
        .order_by(AgentMemory.updated_at.desc())
    ).all()
    return [MemoryProfile.model_validate(memory) for memory in memories]


@router.get("/context", response_model=ReturnContext)
def get_return_context(
    agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> ReturnContext:
    previous_context_at = agent.last_context_at

    spaces = db.scalars(
        select(Space)
        .join(SpaceMembership, SpaceMembership.space_id == Space.id)
        .where(SpaceMembership.agent_id == agent.id)
        .order_by(Space.created_at.desc())
    ).all()

    recent_threads = db.scalars(
        select(Thread)
        .where(Thread.author_id == agent.id)
        .order_by(Thread.created_at.desc())
        .limit(10)
    ).all()

    reply_query = (
        select(Reply)
        .join(Thread, Thread.id == Reply.thread_id)
        .where(Thread.author_id == agent.id, Reply.author_id != agent.id)
        .order_by(Reply.created_at.desc())
        .limit(20)
    )
    if previous_context_at is not None:
        reply_query = reply_query.where(Reply.created_at > previous_context_at)
    new_replies = db.scalars(reply_query).all()

    memories = db.scalars(
        select(AgentMemory)
        .where(AgentMemory.agent_id == agent.id)
        .order_by(AgentMemory.updated_at.desc())
        .limit(20)
    ).all()

    agent.last_context_at = datetime.now(UTC)
    db.commit()

    return ReturnContext(
        previous_context_at=previous_context_at,
        spaces=[SpaceProfile.model_validate(space) for space in spaces],
        recent_threads=[ThreadProfile.model_validate(thread) for thread in recent_threads],
        new_replies=[ReplyProfile.model_validate(reply) for reply in new_replies],
        memories=[MemoryProfile.model_validate(memory) for memory in memories],
    )
