"""Store hashes of single-use registration invitations.

Revision ID: 0015_registration_invitations
Revises: 0014_a2a_tasks
"""

import sqlalchemy as sa
from alembic import op

revision = "0015_registration_invitations"
down_revision = "0014_a2a_tasks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "registration_invitations",
        sa.Column("token_digest", sa.String(64), primary_key=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("registration_invitations")
