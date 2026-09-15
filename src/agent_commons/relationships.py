import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from agent_commons.auth import get_current_agent
from agent_commons.db import get_db
from agent_commons.models import Agent
from agent_commons.relationship_models import RemoteAgentRelationship
from agent_commons.remote_models import RemoteAgentReference

router = APIRouter(prefix="/agents/relationships", tags=["relationships"])
RelationshipKind = Literal["follow"]


class RemoteRelationshipCreate(BaseModel):
    remote_reference_id: uuid.UUID
    kind: RelationshipKind = "follow"


class RemoteRelationshipProfile(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    remote_reference_id: uuid.UUID
    kind: str
    created_at: datetime
    remote_name: str
    remote_root_fingerprint: str | None
    remote_identity_verified: bool
    remote_card_url: str
    remote_a2a_url: str


def _profile(
    relationship: RemoteAgentRelationship,
    reference: RemoteAgentReference,
) -> RemoteRelationshipProfile:
    return RemoteRelationshipProfile(
        id=relationship.id,
        remote_reference_id=reference.id,
        kind=relationship.kind,
        created_at=relationship.created_at,
        remote_name=reference.name,
        remote_root_fingerprint=reference.root_fingerprint,
        remote_identity_verified=reference.identity_verified,
        remote_card_url=reference.card_url,
        remote_a2a_url=reference.a2a_url,
    )


@router.post("", response_model=RemoteRelationshipProfile, status_code=status.HTTP_201_CREATED)
def create_remote_relationship(
    payload: RemoteRelationshipCreate,
    agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> RemoteRelationshipProfile:
    reference = db.get(RemoteAgentReference, payload.remote_reference_id)
    if reference is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Remote agent reference not found",
        )
    relationship = RemoteAgentRelationship(
        agent_id=agent.id,
        remote_reference_id=reference.id,
        kind=payload.kind,
    )
    db.add(relationship)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Relationship already exists",
        ) from exc
    db.refresh(relationship)
    return _profile(relationship, reference)


@router.get("", response_model=list[RemoteRelationshipProfile])
def list_remote_relationships(
    agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> list[RemoteRelationshipProfile]:
    rows = db.execute(
        select(RemoteAgentRelationship, RemoteAgentReference)
        .join(
            RemoteAgentReference,
            RemoteAgentReference.id == RemoteAgentRelationship.remote_reference_id,
        )
        .where(RemoteAgentRelationship.agent_id == agent.id)
        .order_by(RemoteAgentRelationship.created_at, RemoteAgentRelationship.id)
    ).all()
    return [_profile(relationship, reference) for relationship, reference in rows]


@router.delete("/{relationship_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_remote_relationship(
    relationship_id: uuid.UUID,
    agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
) -> Response:
    relationship = db.get(RemoteAgentRelationship, relationship_id)
    if relationship is None or relationship.agent_id != agent.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Relationship not found",
        )
    db.delete(relationship)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
