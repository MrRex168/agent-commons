"""Transactional task storage for opt-in typed A2A execution."""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from fastapi import HTTPException
from pydantic import Field, ValidationError
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from agent_commons.a2a_task_models import A2ATask
from agent_commons.a2a_tasks import (
    MAX_RETAINED_TASKS,
    TASK_RETENTION,
    GetParams,
    RPCError,
    RPCRequest,
    SendConfiguration,
    StrictModel,
)
from agent_commons.collaboration import (
    BUILTIN_EXECUTABLE_SKILLS,
    validate_typed_output,
    validate_typed_request,
)
from agent_commons.config import settings
from agent_commons.db import SessionLocal
from agent_commons.peer_models import PeerApproval, PeerGrant
from agent_commons.preview_limits import consume, consume_grant

MAX_ACTIVE = 32
MAX_WORKING = 4
CLAIM_LOCK = 0x414332
ACTIVE = ("submitted", "working")
TOKEN_PATTERN = re.compile(r"a2c_[A-Za-z0-9_-]{43}")


class DataPart(StrictModel):
    kind: Literal["data"]
    data: dict


class TypedMessage(StrictModel):
    kind: Literal["message"]
    role: Literal["user"]
    messageId: str = Field(pattern=r"^[A-Za-z0-9._:-]{1,128}$")
    parts: list[DataPart] = Field(min_length=1, max_length=1)


class TypedSend(StrictModel):
    message: TypedMessage
    configuration: SendConfiguration | None = None


def _limits(db: Session) -> None:
    db.execute(text("SET LOCAL statement_timeout = '2s'"))
    db.execute(text("SET LOCAL lock_timeout = '1s'"))


def _clock(db: Session) -> datetime:
    return db.scalar(select(func.clock_timestamp()))


def _grant(db: Session, condition) -> tuple[PeerGrant, PeerApproval] | None:
    # Lock the approval before locking the grant, matching revoke/reapprove.
    approval = db.scalar(
        select(PeerApproval)
        .join(PeerGrant, PeerGrant.approval_id == PeerApproval.id)
        .where(condition, PeerApproval.audience == settings.api_url)
        .with_for_update(of=PeerApproval)
    )
    if approval is None or not approval.skill_scopes:
        return None
    grant = db.scalar(
        select(PeerGrant)
        .where(
            condition,
            PeerGrant.approval_id == approval.id,
            PeerGrant.revoked_at.is_(None),
            PeerGrant.expires_at > func.clock_timestamp(),
            PeerGrant.skill_scopes == approval.skill_scopes,
        )
        .with_for_update()
    )
    return (grant, approval) if grant is not None else None


def _enabled(approval: PeerApproval, key: str) -> bool:
    return (
        key in BUILTIN_EXECUTABLE_SKILLS
        and key in (approval.skill_scopes or ())
        and key in settings.collaboration_skills.get(approval.recipient_agent_id, ())
    )


def _task_result(task: A2ATask, state: str, artifact: dict | None = None) -> dict:
    result = {
        "kind": "task",
        "id": str(task.id),
        "contextId": str(task.id),
        "status": {"state": state},
    }
    if artifact is not None:
        result["artifacts"] = [
            {
                "artifactId": str(task.id),
                "name": task.skill,
                "parts": [{"kind": "data", "data": artifact}],
            }
        ]
    return result


def _terminal(task: A2ATask, state: str, artifact: dict | None = None) -> None:
    task.state = state
    task.result = _task_result(task, state, artifact)
    task.task_input = None
    task.attempt = None


def _reserve(db: Session, grant: PeerGrant, approval: PeerApproval, raw: object) -> dict:
    params = TypedSend.model_validate(raw)
    configuration = params.configuration
    if configuration and (
        configuration.blocking is True
        or (
            configuration.acceptedOutputModes is not None
            and "application/json" not in configuration.acceptedOutputModes
        )
    ):
        raise RPCError(-32602, "Typed tasks use asynchronous JSON output")
    data = params.message.parts[0].data
    if set(data) != {"skill", "deadline", "input"}:
        raise RPCError(-32602, "Invalid typed task")
    now = _clock(db)
    command = json.dumps(
        {**data, "message_id": params.message.messageId}, ensure_ascii=False, separators=(",", ":")
    ).encode()
    # Replays must be comparable after their deadline. Fresh admission checks
    # the real clock below, after the existing reservation has been found.
    request, digest = validate_typed_request(command, now=now, check_deadline=False)
    if not _enabled(approval, request.skill):
        raise HTTPException(403, "Typed task is not authorized")
    db.execute(delete(A2ATask).where(A2ATask.expires_at <= now))
    existing = db.scalar(
        select(A2ATask).where(
            A2ATask.caller_root == approval.caller_root,
            A2ATask.recipient_agent_id == approval.recipient_agent_id,
            A2ATask.message_id == request.message_id,
        )
    )
    if existing is not None:
        if existing.skill != request.skill or existing.request_digest != digest:
            raise RPCError(-32010, "messageId was used for different task content")
        return existing.result
    # Use the same bounded validation for the initial reservation and the worker.
    validate_typed_request(command, now=now)
    if request.deadline > grant.expires_at:
        raise RPCError(-32602, "Deadline exceeds grant validity")
    if (
        db.scalar(select(func.count()).select_from(A2ATask)) >= MAX_RETAINED_TASKS
        or db.scalar(select(func.count()).select_from(A2ATask).where(A2ATask.state.in_(ACTIVE)))
        >= MAX_ACTIVE
    ):
        raise RPCError(-32011, "A2A task storage is at capacity")
    task = A2ATask(
        id=uuid4(),
        caller_root=approval.caller_root,
        recipient_agent_id=approval.recipient_agent_id,
        message_id=request.message_id,
        request_digest=digest,
        expires_at=now + TASK_RETENTION,
        skill=request.skill,
        state="submitted",
        task_input=request.input,
        deadline=request.deadline,
        grant_id=grant.id,
    )
    task.result = _task_result(task, "submitted")
    db.add(task)
    return task.result


def _read_or_cancel(db: Session, approval: PeerApproval, rpc: RPCRequest) -> dict:
    task_id = UUID(GetParams.model_validate(rpc.params).id)
    query = select(A2ATask).where(
        A2ATask.id == task_id,
        A2ATask.caller_root == approval.caller_root,
        A2ATask.recipient_agent_id == approval.recipient_agent_id,
        A2ATask.expires_at > func.clock_timestamp(),
        A2ATask.skill.in_(approval.skill_scopes),
    )
    task = db.scalar(query.with_for_update() if rpc.method == "tasks/cancel" else query)
    if task is None:
        raise RPCError(-32001, "Task not found")
    if rpc.method == "tasks/cancel":
        if task.state not in ACTIVE:
            raise RPCError(-32002, "Task cannot be canceled")
        _terminal(task, "canceled")
    return task.result


def charge_invalid(token: str) -> None:
    try:
        with SessionLocal.begin() as db:
            _limits(db)
            matched = _grant(
                db, PeerGrant.token_digest == hashlib.sha256(token.encode()).hexdigest()
            )
            if matched is None:
                raise HTTPException(401, "Invalid A2A peer credential")
            consume_grant(
                matched[0].id,
                settings.preview_task_rate_per_grant,
                settings.preview_task_burst_per_grant,
                db=db,
            )

    except SQLAlchemyError:
        raise HTTPException(503, "A2A task storage is unavailable") from None


def dispatch(token: str, rpc: RPCRequest) -> dict:
    failure = None
    try:
        with SessionLocal.begin() as db:
            _limits(db)
            # Reuse the legacy storage lock, preserving its shared row cap
            # and unique ownership/message namespace across both paths.
            if rpc.method in ("message/send", "tasks/cancel"):
                db.execute(text("LOCK TABLE a2a_tasks IN SHARE ROW EXCLUSIVE MODE"))
            matched = _grant(
                db, PeerGrant.token_digest == hashlib.sha256(token.encode()).hexdigest()
            )
            if matched is None:
                raise HTTPException(401, "Invalid A2A peer credential")
            grant, approval = matched
            consume_grant(
                grant.id,
                settings.preview_task_rate_per_grant,
                settings.preview_task_burst_per_grant,
                db=db,
            )
            try:
                if rpc.method not in ("message/send", "tasks/get", "tasks/cancel"):
                    raise RPCError(-32601, "Method not found")
                if rpc.method not in approval.scopes:
                    raise HTTPException(403, "Credential is not authorized for this A2A method")
                consume("tasks", settings.preview_tasks_per_second, 1, db=db)
                if rpc.method == "message/send":
                    result = _reserve(db, grant, approval, rpc.params)
                else:
                    result = _read_or_cancel(db, approval, rpc)
            except (RPCError, HTTPException, ValueError) as exc:
                # Charge authenticated invalid attempts. Database errors still
                # roll back all admission state, including quota charges.
                if isinstance(exc, HTTPException) and exc.status_code == 503:
                    raise
                failure = exc
    except SQLAlchemyError:
        raise HTTPException(503, "A2A task storage is unavailable") from None
    if isinstance(failure, (ValidationError, ValueError)):
        raise RPCError(-32602, "Invalid typed task parameters") from None
    if failure is not None:
        raise failure
    return result


@dataclass(frozen=True)
class Claim:
    id: UUID
    attempt: UUID
    skill: str
    data: dict
    deadline: datetime


def claim() -> Claim | None:
    with SessionLocal.begin() as db:
        _limits(db)
        db.execute(text("LOCK TABLE a2a_tasks IN ROW EXCLUSIVE MODE"))
        if not db.scalar(select(func.pg_try_advisory_xact_lock(CLAIM_LOCK))):
            return None
        if (
            db.scalar(select(func.count()).select_from(A2ATask).where(A2ATask.state == "working"))
            >= MAX_WORKING
        ):
            return None
        eligible = (A2ATask.state == "submitted", A2ATask.deadline > func.clock_timestamp())
        candidate = db.execute(
            select(A2ATask.id, A2ATask.grant_id)
            .where(*eligible)
            .order_by(A2ATask.deadline, A2ATask.id)
            .limit(1)
        ).one_or_none()
        if candidate is None:
            return None
        matched = _grant(db, PeerGrant.id == candidate.grant_id)
        task = db.scalar(
            select(A2ATask)
            .where(A2ATask.id == candidate.id, *eligible)
            .with_for_update(skip_locked=True)
        )
        if task is None:
            return None
        if matched is None:
            _terminal(task, "rejected")
            return None
        if not _authorized_start(db, task, matched):
            return None
        task.attempt = uuid4()
        task.state = "working"
        task.result = _task_result(task, "working")
        return Claim(task.id, task.attempt, task.skill, task.task_input, task.deadline)


def _authorized_start(db: Session, task: A2ATask, matched) -> bool:
    if task.deadline <= _clock(db):
        _terminal(task, "failed")
        return False
    if matched is not None:
        grant, approval = matched
        if (
            _enabled(approval, task.skill)
            and task.caller_root == approval.caller_root
            and task.recipient_agent_id == approval.recipient_agent_id
            and "message/send" in approval.scopes
            and task.deadline <= grant.expires_at
        ):
            return True
    _terminal(task, "rejected")
    return False


def _working(claimed: Claim):
    return (
        A2ATask.id == claimed.id,
        A2ATask.attempt == claimed.attempt,
        A2ATask.state == "working",
    )


def authorize_start(claimed: Claim) -> bool:
    with SessionLocal.begin() as db:
        _limits(db)
        db.execute(text("LOCK TABLE a2a_tasks IN ROW EXCLUSIVE MODE"))
        grant_id = db.scalar(select(A2ATask.grant_id).where(*_working(claimed)))
        if grant_id is None:
            return False
        matched = _grant(db, PeerGrant.id == grant_id)
        task = db.scalar(select(A2ATask).where(*_working(claimed)).with_for_update())
        return task is not None and _authorized_start(db, task, matched)


def current(claimed: Claim) -> bool:
    with SessionLocal.begin() as db:
        _limits(db)
        return db.scalar(select(A2ATask.id).where(*_working(claimed))) is not None


def finish(claimed: Claim, artifact: dict | None) -> bool:
    with SessionLocal.begin() as db:
        _limits(db)
        db.execute(text("LOCK TABLE a2a_tasks IN ROW EXCLUSIVE MODE"))
        task = db.scalar(select(A2ATask).where(*_working(claimed)).with_for_update())
        if task is None:
            return False
        if artifact is not None:
            try:
                artifact = validate_typed_output(task.skill, artifact)
            except (ValueError, TypeError):
                artifact = None
        if artifact is None or task.deadline <= _clock(db):
            _terminal(task, "failed")
        else:
            _terminal(task, "completed", artifact)
        return True


def sweep() -> None:
    with SessionLocal.begin() as db:
        _limits(db)
        db.execute(text("LOCK TABLE a2a_tasks IN ROW EXCLUSIVE MODE"))
        # Bulk update uses the original task ID for its stable context.
        expired = db.scalars(
            select(A2ATask)
            .where(A2ATask.state.in_(ACTIVE), A2ATask.deadline <= func.clock_timestamp())
            .with_for_update(skip_locked=True)
        ).all()
        for task in expired:
            _terminal(task, "failed")
        db.flush()
        db.execute(
            delete(A2ATask).where(
                A2ATask.skill.is_not(None), A2ATask.expires_at <= func.clock_timestamp()
            )
        )
