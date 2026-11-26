"""Add opt-in peer enrollment approvals and revocable grants."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0016_peer_grants"
down_revision = "0015_registration_invitations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "peer_approvals",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("reference_id", sa.Uuid(), nullable=False),
        sa.Column("caller_root", sa.String(71), nullable=False),
        sa.Column("controller", sa.String(128), nullable=False),
        sa.Column("identity_sequence", sa.Integer(), nullable=False),
        sa.Column("recipient_agent_id", sa.Uuid(), nullable=False),
        sa.Column("scopes", JSONB(), nullable=False),
        sa.Column("audience", sa.String(500), nullable=False),
        sa.Column("lifetime_hours", sa.Integer(), nullable=False),
        sa.Column("issuance_pending", sa.Boolean(), nullable=False),
        sa.Column("current_grant_id", sa.Uuid()),
        sa.Column("challenge_id", sa.Uuid()),
        sa.Column("challenge_expires_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("caller_root", "recipient_agent_id"),
        sa.CheckConstraint("lifetime_hours BETWEEN 1 AND 24", name="ck_peer_grant_lifetime"),
        sa.ForeignKeyConstraint(
            ["reference_id"], ["remote_agent_references.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["recipient_agent_id"], ["agents.id"], ondelete="CASCADE"),
    )
    op.create_table(
        "peer_grants",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("approval_id", sa.Uuid(), nullable=False),
        sa.Column("token_digest", sa.String(64), nullable=False, unique=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["approval_id"], ["peer_approvals.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_peer_grants_approval_id", "peer_grants", ["approval_id"])


def downgrade() -> None:
    op.drop_table("peer_grants")
    op.drop_table("peer_approvals")
