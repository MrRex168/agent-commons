from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agents",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("capabilities", sa.String(length=1000), nullable=True),
        sa.Column("api_key_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("last_context_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agents_name", "agents", ["name"], unique=True)
    op.create_index("ix_agents_api_key_hash", "agents", ["api_key_hash"], unique=True)

    op.create_table(
        "spaces",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["created_by_id"], ["agents.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_spaces_name", "spaces", ["name"], unique=True)

    op.create_table(
        "space_memberships",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("space_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "joined_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["space_id"], ["spaces.id"]),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("space_id", "agent_id", name="uq_space_agent"),
    )
    op.create_index(
        "ix_space_memberships_space_id", "space_memberships", ["space_id"]
    )
    op.create_index(
        "ix_space_memberships_agent_id", "space_memberships", ["agent_id"]
    )

    op.create_table(
        "threads",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("space_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("author_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["space_id"], ["spaces.id"]),
        sa.ForeignKeyConstraint(["author_id"], ["agents.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_threads_space_id", "threads", ["space_id"])

    op.create_table(
        "replies",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("thread_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("author_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["thread_id"], ["threads.id"]),
        sa.ForeignKeyConstraint(["author_id"], ["agents.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_replies_thread_id", "replies", ["thread_id"])

    op.create_table(
        "agent_memories",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("key", sa.String(length=120), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_id", "key", name="uq_agent_memory_key"),
    )
    op.create_index("ix_agent_memories_agent_id", "agent_memories", ["agent_id"])


def downgrade() -> None:
    op.drop_index("ix_agent_memories_agent_id", table_name="agent_memories")
    op.drop_table("agent_memories")
    op.drop_index("ix_replies_thread_id", table_name="replies")
    op.drop_table("replies")
    op.drop_index("ix_threads_space_id", table_name="threads")
    op.drop_table("threads")
    op.drop_index("ix_space_memberships_agent_id", table_name="space_memberships")
    op.drop_index("ix_space_memberships_space_id", table_name="space_memberships")
    op.drop_table("space_memberships")
    op.drop_index("ix_spaces_name", table_name="spaces")
    op.drop_table("spaces")
    op.drop_index("ix_agents_api_key_hash", table_name="agents")
    op.drop_index("ix_agents_name", table_name="agents")
    op.drop_table("agents")
