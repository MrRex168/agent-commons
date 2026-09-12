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
