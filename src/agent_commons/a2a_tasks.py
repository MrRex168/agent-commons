"""Opt-in A2A 0.3 JSON-RPC subset for the Text Statistics skill."""

import asyncio
import hashlib
import json
import re
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from threading import BoundedSemaphore
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import DBAPIError
from starlette.requests import ClientDisconnect

from agent_commons.a2a_task_models import A2ATask
from agent_commons.config import A2ATaskPeer, settings
from agent_commons.db import SessionLocal
from agent_commons.models import Agent
from agent_commons.peer_enrollment import _authenticate_grant, authenticate_grant, require_audience
from agent_commons.preview_limits import consume, consume_grant

router = APIRouter(tags=["a2a-tasks"])
MAX_REQUEST_BYTES = 16_384
MAX_TEXT_BYTES = 4_096
MAX_RETAINED_TASKS = 1_024
TASK_RETENTION = timedelta(hours=24)
BODY_TIMEOUT_SECONDS = 3
REQUEST_SLOTS = BoundedSemaphore(8)
PREVIEW_AUTH_SLOTS = BoundedSemaphore(8)
TOKEN_PATTERN = re.compile(r"a2a_[A-Za-z0-9_-]{43}")


class RPCError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class RPCRequest(StrictModel):
    jsonrpc: Literal["2.0"]
    id: Annotated[str, Field(max_length=128)] | int
    method: str = Field(min_length=1, max_length=128)
    params: object = None


class TextPart(StrictModel):
    kind: Literal["text"]
    text: str = Field(max_length=MAX_TEXT_BYTES)


class Message(StrictModel):
    kind: Literal["message"]
    role: Literal["user"]
    messageId: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    parts: list[TextPart] = Field(min_length=1, max_length=1)


class SendConfiguration(StrictModel):
    acceptedOutputModes: list[str] | None = None
    blocking: bool | None = None
    historyLength: int | None = Field(default=None, ge=0)


class SendParams(StrictModel):
    message: Message
    configuration: SendConfiguration | None = None


class GetParams(StrictModel):
    id: str = Field(min_length=1, max_length=36)
    historyLength: int | None = Field(default=None, ge=0)


async def _authenticate(request: Request) -> A2ATaskPeer:
    values = request.headers.getlist("authorization")
    if len(values) == 1:
        scheme, _, token = values[0].partition(" ")
        if scheme.lower() == "bearer" and TOKEN_PATTERN.fullmatch(token):
            token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
            if settings.a2a_database_grants:
                require_audience(request)
                peer = await run_in_threadpool(authenticate_grant, token_hash)
                if peer is not None:
                    return peer
            else:
                for peer in settings.a2a_task_peers:
                    if secrets.compare_digest(peer.token_sha256, token_hash):
                        if peer.expires_at > datetime.now(UTC):
                            return peer
                        break
    raise HTTPException(401, "Invalid A2A peer credential", headers={"WWW-Authenticate": "Bearer"})


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("Invalid JSON number")


async def _read_json(request: Request, *, native: bool = False) -> object:
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type != "application/json" and not (native and media_type == "application/a2a+json"):
        raise HTTPException(415, "Expected application/json")
    if request.headers.get("content-encoding", "identity") != "identity":
        raise HTTPException(415, "Encoded request bodies are unsupported")
    length = request.headers.get("content-length")
    if length is not None:
        if not length.isascii() or not length.isdecimal():
            raise HTTPException(400, "Invalid Content-Length")
        if len(length) > 16 or int(length) > MAX_REQUEST_BYTES:
            raise HTTPException(413, "A2A request body is too large")
    body = bytearray()
    try:
        async with asyncio.timeout(BODY_TIMEOUT_SECONDS):
            async for chunk in request.stream():
                if len(body) + len(chunk) > MAX_REQUEST_BYTES:
                    raise HTTPException(413, "A2A request body is too large")
                body.extend(chunk)
    except TimeoutError as exc:
        raise HTTPException(408, "A2A request body timed out") from exc
    except ClientDisconnect as exc:
        raise HTTPException(400, "Incomplete A2A request body") from exc

    try:
        return json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_float=Decimal if native else None,
        )
    except (ValueError, RecursionError, InvalidOperation) as exc:
        raise RPCError(-32700, "Parse error") from exc


def _completed_task(task_id: UUID, content: str, byte_count: int) -> dict:
    return {
        "kind": "task",
        "id": str(task_id),
        "contextId": str(uuid4()),
        "status": {"state": "completed"},
        "artifacts": [
            {
                "artifactId": str(uuid4()),
                "name": "text-statistics",
                "parts": [
                    {"kind": "text", "text": f"bytes: {byte_count}\nwords: {len(content.split())}"}
                ],
            }
        ],
    }


def _send_task(peer: A2ATaskPeer, params: SendParams) -> dict:
    output_modes = params.configuration.acceptedOutputModes if params.configuration else None
    if output_modes is not None and "text/plain" not in output_modes:
        raise RPCError(-32005, "The skill requires text/plain output")
    content = params.message.parts[0].text
    try:
        encoded = content.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise RPCError(-32602, "Invalid UTF-8 text") from exc
    if len(encoded) > MAX_TEXT_BYTES:
        raise RPCError(-32602, "Task text exceeds the UTF-8 byte limit")
    digest = hashlib.sha256(encoded).hexdigest()

    with SessionLocal.begin() as db:
        db.execute(text("SET LOCAL statement_timeout = '2s'"))
        db.execute(text("SET LOCAL lock_timeout = '1s'"))
        # Serialize submissions so pruning, idempotency, and the global row cap
        # are decided atomically across all workers. Reads can proceed concurrently.
        db.execute(text("LOCK TABLE a2a_tasks IN SHARE ROW EXCLUSIVE MODE"))
        now = datetime.now(UTC)
        if peer.expires_at <= now or db.get(Agent, peer.recipient_agent_id) is None:
            raise HTTPException(403, "A2A recipient or credential is unavailable")
        db.execute(delete(A2ATask).where(A2ATask.expires_at <= now))
        existing = db.scalar(
            select(A2ATask).where(
                A2ATask.caller_root == peer.caller_root,
                A2ATask.recipient_agent_id == peer.recipient_agent_id,
                A2ATask.message_id == params.message.messageId,
            )
        )
        if existing:
            if existing.skill is not None or existing.request_digest != digest:
                raise RPCError(-32010, "messageId was used for different task content")
            return existing.result
        if db.scalar(select(func.count()).select_from(A2ATask)) >= MAX_RETAINED_TASKS:
            raise RPCError(-32011, "A2A task storage is at capacity")
        task_id = uuid4()
        result = _completed_task(task_id, content, len(encoded))
        db.add(
            A2ATask(
                id=task_id,
                caller_root=peer.caller_root,
                recipient_agent_id=peer.recipient_agent_id,
                message_id=params.message.messageId,
                request_digest=digest,
                result=result,
                expires_at=now + TASK_RETENTION,
            )
        )
        return result


def _get_task(peer: A2ATaskPeer, params: GetParams) -> dict:
    try:
        task_id = UUID(params.id)
    except ValueError as exc:
        raise RPCError(-32602, "Invalid task ID") from exc
    with SessionLocal.begin() as db:
        db.execute(text("SET LOCAL statement_timeout = '2s'"))
        now = datetime.now(UTC)
        if peer.expires_at <= now:
            raise HTTPException(403, "A2A credential is unavailable")
        result = db.scalar(
            select(A2ATask.result).where(
                A2ATask.id == task_id,
                A2ATask.skill.is_(None),
                A2ATask.caller_root == peer.caller_root,
                A2ATask.recipient_agent_id == peer.recipient_agent_id,
                A2ATask.expires_at > now,
            )
        )
        if result is None:
            raise RPCError(-32001, "Task not found")
        return result


def _dispatch(peer: A2ATaskPeer, rpc: RPCRequest) -> dict:
    if rpc.method not in ("message/send", "tasks/get"):
        raise RPCError(-32601, "Method not found")
    if rpc.method not in peer.scopes:
        raise HTTPException(403, "Credential is not authorized for this A2A method")
    try:
        if rpc.method == "message/send":
            return _send_task(peer, SendParams.model_validate(rpc.params))
        return _get_task(peer, GetParams.model_validate(rpc.params))
    except ValidationError as exc:
        raise RPCError(-32602, "Invalid params") from exc
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) in ("55P03", "57014"):
            raise HTTPException(503, "A2A task storage is busy") from exc
        raise


async def _preview_peer(request: Request) -> A2ATaskPeer:
    if not PREVIEW_AUTH_SLOTS.acquire(blocking=False):
        raise HTTPException(503, "A2A admission is busy")
    try:
        peer = await _authenticate(request)
        if peer.grant_id is None:
            raise HTTPException(503, "Preview admission is unavailable")
        await run_in_threadpool(
            consume_grant,
            peer.grant_id,
            settings.preview_task_rate_per_grant,
            settings.preview_task_burst_per_grant,
        )
        return peer
    finally:
        PREVIEW_AUTH_SLOTS.release()


@router.post("/a2a", response_model=None)
async def run_a2a_task(request: Request) -> dict | JSONResponse:
    versions = request.headers.getlist("a2a-version")
    version = versions[0] if len(versions) == 1 else ""
    native = version == "1.0" and settings.a2a_native
    if len(versions) > 1 or (version not in ("", "0.3", "0.3.0") and not native):
        if not REQUEST_SLOTS.acquire(blocking=False):
            raise HTTPException(503, "A2A runtime is busy")
        try:
            try:
                unsupported = RPCRequest.model_validate(await _read_json(request, native=True))
                if isinstance(unsupported.id, str):
                    unsupported.id.encode("utf-8")
            except (ValidationError, UnicodeEncodeError):
                raise RPCError(-32600, "Invalid request") from None
            return {
                "jsonrpc": "2.0",
                "id": unsupported.id,
                "error": {"code": -32009, "message": "Unsupported A2A version"},
            }
        except RPCError as exc:
            return {"jsonrpc": "2.0", "id": None, "error": {"code": exc.code, "message": str(exc)}}
        finally:
            REQUEST_SLOTS.release()
    preview = settings.preview_limits
    typed = False
    if settings.collaboration_execution:
        from agent_commons import typed_tasks

        auth = request.headers.getlist("authorization")
        scheme, _, token = auth[0].partition(" ") if len(auth) == 1 else ("", "", "")
        typed = scheme.lower() == "bearer" and typed_tasks.TOKEN_PATTERN.fullmatch(token)
        if typed:
            require_audience(request)
            if not PREVIEW_AUTH_SLOTS.acquire(blocking=False):
                raise HTTPException(503, "A2A admission is busy")
            try:
                if (
                    await run_in_threadpool(
                        _authenticate_grant, hashlib.sha256(token.encode()).hexdigest(), typed=True
                    )
                    is None
                ):
                    raise HTTPException(
                        401, "Invalid A2A peer credential", headers={"WWW-Authenticate": "Bearer"}
                    )
            finally:
                PREVIEW_AUTH_SLOTS.release()
    if native and not typed:
        raise HTTPException(
            401,
            "Native A2A requires a typed peer credential",
            headers={"WWW-Authenticate": "Bearer"},
        )
    peer = await _preview_peer(request) if preview and not typed else None
    if not REQUEST_SLOTS.acquire(blocking=False):
        raise HTTPException(503, "A2A runtime is busy")
    request_id = None
    dispatched = False
    try:
        if peer is None and not typed:
            peer = await _authenticate(request)
        try:
            raw = await _read_json(request, native=native)
        except HTTPException:
            if typed:
                await run_in_threadpool(typed_tasks.charge_invalid, token)
            raise
        try:
            rpc = RPCRequest.model_validate(raw)
            if isinstance(rpc.id, str):
                rpc.id.encode("utf-8")
        except (ValidationError, UnicodeEncodeError) as exc:
            raise RPCError(-32600, "Invalid request") from exc
        request_id = rpc.id
        if typed:
            if native:
                from agent_commons.a2a_native import decode_request, encode_result

                rpc = decode_request(rpc)
                dispatched = True
                result = await run_in_threadpool(typed_tasks.dispatch, token, rpc, native=True)
                result = encode_result(rpc.method, result)
            else:
                result = await run_in_threadpool(typed_tasks.dispatch, token, rpc)
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        if preview and rpc.method in peer.scopes:
            try:
                await run_in_threadpool(consume, "tasks", settings.preview_tasks_per_second, 1)
            except HTTPException as exc:
                if exc.status_code != 429:
                    raise
                return JSONResponse(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32012, "message": "Preview request rate exceeded"},
                    },
                    status_code=429,
                    headers=exc.headers,
                )
        result = await run_in_threadpool(_dispatch, peer, rpc)
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    except RPCError as exc:
        if typed and (request_id is None or (native and not dispatched)):
            await run_in_threadpool(typed_tasks.charge_invalid, token)
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": exc.code, "message": str(exc)},
        }
    except HTTPException as exc:
        if not typed or exc.status_code != 429 or request_id is None:
            raise
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32012, "message": "Preview request rate exceeded"},
            },
            status_code=429,
            headers=exc.headers,
        )
    finally:
        REQUEST_SLOTS.release()
