from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from agent_commons.db import Base


class CollaborationHistory(Base):
    __tablename__ = "collaboration_history"
    __table_args__ = (
        UniqueConstraint("owner_id", "execution_origin", "task_id"),
        Index("ix_collaboration_owner_created", "owner_id", "submitted_at", "id"),
        Index("ix_collaboration_history_expiry", "expires_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"))
    caller_root: Mapped[str] = mapped_column(String(71))
    peer_root: Mapped[str] = mapped_column(String(71))
    execution_origin: Mapped[str] = mapped_column(String(500))
    endpoint: Mapped[str] = mapped_column(String(500))
    task_id: Mapped[UUID]
    skill: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(16))
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    artifact_digest: Mapped[str | None] = mapped_column(String(64))
    identity_sequence: Mapped[int]
    location_sequence: Mapped[int] = mapped_column(BigInteger)
    location_digest: Mapped[str] = mapped_column(String(64))
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
