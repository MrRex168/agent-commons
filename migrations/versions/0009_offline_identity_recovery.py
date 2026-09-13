"""offline identity recovery

Revision ID: 0009_offline_identity_recovery
Revises: 0008_state_freshness
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009_offline_identity_recovery"
down_revision = "0008_state_freshness"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_recovery_policies",
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("recovery_public_key_multibase", sa.String(length=128), nullable=False),
        sa.Column("recovery_fingerprint", sa.String(length=80), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("statement_payload", sa.Text(), nullable=False),
        sa.Column("active_signature_multibase", sa.String(length=256), nullable=False),
        sa.Column("recovery_signature_multibase", sa.String(length=256), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"]),
        sa.PrimaryKeyConstraint("agent_id"),
    )
    op.create_index(
        "ix_agent_recovery_policies_recovery_fingerprint",
        "agent_recovery_policies",
        ["recovery_fingerprint"],
        unique=False,
    )

    op.create_table(
        "agent_recovery_policy_challenges",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("root_fingerprint", sa.String(length=80), nullable=False),
        sa.Column("current_public_key_multibase", sa.String(length=128), nullable=False),
        sa.Column(
            "proposed_recovery_public_key_multibase",
            sa.String(length=128),
            nullable=False,
        ),
        sa.Column("identity_sequence", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_agent_recovery_policy_challenges_agent_id",
        "agent_recovery_policy_challenges",
        ["agent_id"],
        unique=False,
    )

    op.create_table(
        "agent_recovery_challenges",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("root_fingerprint", sa.String(length=80), nullable=False),
        sa.Column("recovery_public_key_multibase", sa.String(length=128), nullable=False),
        sa.Column("previous_public_key_multibase", sa.String(length=128), nullable=False),
        sa.Column("new_public_key_multibase", sa.String(length=128), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("policy_revision", sa.Integer(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_agent_recovery_challenges_agent_id",
        "agent_recovery_challenges",
        ["agent_id"],
        unique=False,
    )

    op.create_table(
        "agent_recovery_transitions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("policy_revision", sa.Integer(), nullable=False),
        sa.Column("previous_public_key_multibase", sa.String(length=128), nullable=False),
        sa.Column("new_public_key_multibase", sa.String(length=128), nullable=False),
        sa.Column("recovery_public_key_multibase", sa.String(length=128), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("recovery_signature_multibase", sa.String(length=256), nullable=False),
        sa.Column("new_signature_multibase", sa.String(length=256), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "agent_id",
            "sequence",
            name="uq_agent_recovery_transition_sequence",
        ),
    )
    op.create_index(
        "ix_agent_recovery_transitions_agent_id",
        "agent_recovery_transitions",
        ["agent_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_agent_recovery_transitions_agent_id",
        table_name="agent_recovery_transitions",
    )
    op.drop_table("agent_recovery_transitions")
    op.drop_index(
        "ix_agent_recovery_challenges_agent_id",
        table_name="agent_recovery_challenges",
    )
    op.drop_table("agent_recovery_challenges")
    op.drop_index(
        "ix_agent_recovery_policy_challenges_agent_id",
        table_name="agent_recovery_policy_challenges",
    )
    op.drop_table("agent_recovery_policy_challenges")
    op.drop_index(
        "ix_agent_recovery_policies_recovery_fingerprint",
        table_name="agent_recovery_policies",
    )
    op.drop_table("agent_recovery_policies")
