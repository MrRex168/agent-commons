"""Operator-issued registration invitations."""

import re
import secrets
from datetime import datetime, timedelta

from fastapi import HTTPException, Request
from sqlalchemy import DateTime, String, func, select, update
from sqlalchemy.orm import Mapped, Session, mapped_column

from agent_commons.config import settings
from agent_commons.db import Base
from agent_commons.security import REGISTRATION_INVITATION_HEADER, hash_api_key

TOKEN_PATTERN = re.compile(r"aci_[A-Za-z0-9_-]{43}")
INVITATION_TTL = timedelta(minutes=15)


class RegistrationInvitation(Base):
    __tablename__ = "registration_invitations"

    token_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


def issue_invitation(db: Session) -> str:
    token = "aci_" + secrets.token_urlsafe(32)
    db.add(
        RegistrationInvitation(
            token_digest=hash_api_key(token),
            expires_at=db.scalar(select(func.clock_timestamp())) + INVITATION_TTL,
        )
    )
    return token


def require_registration_admission(db: Session, request: Request) -> None:
    if settings.registration_admission == "open":
        return
    tokens = request.headers.getlist(REGISTRATION_INVITATION_HEADER)
    if len(tokens) == 1 and TOKEN_PATTERN.fullmatch(tokens[0]):
        now = func.clock_timestamp()
        consumed = db.scalar(
            update(RegistrationInvitation)
            .where(
                RegistrationInvitation.token_digest == hash_api_key(tokens[0]),
                RegistrationInvitation.consumed_at.is_(None),
                RegistrationInvitation.expires_at > now,
            )
            .values(consumed_at=now)
            .returning(RegistrationInvitation.token_digest)
        )
        if consumed is not None:
            return
    raise HTTPException(403, "A valid registration invitation is required")
