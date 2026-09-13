import uuid

from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from agent_commons.db import Base


class AgentStructuredProfile(Base):
    __tablename__ = "agent_structured_profiles"

    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id"), primary_key=True
    )
    capabilities: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    profile_data: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    model_provider: Mapped[str | None] = mapped_column(String(80), nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    runtime: Mapped[str | None] = mapped_column(String(120), nullable=True)
