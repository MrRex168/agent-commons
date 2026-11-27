"""Reserve bounded typed tasks in the existing task table."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0019_typed_task_execution"
down_revision = "0018_collaboration_skill_scopes"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("a2a_tasks", sa.Column("skill", sa.String(80)))
    op.add_column("a2a_tasks", sa.Column("state", sa.String(16)))
    op.add_column("a2a_tasks", sa.Column("task_input", JSONB()))
    op.add_column("a2a_tasks", sa.Column("deadline", sa.DateTime(timezone=True)))
    op.add_column("a2a_tasks", sa.Column("attempt", sa.Uuid()))
    op.add_column("a2a_tasks", sa.Column("grant_id", sa.Uuid()))
    op.create_index("ix_a2a_task_claim", "a2a_tasks", ["state", "deadline", "id"])


def downgrade():
    op.execute("DELETE FROM a2a_tasks WHERE skill IS NOT NULL")
    op.drop_index("ix_a2a_task_claim", table_name="a2a_tasks")
    for column in ("grant_id", "attempt", "deadline", "task_input", "state", "skill"):
        op.drop_column("a2a_tasks", column)
