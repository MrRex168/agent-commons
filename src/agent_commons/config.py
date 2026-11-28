from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from agent_commons.collaboration import SkillScopes, registry
from agent_commons.peer_protocol import CollaborationMethod, https_audience


class A2ATaskPeer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token_sha256: str = Field(pattern=r"^[0-9a-f]{64}$", repr=False)
    grant_id: UUID | None = None
    skill_scopes: SkillScopes | None = None
    caller_root: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    recipient_agent_id: UUID
    scopes: set[CollaborationMethod] = Field(min_length=1)
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def validate_cancel_scope(self):
        if self.scopes - {"message/send", "tasks/get"} and self.skill_scopes is None:
            raise ValueError("Native operations and cancellation require a typed grant")
        return self


class Settings(BaseSettings):
    app_name: str = "Agent Commons"
    environment: str = "development"
    database_url: str = (
        "postgresql+psycopg://agent_commons:agent_commons@localhost:5432/agent_commons"
    )
    api_url: str = "http://127.0.0.1:8000"
    api_key: str | None = None
    a2a_default_agent: str | None = None
    registration_admission: Literal["open", "invite"] = "open"
    mcp_registration_invitation: str | None = Field(default=None, repr=False)

    preview_limits: bool = False
    preview_registrations_per_hour: int = Field(default=5, ge=1, le=100)
    preview_resolutions_per_minute: int = Field(default=30, ge=1, le=300)
    preview_resolutions_per_caller: int = Field(default=10, ge=1, le=300)
    preview_task_rate_per_grant: float = Field(default=2, ge=0.1, le=100)
    preview_task_burst_per_grant: int = Field(default=5, ge=1, le=32)
    preview_tasks_per_second: int = Field(default=10, ge=1, le=100)

    a2a_native: bool = False
    collaboration_execution: bool = False
    collaboration_skills: dict[UUID, SkillScopes] = Field(default_factory=dict)

    a2a_database_grants: bool = False
    a2a_task_peers: list[A2ATaskPeer] = Field(default_factory=list, max_length=32, repr=False)

    @field_validator("a2a_task_peers")
    @classmethod
    def unique_peer_credentials(cls, peers: list[A2ATaskPeer]) -> list[A2ATaskPeer]:
        if len({peer.token_sha256 for peer in peers}) != len(peers):
            raise ValueError("A2A peer token hashes must be unique")
        return peers

    @model_validator(mode="after")
    def validate_grant_mode(self):
        for scopes in self.collaboration_skills.values():
            if len(set(scopes)) != len(scopes):
                raise ValueError("Duplicate configured collaboration skill")
            for scope in scopes:
                registry.get(scope)
        if self.a2a_native and not self.collaboration_execution:
            raise ValueError("Native A2A requires typed collaboration execution")
        if self.collaboration_execution and not (self.a2a_database_grants and self.preview_limits):
            raise ValueError("Collaboration execution requires database grants and preview quotas")
        if self.preview_limits and not self.a2a_database_grants:
            raise ValueError("Preview limits require database-backed A2A grants")
        if self.a2a_database_grants:
            if self.a2a_task_peers:
                raise ValueError("Database grants cannot be mixed with static peers")
            https_audience(self.api_url)
            if self.registration_admission != "invite":
                raise ValueError("Database grants require invite-only registration")
        return self

    model_config = SettingsConfigDict(env_file=".env", env_prefix="AGENT_COMMONS_")


settings = Settings()
