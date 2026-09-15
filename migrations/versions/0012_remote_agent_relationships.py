"""remote agent relationships

Revision ID: 0012_remote_agent_relationships
Revises: 0011_remote_agent_references
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0012_remote_agent_relationships"
down_revision = "0011_remote_agent_references"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "remote_agent_relationships",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("remote_reference_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"]),
        sa.ForeignKeyConstraint(
            ["remote_reference_id"], ["remote_agent_references.id"]
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "agent_id",
            "remote_reference_id",
            "kind",
            name="uq_agent_remote_relationship_kind",
        ),
    )
    op.create_index(
        "ix_remote_agent_relationships_agent_id",
        "remote_agent_relationships",
        ["agent_id"],
    )
    op.create_index(
        "ix_remote_agent_relationships_remote_reference_id",
        "remote_agent_relationships",
        ["remote_reference_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_remote_agent_relationships_remote_reference_id",
        table_name="remote_agent_relationships",
    )
    op.drop_index(
        "ix_remote_agent_relationships_agent_id",
        table_name="remote_agent_relationships",
    )
    op.drop_table("remote_agent_relationships")
