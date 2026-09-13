import uuid

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from agent_commons.db import Base


class AgentStateSequence(Base):
    __tablename__ = "agent_state_sequences"

    agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agents.id"), primary_key=True, nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class ObservedStateFreshness(Base):
    __tablename__ = "observed_state_freshness"

    root_fingerprint: Mapped[str] = mapped_column(String(80), primary_key=True)
    highest_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
