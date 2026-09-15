import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from agent_commons.db import Base


class RemoteAgentRelationship(Base):
    __tablename__ = "remote_agent_relationships"
    __table_args__ = (
        UniqueConstraint(
            "agent_id",
            "remote_reference_id",
            "kind",
            name="uq_agent_remote_relationship_kind",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agents.id"), nullable=False, index=True
    )
    remote_reference_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("remote_agent_references.id"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="follow")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
