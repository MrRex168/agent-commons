from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from agent_commons.db import Base


class A2ATask(Base):
    __tablename__ = "a2a_tasks"
    __table_args__ = (
        Index("ix_a2a_task_claim", "state", "deadline", "id"),
        UniqueConstraint(
            "caller_root", "recipient_agent_id", "message_id", name="uq_a2a_task_peer_message"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    caller_root: Mapped[str] = mapped_column(String(71))
    recipient_agent_id: Mapped[UUID] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"))
    message_id: Mapped[str] = mapped_column(String(128))
    request_digest: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSONB)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    skill: Mapped[str | None] = mapped_column(String(80))
    state: Mapped[str | None] = mapped_column(String(16))
    task_input: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt: Mapped[UUID | None]
    grant_id: Mapped[UUID | None]
