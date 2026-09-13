import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from agent_commons.db import Base


class AgentRecoveryPolicy(Base):
    __tablename__ = "agent_recovery_policies"

    agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agents.id"), primary_key=True, nullable=False
    )
    recovery_public_key_multibase: Mapped[str] = mapped_column(String(128), nullable=False)
    recovery_fingerprint: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    statement_payload: Mapped[str] = mapped_column(Text, nullable=False)
    active_signature_multibase: Mapped[str] = mapped_column(String(256), nullable=False)
    recovery_signature_multibase: Mapped[str] = mapped_column(String(256), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AgentRecoveryPolicyChallenge(Base):
    __tablename__ = "agent_recovery_policy_challenges"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agents.id"), nullable=False, index=True
    )
    root_fingerprint: Mapped[str] = mapped_column(String(80), nullable=False)
    current_public_key_multibase: Mapped[str] = mapped_column(String(128), nullable=False)
    proposed_recovery_public_key_multibase: Mapped[str] = mapped_column(
        String(128), nullable=False
    )
    identity_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentRecoveryChallenge(Base):
    __tablename__ = "agent_recovery_challenges"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agents.id"), nullable=False, index=True
    )
    root_fingerprint: Mapped[str] = mapped_column(String(80), nullable=False)
    recovery_public_key_multibase: Mapped[str] = mapped_column(String(128), nullable=False)
    previous_public_key_multibase: Mapped[str] = mapped_column(String(128), nullable=False)
    new_public_key_multibase: Mapped[str] = mapped_column(String(128), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentRecoveryTransition(Base):
    __tablename__ = "agent_recovery_transitions"
    __table_args__ = (
        UniqueConstraint(
            "agent_id",
            "sequence",
            name="uq_agent_recovery_transition_sequence",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agents.id"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_public_key_multibase: Mapped[str] = mapped_column(String(128), nullable=False)
    new_public_key_multibase: Mapped[str] = mapped_column(String(128), nullable=False)
    recovery_public_key_multibase: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    recovery_signature_multibase: Mapped[str] = mapped_column(String(256), nullable=False)
    new_signature_multibase: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
