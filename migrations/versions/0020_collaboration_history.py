"""Add caller-owned outbound collaboration observations."""

import sqlalchemy as sa
from alembic import op

revision = "0020_collaboration_history"
down_revision = "0019_typed_task_execution"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "collaboration_history",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "owner_id", sa.Uuid(), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("caller_root", sa.String(71), nullable=False),
        sa.Column("peer_root", sa.String(71), nullable=False),
        sa.Column("execution_origin", sa.String(500), nullable=False),
        sa.Column("endpoint", sa.String(500), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("skill", sa.String(80), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("artifact_digest", sa.String(64)),
        sa.Column("identity_sequence", sa.Integer(), nullable=False),
        sa.Column("location_sequence", sa.BigInteger(), nullable=False),
        sa.Column("location_digest", sa.String(64), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("owner_id", "execution_origin", "task_id"),
    )
    op.create_index(
        "ix_collaboration_owner_created",
        "collaboration_history",
        ["owner_id", "submitted_at", "id"],
    )
    op.create_index("ix_collaboration_history_expiry", "collaboration_history", ["expires_at"])


def downgrade():
    op.drop_table("collaboration_history")
