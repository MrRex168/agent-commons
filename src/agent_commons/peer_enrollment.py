"""Operator-approved enrollment and receiving-side credential authorization."""

import re
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from agent_commons.collaboration import SkillScopes, validate_typed_request
from agent_commons.config import A2ATaskPeer, settings
from agent_commons.db import SessionLocal, get_db
from agent_commons.identity_crypto import verify_identity_signature
from agent_commons.lineage import PortableIdentityLineage, verify_lineage
from agent_commons.models import Agent
from agent_commons.peer_models import PeerApproval, PeerGrant
from agent_commons.peer_protocol import (
    CollaborationMethod,
    EnrollmentChallenge,
    IssuedGrant,
    Redemption,
    SkillEnrollmentChallenge,
    SkillIssuedGrant,
)
from agent_commons.remote_models import RemoteAgentReference
from agent_commons.security import hash_api_key

router = APIRouter(prefix="/api/v1/peers", tags=["peer-enrollment"])
CHALLENGE_TTL = timedelta(minutes=5)
ROTATION_OVERLAP = timedelta(minutes=5)


def _now(db: Session):
    return db.scalar(select(func.clock_timestamp()))


def _reference(db: Session, reference_id: UUID, expected_root: str):
    reference = db.get(
        RemoteAgentReference, reference_id, with_for_update=True, populate_existing=True
    )
    if (
        reference is None
        or reference.root_fingerprint != expected_root
        or reference.endpoint_status(_now(db)) != "authorized"
        or reference.lineage is None
    ):
        raise HTTPException(403, "An authorized sovereign peer with the expected root is required")
    try:
        lineage = verify_lineage(PortableIdentityLineage.model_validate(reference.lineage))
    except ValueError:
        raise HTTPException(403, "Peer lineage is not valid") from None
    if (
        lineage.root_fingerprint != expected_root
        or lineage.current_public_key_multibase != reference.current_controller_public_key
        or lineage.sequence != reference.identity_sequence
    ):
        raise HTTPException(403, "Peer lineage is inconsistent with the current reference")
    return lineage


def _recheck(db: Session, approval: PeerApproval) -> None:
    lineage = _reference(db, approval.reference_id, approval.caller_root)
    if (
        lineage.current_public_key_multibase != approval.controller
        or lineage.sequence != approval.identity_sequence
    ):
        raise HTTPException(403, "Peer controller changed; operator reapproval is required")

    if approval.skill_scopes is not None and not set(approval.skill_scopes) <= set(
        settings.collaboration_skills.get(approval.recipient_agent_id, ())
    ):
        raise HTTPException(403, "Peer issuance unavailable")


def _disable_challenge(approval: PeerApproval) -> None:
    approval.challenge_id = None
    approval.challenge_expires_at = None


def approve_peer(
    db: Session,
    *,
    reference_id: UUID,
    expected_root: str,
    recipient: UUID,
    scopes: list[CollaborationMethod],
    lifetime_hours: int = 1,
    skills: list[str] | None = None,
) -> UUID:
    if not settings.a2a_database_grants:
        raise ValueError("Database grant mode must be enabled")
    allowed = {"message/send", "tasks/get"}
    if skills is not None:
        allowed.add("tasks/cancel")
        if settings.a2a_native:
            allowed.update(("a2a/1.0", "tasks/list"))
    if (
        not scopes
        or not set(scopes) <= allowed
        or ("tasks/list" in scopes and "a2a/1.0" not in scopes)
        or not 1 <= lifetime_hours <= 24
    ):
        raise ValueError("Specify allowed methods and a grant lifetime of 1–24 hours")
    approval = db.scalar(
        select(PeerApproval)
        .where(
            PeerApproval.caller_root == expected_root,
            PeerApproval.recipient_agent_id == recipient,
        )
        .with_for_update()
    )
    lineage = _reference(db, reference_id, expected_root)
    agent = db.get(Agent, recipient)
    if agent is None:
        raise ValueError("The local recipient does not exist")
    if skills is not None:
        skills = sorted(TypeAdapter(SkillScopes).validate_python(skills))
        if len(set(skills)) != len(skills) or not set(skills) <= set(
            settings.collaboration_skills.get(agent.id, ())
        ):
            raise ValueError("Skills must be distinct and enabled locally for this recipient")
    if approval is None:
        approval = PeerApproval(id=uuid4(), caller_root=expected_root, recipient_agent_id=recipient)
        db.add(approval)
    else:
        db.execute(
            update(PeerGrant)
            .where(
                PeerGrant.approval_id == approval.id,
                PeerGrant.revoked_at.is_(None),
            )
            .values(revoked_at=_now(db))
        )
    approval.reference_id = reference_id
    approval.controller = lineage.current_public_key_multibase
    approval.identity_sequence = lineage.sequence
    approval.scopes = sorted(set(scopes))
    approval.skill_scopes = skills
    approval.lifetime_hours = lifetime_hours
    approval.audience = settings.api_url
    approval.issuance_pending = True
    approval.current_grant_id = None
    _disable_challenge(approval)
    return approval.id


def _locked_approval(db: Session, approval_id: UUID) -> PeerApproval:
    approval = db.get(PeerApproval, approval_id, with_for_update=True, populate_existing=True)
    if approval is None or approval.audience != settings.api_url:
        raise HTTPException(403, "Peer approval unavailable")
    return approval


def change_grant(db: Session, grant_id: UUID, *, rotate: bool) -> UUID:
    if not settings.a2a_database_grants:
        raise ValueError("Database grant mode must be enabled")
    approval_id = db.scalar(select(PeerGrant.approval_id).where(PeerGrant.id == grant_id))
    if approval_id is None:
        raise ValueError("Grant unavailable")
    approval = _locked_approval(db, approval_id)
    grant = db.get(PeerGrant, grant_id, populate_existing=True)
    if grant is None:
        raise ValueError("Grant unavailable")
    now = _now(db)
    if rotate:
        if approval.current_grant_id != grant.id or grant.revoked_at or grant.expires_at <= now:
            raise ValueError("Only the current active grant can be rotated")
        _recheck(db, approval)
        if grant.skill_scopes != approval.skill_scopes:
            raise ValueError("Grant skill permissions changed; operator reapproval is required")
        approval.issuance_pending = True
        _disable_challenge(approval)
    else:
        if grant.revoked_at is None:
            grant.revoked_at = now
        if approval.current_grant_id == grant.id:
            approval.issuance_pending = False
            _disable_challenge(approval)
    return approval.id


def require_audience(request: Request) -> None:
    if not settings.a2a_database_grants:
        raise HTTPException(404, "Peer enrollment unavailable")
    if request.url.scheme != "https" or f"https://{request.url.netloc}" != settings.api_url:
        raise HTTPException(403, "Peer grants require the configured HTTPS audience")


def _challenge(approval: PeerApproval) -> EnrollmentChallenge:
    fields = dict(
        approval_id=approval.id,
        challenge_id=approval.challenge_id,
        audience=approval.audience,
        caller_root=approval.caller_root,
        controller=approval.controller,
        identity_sequence=approval.identity_sequence,
        recipient_agent_id=approval.recipient_agent_id,
        scopes=approval.scopes,
        expires_at=approval.challenge_expires_at.astimezone(UTC),
    )
    if approval.skill_scopes is not None:
        return SkillEnrollmentChallenge(**fields, version=2, skills=approval.skill_scopes)
    return EnrollmentChallenge(**fields)


@router.post(
    "/{approval_id}/challenge", response_model=SkillEnrollmentChallenge | EnrollmentChallenge
)
def create_challenge(approval_id: UUID, request: Request, db: Session = Depends(get_db)):
    require_audience(request)
    approval = _locked_approval(db, approval_id)
    if not approval.issuance_pending:
        raise HTTPException(403, "Peer issuance unavailable")
    _recheck(db, approval)
    now = _now(db)
    if approval.challenge_expires_at is None or approval.challenge_expires_at <= now:
        approval.challenge_id = uuid4()
        approval.challenge_expires_at = now + CHALLENGE_TTL
    challenge = _challenge(approval)
    db.commit()
    return challenge


@router.post("/{approval_id}/redeem")
def redeem(
    approval_id: UUID,
    payload: Redemption,
    request: Request,
    db: Session = Depends(get_db),
):
    require_audience(request)
    approval = _locked_approval(db, approval_id)
    if (
        not approval.issuance_pending
        or approval.challenge_id != payload.challenge_id
        or approval.challenge_expires_at is None
    ):
        raise HTTPException(403, "Enrollment challenge unavailable")
    _recheck(db, approval)
    now = _now(db)
    if approval.challenge_expires_at <= now:
        raise HTTPException(403, "Enrollment challenge unavailable")
    try:
        valid = verify_identity_signature(
            approval.controller, _challenge(approval).signing_payload(), payload.signature_multibase
        )
    except ValueError:
        valid = False
    if not valid:
        raise HTTPException(403, "Enrollment signature rejected")

    previous_id = approval.current_grant_id
    if previous_id:
        previous = db.get(PeerGrant, previous_id)
        if (
            previous is None
            or previous.revoked_at
            or previous.expires_at <= now
            or previous.skill_scopes != approval.skill_scopes
        ):
            raise HTTPException(403, "The grant being rotated is unavailable")
        db.execute(
            update(PeerGrant)
            .where(
                PeerGrant.approval_id == approval.id,
                PeerGrant.id != previous_id,
                PeerGrant.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        previous.expires_at = min(previous.expires_at, now + ROTATION_OVERLAP)

    typed = approval.skill_scopes is not None
    token = ("a2c_" if typed else "a2a_") + secrets.token_urlsafe(32)
    grant = PeerGrant(
        id=uuid4(),
        approval_id=approval.id,
        token_digest=hash_api_key(token),
        skill_scopes=approval.skill_scopes,
        issued_at=now,
        expires_at=now + timedelta(hours=approval.lifetime_hours),
    )
    db.add(grant)
    approval.current_grant_id = grant.id
    approval.issuance_pending = False
    _disable_challenge(approval)
    fields = dict(
        grant_id=grant.id,
        audience=approval.audience,
        caller_root=approval.caller_root,
        recipient_agent_id=approval.recipient_agent_id,
        scopes=approval.scopes,
        expires_at=grant.expires_at,
        token=token,
    )
    result = (
        SkillIssuedGrant(**fields, version=2, skills=approval.skill_scopes)
        if typed
        else IssuedGrant(**fields)
    )
    db.commit()
    return JSONResponse(
        {**result.model_dump(mode="json"), "token": token}, headers={"Cache-Control": "no-store"}
    )


def authenticate_grant(token_digest: str) -> A2ATaskPeer | None:
    return _authenticate_grant(token_digest, typed=False)


def _authenticate_grant(token_digest: str, *, typed: bool) -> A2ATaskPeer | None:
    try:
        with SessionLocal() as db:
            db.execute(text("SET LOCAL statement_timeout = '2s'"))
            row = db.execute(
                select(
                    PeerGrant.id,
                    PeerGrant.expires_at,
                    PeerGrant.skill_scopes,
                    PeerApproval.caller_root,
                    PeerApproval.recipient_agent_id,
                    PeerApproval.scopes,
                )
                .join(PeerApproval, PeerGrant.approval_id == PeerApproval.id)
                .where(
                    PeerGrant.token_digest == token_digest,
                    PeerGrant.revoked_at.is_(None),
                    PeerGrant.expires_at > func.clock_timestamp(),
                    PeerApproval.audience == settings.api_url,
                    PeerGrant.skill_scopes.is_not(None)
                    if typed
                    else PeerGrant.skill_scopes.is_(None),
                    PeerGrant.skill_scopes.is_not_distinct_from(PeerApproval.skill_scopes),
                )
            ).one_or_none()
            if row is None:
                return None
            return A2ATaskPeer(
                token_sha256=token_digest,
                grant_id=row.id,
                caller_root=row.caller_root,
                recipient_agent_id=row.recipient_agent_id,
                scopes=set(row.scopes),
                skill_scopes=row.skill_scopes,
                expires_at=row.expires_at,
            )
    except (SQLAlchemyError, ValidationError):
        raise HTTPException(503, "Peer authorization is unavailable") from None


def admit_typed_task(raw: bytes, *, bearer: str, caller_root: str, recipient: UUID, audience: str):
    """Internal preflight contract only. No task is created or executed."""
    if (
        not settings.a2a_database_grants
        or audience != settings.api_url
        or not re.fullmatch(r"a2c_[A-Za-z0-9_-]{43}", bearer)
    ):
        raise HTTPException(403, "Typed collaboration is not authorized")
    peer = _authenticate_grant(hash_api_key(bearer), typed=True)
    if (
        peer is None
        or peer.recipient_agent_id != recipient
        or peer.caller_root != caller_root
        or "message/send" not in peer.scopes
    ):
        raise HTTPException(403, "Typed collaboration is not authorized")
    try:
        request, digest = validate_typed_request(raw, now=datetime.now(UTC))
    except ValueError:
        raise HTTPException(422, "Invalid typed collaboration request") from None
    if request.skill not in (peer.skill_scopes or ()):
        raise HTTPException(403, "Typed collaboration is not authorized")
    if (
        request.skill not in settings.collaboration_skills.get(recipient, ())
        or request.deadline > peer.expires_at
    ):
        raise HTTPException(403, "Typed collaboration is not authorized")
    return request, digest
