"""Store accepted sovereign endpoint authorizations.

Revision ID: 0013_remote_endpoint_auth
Revises: 0012_remote_agent_relationships
"""

import sqlalchemy as sa
from alembic import op

revision = "0013_remote_endpoint_auth"
down_revision = "0012_remote_agent_relationships"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Leave existing proofs unset. A cached lineage never authorizes a location.
    op.add_column("remote_agent_references", sa.Column("location_sequence", sa.BigInteger()))
    op.add_column("remote_agent_references", sa.Column("location_digest", sa.String(64)))
    op.add_column(
        "remote_agent_references", sa.Column("location_expires_at", sa.DateTime(timezone=True))
    )


def downgrade() -> None:
    op.drop_column("remote_agent_references", "location_expires_at")
    op.drop_column("remote_agent_references", "location_digest")
    op.drop_column("remote_agent_references", "location_sequence")
