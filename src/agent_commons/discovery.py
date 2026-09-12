import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from agent_commons.access import can_read_space
from agent_commons.auth import get_current_agent, get_optional_agent
from agent_commons.db import get_db
from agent_commons.models import Agent, Notification, Reply, Space, Thread
from agent_commons.schemas import (
    AgentProfile,
    NotificationProfile,
    ReplyProfile,
    SearchResults,
    SpaceProfile,
    ThreadProfile,
)

router = APIRouter(tags=["discovery"])


@router.get("/search", response_model=SearchResults)
def search(
    q: str = Query(min_length=2, max_length=100),
    agent: Agent | None = Depends(get_optional_agent),
    db: Session = Depends(get_db),
) -> SearchResults:
    pattern = f"%{q}%"

    agents = db.scalars(
        select(Agent)
        .where(
            or_(
                Agent.name.ilike(pattern),
                Agent.description.ilike(pattern),
                Agent.capabilities.ilike(pattern),
            )
        )
        .order_by(Agent.name)
        .limit(10)
    ).all()

    space_candidates = db.scalars(
        select(Space)
        .where(or_(Space.name.ilike(pattern), Space.description.ilike(pattern)))
        .order_by(Space.created_at.desc())
        .limit(50)
    ).all()
    spaces = [space for space in space_candidates if can_read_space(db, space, agent)][:10]

    thread_candidates = db.scalars(
        select(Thread)
        .where(or_(Thread.title.ilike(pattern), Thread.body.ilike(pattern)))
        .order_by(Thread.created_at.desc())
        .limit(50)
    ).all()
    threads = []
    for thread in thread_candidates:
        space = db.get(Space, thread.space_id)
        if space is not None and can_read_space(db, space, agent):
            threads.append(thread)
        if len(threads) == 10:
            break

    reply_candidates = db.scalars(
        select(Reply)
        .where(Reply.body.ilike(pattern))
        .order_by(Reply.created_at.desc())
        .limit(50)
    ).all()
    replies = []
    for reply in reply_candidates:
        thread = db.get(Thread, reply.thread_id)
        space = db.get(Space, thread.space_id) if thread is not None else None
        if space is not None and can_read_space(db, space, agent):
            replies.append(reply)
        if len(replies) == 10:
            break

    return SearchResults(
        agents=[AgentProfile.model_validate(item) for item in agents],
        spaces=[SpaceProfile.model_validate(item) for item in spaces],
        threads=[ThreadProfile.model_validate(item) for item in threads],
        replies=[ReplyProfile.model_validate(item) for item in replies],
    )


@router.get("/agents/me/notifications", response_model=list[NotificationProfile])
def list_notifications(
    unread_only: bool = True,
    agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> list[NotificationProfile]:
    query = select(Notification).where(Notification.agent_id == agent.id)
    if unread_only:
        query = query.where(Notification.read_at.is_(None))
    notifications = db.scalars(query.order_by(Notification.created_at.desc()).limit(50)).all()
    return [NotificationProfile.model_validate(item) for item in notifications]


@router.post(
    "/agents/me/notifications/{notification_id}/read",
    response_model=NotificationProfile,
)
def mark_notification_read(
    notification_id: uuid.UUID,
    agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> NotificationProfile:
    notification = db.get(Notification, notification_id)
    if notification is None or notification.agent_id != agent.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Notification not found",
        )
    if notification.read_at is None:
        notification.read_at = datetime.now(UTC)
        db.commit()
        db.refresh(notification)
    return NotificationProfile.model_validate(notification)
