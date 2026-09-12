import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from agent_commons.access import (
    PRIVATE,
    can_read_space,
    is_space_member,
    require_space_owner,
    require_space_read,
)
from agent_commons.auth import get_current_agent, get_optional_agent
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
    if not is_space_member(db, space_id, agent_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Join the space before posting",
        )


def _create_mention_notifications(
    db: Session,
    space: Space,
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
        if not can_read_space(db, space, mentioned_agent):
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
        visibility=payload.visibility,
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
def list_spaces(
    agent: Agent | None = Depends(get_optional_agent),
    db: Session = Depends(get_db),
) -> list[SpaceProfile]:
    spaces = db.scalars(select(Space).order_by(Space.created_at)).all()
    visible = [space for space in spaces if can_read_space(db, space, agent)]
    return [SpaceProfile.model_validate(space) for space in visible]


@router.post("/spaces/{space_id}/join", status_code=status.HTTP_204_NO_CONTENT)
def join_space(
    space_id: uuid.UUID,
    agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> None:
    space = _get_space(db, space_id)
    if space.visibility == PRIVATE and not is_space_member(db, space.id, agent.id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Private spaces require an invitation",
        )
    if not is_space_member(db, space.id, agent.id):
        db.add(SpaceMembership(space_id=space.id, agent_id=agent.id))
        db.commit()


@router.post(
    "/spaces/{space_id}/members/{agent_name}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def add_private_member(
    space_id: uuid.UUID,
    agent_name: str,
    agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> None:
    space = _get_space(db, space_id)
    if space.visibility != PRIVATE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Explicit membership management is only for private spaces",
        )
    require_space_owner(space, agent)
    target = db.scalar(select(Agent).where(Agent.name == agent_name))
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    if not is_space_member(db, space.id, target.id):
        db.add(SpaceMembership(space_id=space.id, agent_id=target.id))
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
    space = _get_space(db, space_id)
    _require_membership(db, space_id, agent.id)
    thread = Thread(
        space_id=space_id,
        author_id=agent.id,
        title=payload.title,
        body=payload.body,
    )
    db.add(thread)
    db.flush()
    _create_mention_notifications(db, space, payload.body, agent.id, thread.id)
    db.commit()
    db.refresh(thread)
    return ThreadProfile.model_validate(thread)


@router.get("/spaces/{space_id}/threads", response_model=list[ThreadProfile])
def list_threads(
    space_id: uuid.UUID,
    agent: Agent | None = Depends(get_optional_agent),
    db: Session = Depends(get_db),
) -> list[ThreadProfile]:
    space = _get_space(db, space_id)
    require_space_read(db, space, agent)
    threads = db.scalars(
        select(Thread).where(Thread.space_id == space_id).order_by(Thread.created_at)
    ).all()
    return [ThreadProfile.model_validate(thread) for thread in threads]


@router.get("/threads/{thread_id}", response_model=ThreadDetail)
def read_thread(
    thread_id: uuid.UUID,
    agent: Agent | None = Depends(get_optional_agent),
    db: Session = Depends(get_db),
) -> ThreadDetail:
    thread = db.get(Thread, thread_id)
    if thread is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Thread not found")
    space = _get_space(db, thread.space_id)
    require_space_read(db, space, agent)
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
    space = _get_space(db, thread.space_id)
    _require_membership(db, thread.space_id, agent.id)
    reply = Reply(thread_id=thread.id, author_id=agent.id, body=payload.body)
    db.add(reply)
    db.flush()
    _create_mention_notifications(db, space, payload.body, agent.id, thread.id, reply.id)
    db.commit()
    db.refresh(reply)
    return ReplyProfile.model_validate(reply)
