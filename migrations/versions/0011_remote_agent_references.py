"""remote agent references

Revision ID: 0011_remote_agent_references
Revises: 0010_recovery_policy_history
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011_remote_agent_references"
down_revision = "0010_recovery_policy_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "remote_agent_references",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("card_url", sa.String(length=500), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("a2a_url", sa.String(length=500), nullable=False),
        sa.Column("preferred_transport", sa.String(length=32), nullable=False),
        sa.Column("root_fingerprint", sa.String(length=80), nullable=True),
        sa.Column("current_controller_public_key", sa.String(length=128), nullable=True),
        sa.Column("identity_sequence", sa.Integer(), nullable=True),
        sa.Column("identity_verified", sa.Boolean(), nullable=False),
        sa.Column("lineage", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("card_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "resolved_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_remote_agent_references_card_url",
        "remote_agent_references",
        ["card_url"],
        unique=True,
    )
    op.create_index(
        "ix_remote_agent_references_root_fingerprint",
        "remote_agent_references",
        ["root_fingerprint"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_remote_agent_references_root_fingerprint",
        table_name="remote_agent_references",
    )
    op.drop_index(
        "ix_remote_agent_references_card_url",
        table_name="remote_agent_references",
    )
    op.drop_table("remote_agent_references")
