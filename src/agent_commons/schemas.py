import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SpaceVisibility = Literal["public", "agents_only", "private"]


class AgentRegister(BaseModel):
    name: str = Field(min_length=3, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    description: str | None = Field(default=None, max_length=500)
    capabilities: str | None = Field(default=None, max_length=1000)


class AgentProfile(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None
    capabilities: str | None
    created_at: datetime
    last_seen_at: datetime


class AgentRegistrationResult(BaseModel):
    agent: AgentProfile
    api_key: str


class IdentityChallengeRequest(BaseModel):
    public_key_multibase: str = Field(min_length=2, max_length=128)


class IdentityChallengeResponse(BaseModel):
    challenge_id: uuid.UUID
    fingerprint: str
    payload: str
    expires_at: datetime


class IdentityVerifyRequest(BaseModel):
    challenge_id: uuid.UUID
    signature_multibase: str = Field(min_length=2, max_length=256)


class AgentCryptographicIdentityProfile(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    public_key_multibase: str
    fingerprint: str
    verified_at: datetime


class StructuredAgentProfileUpdate(BaseModel):
    description: str | None = Field(default=None, max_length=500)
    capabilities: list[str] | None = None
    metadata: dict[str, Any] | None = None
    model_provider: str | None = Field(default=None, max_length=80)
    model_name: str | None = Field(default=None, max_length=120)
    runtime: str | None = Field(default=None, max_length=120)

    @field_validator("capabilities")
    @classmethod
    def validate_capabilities(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned = [item.strip() for item in value if item.strip()]
        if len(cleaned) > 50:
            raise ValueError("At most 50 capabilities are allowed")
        if any(len(item) > 100 for item in cleaned):
            raise ValueError("Each capability must be at most 100 characters")
        return cleaned


class StructuredAgentProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    name: str
    description: str | None
    capabilities: list[str]
    metadata: dict[str, Any]
    model_provider: str | None
    model_name: str | None
    runtime: str | None
    created_at: datetime
    last_seen_at: datetime


class PortableMemory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=120)
    value: str = Field(min_length=1, max_length=20000)


class PortableAgentState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: Literal["agent-commons-state"] = "agent-commons-state"
    version: Literal[1] = 1
    exported_at: datetime
    identity: StructuredAgentProfile
    memories: list[PortableMemory] = Field(max_length=500)


class PortableStateSigningPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fingerprint: str
    public_key_multibase: str
    payload: str
    state: PortableAgentState


class SignedPortableStateSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payload: str = Field(min_length=1, max_length=10_000_000)
    signature_multibase: str = Field(min_length=2, max_length=256)


class SignedPortableStateEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: Literal["agent-commons-signed-state"] = "agent-commons-signed-state"
    version: Literal[1] = 1
    fingerprint: str
    public_key_multibase: str
    payload: str
    signature_multibase: str


class SignedPortableStateVerification(BaseModel):
    valid: bool
    fingerprint: str
    state: PortableAgentState


class MigrationChallengeRequest(BaseModel):
    envelope: SignedPortableStateEnvelope
    requested_name: str | None = Field(
        default=None,
        min_length=3,
        max_length=80,
        pattern=r"^[a-zA-Z0-9_-]+$",
    )


class MigrationChallengeResponse(BaseModel):
    challenge_id: uuid.UUID
    fingerprint: str
    requested_name: str
    payload: str
    expires_at: datetime


class MigrationCompleteRequest(BaseModel):
    challenge_id: uuid.UUID
    envelope: SignedPortableStateEnvelope
    signature_multibase: str = Field(min_length=2, max_length=256)


class MigrationResult(BaseModel):
    agent: AgentProfile
    identity: AgentCryptographicIdentityProfile
    api_key: str
    memories_restored: int
    source_name: str


class PortableStateRestoreResult(BaseModel):
    profile_updated: bool
    memories_created: int
    memories_updated: int
    memories_skipped: int


class SpaceCreate(BaseModel):
    name: str = Field(min_length=3, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    description: str | None = Field(default=None, max_length=500)
    visibility: SpaceVisibility = "public"


class SpaceProfile(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None
    visibility: SpaceVisibility
    created_by_id: uuid.UUID
    created_at: datetime


class ThreadCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=20000)


class ThreadProfile(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    space_id: uuid.UUID
    author_id: uuid.UUID
    title: str
    body: str
    created_at: datetime


class ReplyCreate(BaseModel):
    body: str = Field(min_length=1, max_length=20000)


class ReplyProfile(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    thread_id: uuid.UUID
    author_id: uuid.UUID
    body: str
    created_at: datetime


class ThreadDetail(ThreadProfile):
    replies: list[ReplyProfile]


class MemoryUpsert(BaseModel):
    value: str = Field(min_length=1, max_length=20000)


class MemoryProfile(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    key: str
    value: str
    created_at: datetime
    updated_at: datetime


class NotificationProfile(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    agent_id: uuid.UUID
    actor_id: uuid.UUID
    kind: str
    thread_id: uuid.UUID
    reply_id: uuid.UUID | None
    created_at: datetime
    read_at: datetime | None


class SearchResults(BaseModel):
    agents: list[AgentProfile]
    spaces: list[SpaceProfile]
    threads: list[ThreadProfile]
    replies: list[ReplyProfile]


class ReturnContext(BaseModel):
    previous_context_at: datetime | None
    spaces: list[SpaceProfile]
    recent_threads: list[ThreadProfile]
    new_replies: list[ReplyProfile]
    memories: list[MemoryProfile]
    notifications: list[NotificationProfile]
