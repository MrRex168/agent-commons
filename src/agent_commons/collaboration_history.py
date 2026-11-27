"""Owner-scoped metadata observations kept on the caller's home instance."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete, func, select, text, tuple_
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from agent_commons.auth import get_current_agent
from agent_commons.collaboration_history_models import CollaborationHistory
from agent_commons.db import get_db
from agent_commons.models import Agent, AgentCryptographicIdentity
from agent_commons.remote_models import RemoteAgentReference
from agent_commons.typed_client_models import (
    TERMINAL_STATES,
    CollaborationObservation,
    CollaborationPage,
    CollaborationRecord,
)

router = APIRouter(prefix="/agents/collaboration", tags=["collaboration-history"])
RETENTION = timedelta(days=30)
OWNER_CAP = 512
INSTANCE_CAP = 8192


@router.post("/history", response_model=CollaborationRecord)
def observe(
    observation: CollaborationObservation,
    owner: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
):
    task = observation.task
    try:
        db.execute(text("SET LOCAL statement_timeout = '750ms'"))
        db.execute(text("SET LOCAL lock_timeout = '250ms'"))
        db.execute(text("LOCK TABLE collaboration_history IN SHARE ROW EXCLUSIVE MODE"))
        identity = db.get(AgentCryptographicIdentity, owner.id)
        if identity is None or identity.fingerprint != observation.caller_root:
            raise HTTPException(403, "Collaboration observation is not authorized")
        reference = db.get(RemoteAgentReference, observation.reference_id, with_for_update=True)
        now = datetime.now(UTC)
        if (
            reference is None
            or reference.endpoint_status(now) != "authorized"
            or reference.root_fingerprint != task.peer_root
            or reference.a2a_url != task.endpoint
            or reference.identity_sequence != task.identity_sequence
            or reference.location_sequence != task.location_sequence
            or not reference.location_digest
        ):
            raise HTTPException(409, "Collaboration endpoint has changed or expired")
        if observation.artifact_digest is not None and observation.status != "completed":
            raise HTTPException(422, "Artifact digest requires a completed observation")
        url = httpx.URL(reference.a2a_url)
        origin = str(url.copy_with(path="/", query=None, fragment=None)).rstrip("/")
        db.execute(delete(CollaborationHistory).where(CollaborationHistory.expires_at <= now))
        row = db.scalar(
            select(CollaborationHistory).where(
                CollaborationHistory.owner_id == owner.id,
                CollaborationHistory.execution_origin == origin,
                CollaborationHistory.task_id == task.id,
            )
        )
        if row is None:
            if (
                db.scalar(select(func.count()).select_from(CollaborationHistory)) >= INSTANCE_CAP
                or db.scalar(
                    select(func.count())
                    .select_from(CollaborationHistory)
                    .where(CollaborationHistory.owner_id == owner.id)
                )
                >= OWNER_CAP
            ):
                raise HTTPException(429, "Collaboration history is at capacity")
            row = CollaborationHistory(
                id=uuid4(),
                owner_id=owner.id,
                caller_root=identity.fingerprint,
                peer_root=task.peer_root,
                execution_origin=origin,
                endpoint=task.endpoint,
                task_id=task.id,
                skill=task.skill,
                status=observation.status,
                submitted_at=now,
                expires_at=now + RETENTION,
                identity_sequence=task.identity_sequence,
                location_sequence=task.location_sequence,
                location_digest=reference.location_digest,
                verified_at=reference.resolved_at,
            )
            db.add(row)
        elif (
            row.peer_root != task.peer_root
            or row.skill != task.skill
            or row.endpoint != task.endpoint
            or row.identity_sequence != task.identity_sequence
            or row.location_sequence != task.location_sequence
            or row.location_digest != reference.location_digest
        ):
            raise HTTPException(409, "Collaboration observation conflicts with task provenance")
        if row.completed_at is None and not (
            row.status == "working" and observation.status == "submitted"
        ):
            row.status = observation.status
            if row.status in TERMINAL_STATES:
                row.completed_at = now
                row.artifact_digest = observation.artifact_digest
        db.commit()
        return row
    except SQLAlchemyError:
        raise HTTPException(503, "Collaboration history is unavailable") from None


@router.get("/history", response_model=CollaborationPage)
def history(
    cursor: UUID | None = None,
    limit: int = Query(default=25, ge=1, le=25),
    owner: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
):
    model = CollaborationHistory
    rows = select(model).where(model.owner_id == owner.id, model.expires_at > func.now())
    try:
        db.execute(text("SET LOCAL statement_timeout = '750ms'"))
        if cursor is not None:
            boundary = db.scalar(rows.where(model.id == cursor))
            if boundary is None:
                raise HTTPException(404, "History cursor not found")
            rows = rows.where(
                tuple_(model.submitted_at, model.id) < (boundary.submitted_at, boundary.id)
            )
        records = db.scalars(
            rows.order_by(model.submitted_at.desc(), model.id.desc()).limit(limit + 1)
        ).all()
        return CollaborationPage(
            records=[CollaborationRecord.model_validate(row) for row in records[:limit]],
            next_cursor=records[limit - 1].id if len(records) > limit else None,
        )
    except SQLAlchemyError:
        raise HTTPException(503, "Collaboration history is unavailable") from None
