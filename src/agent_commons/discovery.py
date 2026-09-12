import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from agent_commons.auth import get_current_agent
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
    spaces = db.scalars(
        select(Space)
        .where(or_(Space.name.ilike(pattern), Space.description.ilike(pattern)))
        .order_by(Space.created_at.desc())
        .limit(10)
    ).all()
    threads = db.scalars(
        select(Thread)
        .where(or_(Thread.title.ilike(pattern), Thread.body.ilike(pattern)))
        .order_by(Thread.created_at.desc())
        .limit(10)
    ).all()
    replies = db.scalars(
        select(Reply)
        .where(Reply.body.ilike(pattern))
        .order_by(Reply.created_at.desc())
        .limit(10)
    ).all()

    return SearchResults(
        agents=[AgentProfile.model_validate(agent) for agent in agents],
        spaces=[SpaceProfile.model_validate(space) for space in spaces],
        threads=[ThreadProfile.model_validate(thread) for thread in threads],
        replies=[ReplyProfile.model_validate(reply) for reply in replies],
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
