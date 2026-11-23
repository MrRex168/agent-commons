from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class A2ATaskPeer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token_sha256: str = Field(pattern=r"^[0-9a-f]{64}$", repr=False)
    caller_root: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    recipient_agent_id: UUID
    scopes: set[Literal["message/send", "tasks/get"]] = Field(min_length=1)
    expires_at: AwareDatetime


class Settings(BaseSettings):
    app_name: str = "Agent Commons"
    environment: str = "development"
    database_url: str = "postgresql+psycopg://agent_commons:agent_commons@localhost:5432/agent_commons"
    api_url: str = "http://127.0.0.1:8000"
    api_key: str | None = None
    a2a_default_agent: str | None = None

    a2a_task_peers: list[A2ATaskPeer] = Field(default_factory=list, max_length=32, repr=False)

    @field_validator("a2a_task_peers")
    @classmethod
    def unique_peer_credentials(cls, peers: list[A2ATaskPeer]) -> list[A2ATaskPeer]:
        if len({peer.token_sha256 for peer in peers}) != len(peers):
            raise ValueError("A2A peer token hashes must be unique")
        return peers

    model_config = SettingsConfigDict(env_file=".env", env_prefix="AGENT_COMMONS_")


settings = Settings()
