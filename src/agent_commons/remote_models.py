import uuid
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import BigInteger, Boolean, DateTime, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from agent_commons.db import Base

EndpointStatus = Literal["non_sovereign", "pending", "authorized", "expired"]


class RemoteAgentReference(Base):
    __tablename__ = "remote_agent_references"
    __table_args__ = (Index("ix_remote_discovery_name_id", "name", "id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    card_url: Mapped[str] = mapped_column(
        String(500), unique=True, index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    a2a_url: Mapped[str] = mapped_column(String(500), nullable=False)
    preferred_transport: Mapped[str] = mapped_column(String(32), nullable=False)
    root_fingerprint: Mapped[str | None] = mapped_column(
        String(80), unique=True, index=True
    )
    current_controller_public_key: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    identity_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    identity_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    location_sequence: Mapped[int | None] = mapped_column(BigInteger)
    location_digest: Mapped[str | None] = mapped_column(String(64))
    location_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lineage: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    card_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    resolved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def endpoint_status(self, now: datetime | None = None) -> EndpointStatus:
        if self.root_fingerprint is None and not self.identity_verified:
            return "non_sovereign"
        if not self.identity_verified or self.location_expires_at is None:
            return "pending"
        if self.location_expires_at <= (now if now is not None else datetime.now(UTC)):
            return "expired"
        return "authorized"
