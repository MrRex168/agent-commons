"""Wire types shared by the enrollment server and client."""

import json
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, SecretStr

from agent_commons.collaboration import SkillScopes

Method = Literal["message/send", "tasks/get"]
Root = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
Scopes = Annotated[list[Method], Field(min_length=1, max_length=2)]
DOMAIN = "agent-commons/peer-enrollment/v1"


def https_audience(value: str) -> str:
    url = httpx.URL(value)
    canonical = str(url.copy_with(path="/")).rstrip("/")
    if url.scheme != "https" or not url.host or value != canonical:
        raise ValueError("Peer grants require a canonical HTTPS origin without a trailing slash")
    if "@" in value or "?" in value or "#" in value:
        raise ValueError("Peer grant audience must be an HTTPS origin")
    return canonical


class EnrollmentChallenge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approval_id: UUID
    challenge_id: UUID
    audience: str
    caller_root: Root
    controller: Annotated[str, Field(min_length=2, max_length=128)]
    identity_sequence: Annotated[int, Field(ge=0)]
    recipient_agent_id: UUID
    scopes: Scopes
    expires_at: AwareDatetime

    def signing_payload(self) -> str:
        return (
            DOMAIN
            + "\n"
            + json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        )


class Redemption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    challenge_id: UUID
    signature_multibase: Annotated[str, Field(min_length=2, max_length=128)]


class IssuedGrant(BaseModel):
    grant_id: UUID
    audience: str
    caller_root: Root
    recipient_agent_id: UUID
    scopes: Scopes
    expires_at: datetime
    token: SecretStr


class SkillEnrollmentChallenge(EnrollmentChallenge):
    version: Literal[2]
    skills: SkillScopes

    def signing_payload(self) -> str:
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return "agent-commons/peer-enrollment/v2\n" + canonical


class SkillIssuedGrant(IssuedGrant):
    model_config = ConfigDict(extra="forbid")

    version: Literal[2]
    skills: SkillScopes
