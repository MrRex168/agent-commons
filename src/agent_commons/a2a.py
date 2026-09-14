from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from agent_commons.config import settings
from agent_commons.db import get_db
from agent_commons.models import Agent
from agent_commons.profile_models import AgentStructuredProfile

A2A_PROTOCOL_VERSION = "0.3.0"
SUPPORTED_TRANSPORTS = {"JSONRPC", "GRPC", "HTTP+JSON"}

router = APIRouter(prefix="/agents", tags=["a2a"])
well_known_router = APIRouter(tags=["a2a"])


class A2AAgentCapabilities(BaseModel):
    streaming: bool | None = None
    pushNotifications: bool | None = None
    stateTransitionHistory: bool | None = None


class A2AAgentSkill(BaseModel):
    id: str
    name: str
    description: str
    tags: list[str]
    examples: list[str] | None = None
    inputModes: list[str] | None = None
    outputModes: list[str] | None = None


class A2AAgentInterface(BaseModel):
    url: str
    transport: str


class A2AAgentCard(BaseModel):
    protocolVersion: str = A2A_PROTOCOL_VERSION
    name: str
    description: str
    url: str
    preferredTransport: str = "JSONRPC"
    additionalInterfaces: list[A2AAgentInterface] | None = None
    version: str
    documentationUrl: str | None = None
    capabilities: A2AAgentCapabilities = Field(default_factory=A2AAgentCapabilities)
    securitySchemes: dict[str, Any] | None = None
    security: list[dict[str, list[str]]] | None = None
    defaultInputModes: list[str]
    defaultOutputModes: list[str]
    skills: list[A2AAgentSkill]
    supportsAuthenticatedExtendedCard: bool = False


def _profile_and_config(agent: Agent, db: Session) -> tuple[AgentStructuredProfile | None, dict[str, Any]]:
    profile = db.get(AgentStructuredProfile, agent.id)
    metadata = profile.profile_data if profile is not None else {}
    raw = metadata.get("a2a") if isinstance(metadata, dict) else None
    config = raw if isinstance(raw, dict) else {}
    return profile, config


def _required_string(config: dict[str, Any], key: str) -> str:
    value = config.get(key)
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A2A metadata requires a non-empty '{key}' value",
        )
    return value.strip()


def _string_list(config: dict[str, Any], key: str, default: list[str]) -> list[str]:
    value = config.get(key, default)
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A2A metadata '{key}' must be a non-empty list of strings",
        )
    return value


def _skill_from_capability(capability: str) -> A2AAgentSkill:
    skill_id = capability.strip().lower().replace(" ", "-").replace("_", "-")
    return A2AAgentSkill(
        id=skill_id,
        name=capability,
        description=f"Agent capability: {capability}",
        tags=[capability],
    )


def _skills(config: dict[str, Any], profile: AgentStructuredProfile | None) -> list[A2AAgentSkill]:
    raw_skills = config.get("skills")
    if raw_skills is not None:
        if not isinstance(raw_skills, list):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A2A metadata 'skills' must be a list",
            )
        try:
            return [A2AAgentSkill.model_validate(item) for item in raw_skills]
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Invalid A2A skill metadata: {exc}",
            ) from exc

    capabilities = profile.capabilities if profile is not None else []
    return [_skill_from_capability(item) for item in capabilities]


def build_agent_card(agent: Agent, db: Session) -> A2AAgentCard:
    profile, config = _profile_and_config(agent, db)
    endpoint = _required_string(config, "url")
    transport = str(config.get("preferredTransport", "JSONRPC")).upper()
    if transport not in SUPPORTED_TRANSPORTS:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A2A preferredTransport must be JSONRPC, GRPC, or HTTP+JSON",
        )

    version = str(config.get("version", "1.0.0"))
    if not version.strip():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A2A metadata 'version' must be non-empty",
        )

    capabilities_config = config.get("capabilities", {})
    if not isinstance(capabilities_config, dict):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A2A metadata 'capabilities' must be an object",
        )

    interfaces = config.get("additionalInterfaces")
    additional_interfaces = None
    if interfaces is not None:
        if not isinstance(interfaces, list):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A2A metadata 'additionalInterfaces' must be a list",
            )
        try:
            additional_interfaces = [A2AAgentInterface.model_validate(item) for item in interfaces]
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Invalid A2A interface metadata: {exc}",
            ) from exc
        for interface in additional_interfaces:
            if interface.transport.upper() not in SUPPORTED_TRANSPORTS:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="A2A additional interface uses an unsupported transport",
                )

    security_schemes = config.get("securitySchemes")
    security_requirements = config.get("security")
    if security_schemes is not None and not isinstance(security_schemes, dict):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A2A metadata 'securitySchemes' must be an object",
        )
    if security_requirements is not None and not isinstance(security_requirements, list):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A2A metadata 'security' must be a list",
        )

    return A2AAgentCard(
        name=agent.name,
        description=agent.description or f"Agent Commons agent {agent.name}",
        url=endpoint,
        preferredTransport=transport,
        additionalInterfaces=additional_interfaces,
        version=version,
        documentationUrl=config.get("documentationUrl"),
        capabilities=A2AAgentCapabilities.model_validate(capabilities_config),
        securitySchemes=security_schemes,
        security=security_requirements,
        defaultInputModes=_string_list(config, "defaultInputModes", ["text/plain"]),
        defaultOutputModes=_string_list(config, "defaultOutputModes", ["text/plain"]),
        skills=_skills(config, profile),
        supportsAuthenticatedExtendedCard=bool(
            config.get("supportsAuthenticatedExtendedCard", False)
        ),
    )


@router.get("/{agent_name}/agent-card", response_model=A2AAgentCard)
def get_agent_card(
    agent_name: str,
    db: Session = Depends(get_db),
) -> A2AAgentCard:
    agent = db.scalar(select(Agent).where(Agent.name == agent_name))
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    return build_agent_card(agent, db)


@well_known_router.get("/.well-known/agent-card.json", response_model=A2AAgentCard)
def get_well_known_agent_card(
    db: Session = Depends(get_db),
) -> A2AAgentCard:
    agent_name = settings.a2a_default_agent
    if not agent_name:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No default A2A agent configured for this deployment",
        )
    agent = db.scalar(select(Agent).where(Agent.name == agent_name))
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Configured default A2A agent was not found",
        )
    return build_agent_card(agent, db)
