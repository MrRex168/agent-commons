from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from agent_commons.db import Base


class PeerApproval(Base):
    __tablename__ = "peer_approvals"
    __table_args__ = (
        sa.UniqueConstraint("caller_root", "recipient_agent_id"),
        sa.CheckConstraint("lifetime_hours BETWEEN 1 AND 24", name="ck_peer_grant_lifetime"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    reference_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("remote_agent_references.id", ondelete="CASCADE")
    )
    caller_root: Mapped[str] = mapped_column(sa.String(71))
    controller: Mapped[str] = mapped_column(sa.String(128))
    identity_sequence: Mapped[int]
    recipient_agent_id: Mapped[UUID] = mapped_column(sa.ForeignKey("agents.id", ondelete="CASCADE"))
    scopes: Mapped[list[str]] = mapped_column(JSONB)
    skill_scopes: Mapped[list[str] | None] = mapped_column(JSONB(none_as_null=True))
    audience: Mapped[str] = mapped_column(sa.String(500))
    lifetime_hours: Mapped[int]
    issuance_pending: Mapped[bool]
    current_grant_id: Mapped[UUID | None]
    challenge_id: Mapped[UUID | None]
    challenge_expires_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class PeerGrant(Base):
    __tablename__ = "peer_grants"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    approval_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("peer_approvals.id", ondelete="CASCADE"), index=True
    )
    token_digest: Mapped[str] = mapped_column(sa.String(64), unique=True)
    skill_scopes: Mapped[list[str] | None] = mapped_column(JSONB(none_as_null=True))
    issued_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    rate_next_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
