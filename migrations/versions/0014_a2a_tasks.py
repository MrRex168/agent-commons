"""Persist authenticated A2A tasks.

Revision ID: 0014_a2a_tasks
Revises: 0013_remote_endpoint_auth
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0014_a2a_tasks"
down_revision = "0013_remote_endpoint_auth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "a2a_tasks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("caller_root", sa.String(71), nullable=False),
        sa.Column("recipient_agent_id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.String(128), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["recipient_agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "caller_root", "recipient_agent_id", "message_id", name="uq_a2a_task_peer_message"
        ),
    )
    op.create_index("ix_a2a_tasks_expires_at", "a2a_tasks", ["expires_at"])


def downgrade() -> None:
    op.drop_table("a2a_tasks")
