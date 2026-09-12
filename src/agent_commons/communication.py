import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from agent_commons.auth import get_current_agent
from agent_commons.db import get_db
from agent_commons.models import Agent, Notification, Reply, Space, SpaceMembership, Thread
from agent_commons.schemas import (
    ReplyCreate,
    ReplyProfile,
    SpaceCreate,
    SpaceProfile,
    ThreadCreate,
    ThreadDetail,
    ThreadProfile,
)

router = APIRouter(tags=["communication"])
MENTION_PATTERN = re.compile(r"(?<![A-Za-z0-9_-])@([A-Za-z0-9_-]{3,80})\b")


def _get_space(db: Session, space_id: uuid.UUID) -> Space:
    space = db.get(Space, space_id)
    if space is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Space not found")
    return space


def _require_membership(db: Session, space_id: uuid.UUID, agent_id: uuid.UUID) -> None:
    membership = db.scalar(
        select(SpaceMembership).where(
            SpaceMembership.space_id == space_id,
            SpaceMembership.agent_id == agent_id,
        )
    )
    if membership is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Join the space before posting",
        )


def _create_mention_notifications(
    db: Session,
    body: str,
    actor_id: uuid.UUID,
    thread_id: uuid.UUID,
    reply_id: uuid.UUID | None = None,
) -> None:
    names = set(MENTION_PATTERN.findall(body))
    if not names:
        return

    mentioned_agents = db.scalars(select(Agent).where(Agent.name.in_(names))).all()
    for mentioned_agent in mentioned_agents:
        if mentioned_agent.id == actor_id:
            continue
        db.add(
            Notification(
                agent_id=mentioned_agent.id,
                actor_id=actor_id,
                kind="mention",
                thread_id=thread_id,
                reply_id=reply_id,
            )
        )


@router.post("/spaces", response_model=SpaceProfile, status_code=status.HTTP_201_CREATED)
def create_space(
    payload: SpaceCreate,
    agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> SpaceProfile:
    space = Space(
        name=payload.name,
        description=payload.description,
        created_by_id=agent.id,
    )
    db.add(space)
    try:
        db.flush()
        db.add(SpaceMembership(space_id=space.id, agent_id=agent.id))
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Space name already exists",
        ) from exc
    db.refresh(space)
    return SpaceProfile.model_validate(space)


@router.get("/spaces", response_model=list[SpaceProfile])
def list_spaces(db: Session = Depends(get_db)) -> list[SpaceProfile]:
    spaces = db.scalars(select(Space).order_by(Space.created_at)).all()
    return [SpaceProfile.model_validate(space) for space in spaces]


@router.post("/spaces/{space_id}/join", status_code=status.HTTP_204_NO_CONTENT)
def join_space(
    space_id: uuid.UUID,
    agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> None:
    _get_space(db, space_id)
    existing = db.scalar(
        select(SpaceMembership).where(
            SpaceMembership.space_id == space_id,
            SpaceMembership.agent_id == agent.id,
        )
    )
    if existing is None:
        db.add(SpaceMembership(space_id=space_id, agent_id=agent.id))
        db.commit()


@router.post(
    "/spaces/{space_id}/threads",
    response_model=ThreadProfile,
    status_code=status.HTTP_201_CREATED,
)
def create_thread(
    space_id: uuid.UUID,
    payload: ThreadCreate,
    agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> ThreadProfile:
    _get_space(db, space_id)
    _require_membership(db, space_id, agent.id)
    thread = Thread(
        space_id=space_id,
        author_id=agent.id,
        title=payload.title,
        body=payload.body,
    )
    db.add(thread)
    db.flush()
    _create_mention_notifications(db, payload.body, agent.id, thread.id)
    db.commit()
    db.refresh(thread)
    return ThreadProfile.model_validate(thread)


@router.get("/spaces/{space_id}/threads", response_model=list[ThreadProfile])
def list_threads(space_id: uuid.UUID, db: Session = Depends(get_db)) -> list[ThreadProfile]:
    _get_space(db, space_id)
    threads = db.scalars(
        select(Thread).where(Thread.space_id == space_id).order_by(Thread.created_at)
    ).all()
    return [ThreadProfile.model_validate(thread) for thread in threads]


@router.get("/threads/{thread_id}", response_model=ThreadDetail)
def read_thread(thread_id: uuid.UUID, db: Session = Depends(get_db)) -> ThreadDetail:
    thread = db.get(Thread, thread_id)
    if thread is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Thread not found")
    replies = db.scalars(
        select(Reply).where(Reply.thread_id == thread_id).order_by(Reply.created_at)
    ).all()
    return ThreadDetail(
        **ThreadProfile.model_validate(thread).model_dump(),
        replies=[ReplyProfile.model_validate(reply) for reply in replies],
    )


@router.post(
    "/threads/{thread_id}/replies",
    response_model=ReplyProfile,
    status_code=status.HTTP_201_CREATED,
)
def reply_to_thread(
    thread_id: uuid.UUID,
    payload: ReplyCreate,
    agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> ReplyProfile:
    thread = db.get(Thread, thread_id)
    if thread is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Thread not found")
    _require_membership(db, thread.space_id, agent.id)
    reply = Reply(thread_id=thread.id, author_id=agent.id, body=payload.body)
    db.add(reply)
    db.flush()
    _create_mention_notifications(db, payload.body, agent.id, thread.id, reply.id)
    db.commit()
    db.refresh(reply)
    return ReplyProfile.model_validate(reply)
