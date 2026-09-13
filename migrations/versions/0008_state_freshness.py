"""state freshness and anti-rollback

Revision ID: 0008_state_freshness
Revises: 0007_planned_key_rotation
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008_state_freshness"
down_revision = "0007_planned_key_rotation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_state_sequences",
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"]),
        sa.PrimaryKeyConstraint("agent_id"),
    )
    op.create_table(
        "observed_state_freshness",
        sa.Column("root_fingerprint", sa.String(length=80), nullable=False),
        sa.Column("highest_sequence", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("root_fingerprint"),
    )


def downgrade() -> None:
    op.drop_table("observed_state_freshness")
    op.drop_table("agent_state_sequences")
