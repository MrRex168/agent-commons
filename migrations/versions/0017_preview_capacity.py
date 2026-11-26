"""Add shared preview quota buckets and discovery keyset index."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0017_preview_capacity"
down_revision = "0016_peer_grants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("peer_grants", sa.Column("rate_next_at", sa.DateTime(timezone=True)))
    op.create_table(
        "preview_quotas",
        sa.Column("bucket", sa.String(80), primary_key=True),
        sa.Column("timestamps", postgresql.ARRAY(sa.DateTime(timezone=True)), nullable=False),
    )
    op.create_index("ix_remote_discovery_name_id", "remote_agent_references", ["name", "id"])


def downgrade() -> None:
    op.drop_index("ix_remote_discovery_name_id", table_name="remote_agent_references")
    op.drop_table("preview_quotas")
    op.drop_column("peer_grants", "rate_next_at")
