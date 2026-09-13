"""planned key rotation

Revision ID: 0007_planned_key_rotation
Revises: 0006_agent_migration_challenges
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007_planned_key_rotation"
down_revision = "0006_agent_migration_challenges"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_identity_key_states",
        sa.Column(
            "agent_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "root_public_key_multibase",
            sa.String(length=128),
            nullable=False,
        ),
        sa.Column(
            "root_fingerprint",
            sa.String(length=80),
            nullable=False,
        ),
        sa.Column(
            "current_public_key_multibase",
            sa.String(length=128),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"]),
        sa.PrimaryKeyConstraint("agent_id"),
    )
    op.create_index(
        "ix_agent_identity_key_states_root_fingerprint",
        "agent_identity_key_states",
        ["root_fingerprint"],
        unique=False,
    )

    op.create_table(
        "agent_key_rotation_challenges",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "agent_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("root_fingerprint", sa.String(length=80), nullable=False),
        sa.Column(
            "previous_public_key_multibase",
            sa.String(length=128),
            nullable=False,
        ),
        sa.Column(
            "new_public_key_multibase",
            sa.String(length=128),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column(
            "issued_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "consumed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_agent_key_rotation_challenges_agent_id",
        "agent_key_rotation_challenges",
        ["agent_id"],
        unique=False,
    )

    op.create_table(
        "agent_key_transitions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "agent_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column(
            "previous_public_key_multibase",
            sa.String(length=128),
            nullable=False,
        ),
        sa.Column(
            "new_public_key_multibase",
            sa.String(length=128),
            nullable=False,
        ),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column(
            "previous_signature_multibase",
            sa.String(length=256),
            nullable=False,
        ),
        sa.Column(
            "new_signature_multibase",
            sa.String(length=256),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "agent_id",
            "sequence",
            name="uq_agent_key_transition_sequence",
        ),
    )
    op.create_index(
        "ix_agent_key_transitions_agent_id",
        "agent_key_transitions",
        ["agent_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_agent_key_transitions_agent_id",
        table_name="agent_key_transitions",
    )
    op.drop_table("agent_key_transitions")
    op.drop_index(
        "ix_agent_key_rotation_challenges_agent_id",
        table_name="agent_key_rotation_challenges",
    )
    op.drop_table("agent_key_rotation_challenges")
    op.drop_index(
        "ix_agent_identity_key_states_root_fingerprint",
        table_name="agent_identity_key_states",
    )
    op.drop_table("agent_identity_key_states")
