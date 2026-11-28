"""Typed task handles and owner-scoped observation wire models."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agent_commons.collaboration import SkillKey, validate_typed_output
from agent_commons.peer_protocol import Root

TaskState = Literal["submitted", "working", "completed", "failed", "canceled", "rejected"]
TERMINAL_STATES = frozenset({"completed", "failed", "canceled", "rejected"})
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class TypedTaskRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    skill: SkillKey
    peer_root: Root
    endpoint: str = Field(max_length=500)
    identity_sequence: int = Field(ge=0)
    location_sequence: int = Field(ge=1)


class TypedTask(BaseModel):
    reference: TypedTaskRef
    status: TaskState
    artifact: dict | None = None

    @property
    def id(self) -> UUID:
        return self.reference.id


class CollaborationObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reference_id: UUID
    caller_root: Root
    task: TypedTaskRef
    status: TaskState
    artifact_digest: Digest | None = None


class CollaborationRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    caller_root: Root
    peer_root: Root
    task_id: UUID
    skill: SkillKey
    execution_origin: str
    status: TaskState
    submitted_at: datetime
    completed_at: datetime | None
    artifact_digest: Digest | None
    endpoint: str
    identity_sequence: int
    location_sequence: int
    location_digest: Digest
    verified_at: datetime
    expires_at: datetime


class CollaborationPage(BaseModel):
    records: list[CollaborationRecord] = Field(max_length=25)
    next_cursor: UUID | None


class KnownPeer(BaseModel):
    name: str
    root_fingerprint: Root | None
    card_url: str | None
    a2a_url: str | None
    identity_verified: bool


class KnownPeerPage(BaseModel):
    agents: list[KnownPeer] = Field(max_length=25)
    next_cursor: str | None


def decode_task(payload: dict, *, skill: str) -> tuple[UUID, TaskState, dict | None]:
    if payload["kind"] != "task":
        raise ValueError
    identifier = UUID(payload["id"])
    UUID(payload["contextId"])
    state = payload["status"]["state"]
    if state not in TERMINAL_STATES and state not in ("submitted", "working"):
        raise ValueError
    artifacts = payload.get("artifacts", [])
    if state == "completed":
        (artifact,) = artifacts
        UUID(artifact["artifactId"])
        if artifact["name"] != skill:
            raise ValueError
        (part,) = artifact["parts"]
        if part["kind"] != "data":
            raise ValueError
        return identifier, state, validate_typed_output(skill, part["data"])
    if artifacts:
        raise ValueError
    return identifier, state, None


class NativeTaskStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: Literal[
        "TASK_STATE_SUBMITTED",
        "TASK_STATE_WORKING",
        "TASK_STATE_COMPLETED",
        "TASK_STATE_FAILED",
        "TASK_STATE_CANCELED",
        "TASK_STATE_REJECTED",
    ]
    timestamp: datetime


class NativeTaskSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    contextId: UUID
    status: NativeTaskStatus


class NativeTaskPage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tasks: list[NativeTaskSummary] = Field(max_length=20)
    pageSize: int = Field(strict=True, ge=0, le=20)
    totalSize: int = Field(strict=True, ge=0, le=1024)
    nextPageToken: UUID | Literal[""]
