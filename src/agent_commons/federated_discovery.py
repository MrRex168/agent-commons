from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import and_, case, cast, func, literal, or_, select, text, tuple_
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, aliased

from agent_commons.auth import get_current_agent
from agent_commons.collaboration import EXTENSION_URI, SKILL_PATTERN
from agent_commons.db import get_db
from agent_commons.models import Agent
from agent_commons.remote_models import EndpointStatus, RemoteAgentReference

router = APIRouter(prefix="/agents/discovery", tags=["federated-discovery"])
PAGE_SIZE = 25
SCAN_SIZE = 250


class FederatedAgentSkill(BaseModel):
    id: str
    name: str
    description: str
    tags: list[str]


class FederatedAgentProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str | None
    card_url: str | None
    a2a_url: str | None
    root_fingerprint: str | None
    identity_sequence: int | None
    identity_verified: bool
    lineage_verified: bool
    endpoint_authorization_status: EndpointStatus
    resolved_at: datetime
    skills: list[FederatedAgentSkill]


class FederatedDiscoveryResults(BaseModel):
    query: str
    skill: str | None
    agents: list[FederatedAgentProfile]
    next_cursor: str | None = None


def _skills(reference: RemoteAgentReference) -> list[FederatedAgentSkill]:
    raw = reference.card_snapshot.get("skills", [])
    if not isinstance(raw, list):
        return []
    skills: list[FederatedAgentSkill] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            skills.append(FederatedAgentSkill.model_validate(item))
        except ValueError:
            continue
    return skills


def _cursor(name: str, identifier: UUID) -> str:
    return base64.urlsafe_b64encode(
        json.dumps([name, str(identifier)], ensure_ascii=False).encode()
    ).decode()


def _cursor_key(cursor: str) -> tuple[str, UUID]:
    try:
        name, identifier = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if not isinstance(name, str) or len(name) > 200 or "\x00" in name:
            raise ValueError
        name.encode("utf-8")
        return name, UUID(identifier)
    except (TypeError, ValueError, AttributeError, RecursionError):
        raise HTTPException(422, "Invalid discovery cursor") from None


def _array(value):
    return case((func.jsonb_typeof(value) == "array", value), else_=cast(literal("[]"), JSONB))


def _skill_filter(reference, skill: str):
    snapshot = reference.card_snapshot
    entry = func.jsonb_array_elements(_array(snapshot["skills"])).column_valued(
        "skill_entry", joins_implicitly=True
    )
    item = cast(entry, JSONB)
    tag = func.jsonb_array_elements_text(_array(item["tags"])).column_valued(
        "skill_tag", joins_implicitly=True
    )
    tag_match = select(literal(1)).where(tag.icontains(skill, autoescape=True)).exists()
    matches = or_(
        *(
            item[field].astext.icontains(skill, autoescape=True)
            for field in ("id", "name", "description")
        ),
        tag_match,
    )
    return (
        select(literal(1))
        .where(
            *(
                func.jsonb_typeof(item[field]) == "string"
                for field in ("id", "name", "description")
            ),
            func.jsonb_typeof(item["tags"]) == "array",
            matches,
        )
        .correlate(reference)
        .exists()
    )


def _profile(reference: RemoteAgentReference, now: datetime) -> FederatedAgentProfile:
    endpoint_status = reference.endpoint_status(now)
    routable = endpoint_status in ("authorized", "non_sovereign")
    return FederatedAgentProfile(
        name=reference.name,
        description=reference.description,
        card_url=reference.card_url if routable else None,
        a2a_url=reference.a2a_url if routable else None,
        root_fingerprint=reference.root_fingerprint,
        identity_sequence=reference.identity_sequence,
        identity_verified=endpoint_status == "authorized",
        lineage_verified=reference.identity_verified,
        endpoint_authorization_status=endpoint_status,
        resolved_at=reference.resolved_at,
        skills=_skills(reference),
    )


@router.get("/remote", response_model=FederatedDiscoveryResults)
def discover_remote_agents(
    q: str = Query(default="", max_length=100),
    skill: str | None = Query(default=None, min_length=2, max_length=100),
    skill_exact: str | None = Query(default=None, pattern=SKILL_PATTERN, max_length=80),
    verified_only: bool = Query(default=False),
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=1800),
    _agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> FederatedDiscoveryResults:
    """Search a bounded, keyset-ordered segment of resolved remote agents."""
    if skill is not None and skill_exact is not None:
        raise HTTPException(422, "Choose skill or skill_exact")
    model = RemoteAgentReference
    page_size = min(limit, PAGE_SIZE)
    keys = select(model.name, model.id)
    if cursor is not None:
        keys = keys.where(tuple_(model.name, model.id) > _cursor_key(cursor))
    now = datetime.now(UTC)
    try:
        db.execute(text("SET LOCAL statement_timeout = '750ms'"))
        candidates = db.execute(keys.order_by(model.name, model.id).limit(SCAN_SIZE + 1)).all()
        if not candidates:
            return FederatedDiscoveryResults(query=q, skill=skill_exact or skill, agents=[])
        # Materialize at most SCAN_SIZE rows before touching large JSON or text values.
        # Select the original rows by primary key in SQL, including all visibility filters.
        ids = [row.id for row in candidates[:SCAN_SIZE]]
        window = (
            select(model)
            .where(model.id.in_(ids))
            .cte("discovery_window")
            .prefix_with("MATERIALIZED")
        )
        candidate = aliased(model, window)
        query = select(candidate)
        if q:
            query = query.where(
                or_(
                    candidate.name.icontains(q, autoescape=True),
                    candidate.description.icontains(q, autoescape=True),
                    candidate.root_fingerprint.icontains(q, autoescape=True),
                )
            )
        if verified_only:
            query = query.where(
                and_(candidate.identity_verified.is_(True), candidate.location_expires_at > now)
            )
        if skill is not None:
            query = query.where(_skill_filter(candidate, skill))
        if skill_exact is not None:
            identifier, version = skill_exact.split("@")
            query = query.where(
                candidate.card_snapshot["capabilities"]["extensions"].contains(
                    [
                        {
                            "uri": EXTENSION_URI,
                            "params": {
                                "version": 1,
                                "skills": [{"id": identifier, "schemaVersion": int(version)}],
                            },
                        }
                    ]
                )
            )
        matches = db.scalars(
            query.order_by(candidate.name, candidate.id).limit(page_size + 1)
        ).all()
    except SQLAlchemyError:
        raise HTTPException(503, "Remote discovery is unavailable") from None

    profiles = [_profile(item, now) for item in matches[:page_size]]
    next_cursor = None
    if len(matches) > page_size:
        last = matches[page_size - 1]
        next_cursor = _cursor(last.name, last.id)
    elif len(candidates) > SCAN_SIZE:
        last = candidates[SCAN_SIZE - 1]
        next_cursor = _cursor(last.name, last.id)
    return FederatedDiscoveryResults(
        query=q, skill=skill_exact or skill, agents=profiles, next_cursor=next_cursor
    )
