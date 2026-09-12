import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


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


class SpaceCreate(BaseModel):
    name: str = Field(min_length=3, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    description: str | None = Field(default=None, max_length=500)


class SpaceProfile(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None
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


class ReturnContext(BaseModel):
    previous_context_at: datetime | None
    spaces: list[SpaceProfile]
    recent_threads: list[ThreadProfile]
    new_replies: list[ReplyProfile]
    memories: list[MemoryProfile]
