"""Index owner-scoped native A2A tasks by last update."""

import sqlalchemy as sa
from alembic import op

revision = "0021_a2a_task_pagination"
down_revision = "0020_collaboration_history"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "a2a_tasks",
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.clock_timestamp()
        ),
    )
    # Old task status changes have no timestamp. Use original retention-based creation.
    op.execute("UPDATE a2a_tasks SET updated_at = expires_at - interval '1 day'")
    op.alter_column("a2a_tasks", "updated_at", nullable=False)
    op.create_index(
        "ix_a2a_task_owner_updated",
        "a2a_tasks",
        ["caller_root", "recipient_agent_id", "updated_at", "id"],
    )


def downgrade():
    op.drop_index("ix_a2a_task_owner_updated", table_name="a2a_tasks")
    op.drop_column("a2a_tasks", "updated_at")
