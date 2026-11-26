"""Typed approvals and sanitized task results for direct A2A 0.3 dispatch."""

import re
from typing import Annotated, Literal
from uuid import UUID

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from agent_commons.endpoint_authorization import canonical_endpoint_url
from agent_commons.peer_protocol import IssuedGrant, Root, SkillIssuedGrant, https_audience


class DestinationApproval(BaseModel):
    """Exact destination independently approved by the caller's operator."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reference_id: UUID
    peer_root: Root
    controller: Annotated[str, Field(min_length=2, max_length=128)]
    recipient_agent_id: UUID
    endpoint: str
    transport: Literal["JSONRPC"] = "JSONRPC"

    @model_validator(mode="after")
    def canonical_endpoint(self):
        if (
            canonical_endpoint_url(self.endpoint) != self.endpoint
            or str(httpx.URL(self.endpoint)) != self.endpoint
            or httpx.URL(self.endpoint).query
            or "?" in self.endpoint
        ):
            raise ValueError("Approve an exact canonical HTTPS task URL without a query")
        return self

    @property
    def audience(self) -> str:
        return https_audience(str(httpx.URL(self.endpoint).copy_with(path="/")).rstrip("/"))


class BoundGrant(BaseModel):
    """A fresh grant plus the verified location at the time of enrollment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    destination: DestinationApproval
    home_origin: str
    grant: SkillIssuedGrant | IssuedGrant
    expires_at: AwareDatetime
    identity_sequence: int = Field(ge=0)
    location_sequence: int = Field(ge=1)

    @model_validator(mode="before")
    @classmethod
    def check_version(cls, data):
        if isinstance(data, dict) and isinstance(grant := data.get("grant"), dict):
            if "version" in grant or "skills" in grant:
                SkillIssuedGrant.model_validate(grant)
        return data

    @model_validator(mode="after")
    def matching_grant(self):
        prefix = "a2c_" if isinstance(self.grant, SkillIssuedGrant) else "a2a_"
        if (
            self.grant.audience != self.destination.audience
            or self.grant.recipient_agent_id != self.destination.recipient_agent_id
            or self.grant.expires_at != self.expires_at
            or not re.fullmatch(prefix + r"[A-Za-z0-9_-]{43}", self.grant.token.get_secret_value())
        ):
            raise ValueError("Grant does not match the approved destination")
        return self


class TaskStatistics(BaseModel):
    id: UUID
    state: Literal["completed"]
    bytes: int = Field(ge=0, le=4096)
    words: int = Field(ge=0, le=4096)


def statistics(task: dict) -> TaskStatistics:
    if task["kind"] != "task" or task["status"]["state"] != "completed":
        raise ValueError
    (artifact,) = task["artifacts"]
    (part,) = artifact["parts"]
    if artifact["name"] != "text-statistics" or part["kind"] != "text":
        raise ValueError
    match = re.fullmatch(r"bytes: ([0-9]{1,4})\nwords: ([0-9]{1,4})", part["text"])
    if match is None:
        raise ValueError
    return TaskStatistics(
        id=task["id"], state="completed", bytes=int(match[1]), words=int(match[2])
    )
