from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_cross_instance_migration"
down_revision: str | None = "0005_cryptographic_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_migration_challenges",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("public_key_multibase", sa.String(length=128), nullable=False),
        sa.Column("fingerprint", sa.String(length=80), nullable=False),
        sa.Column("requested_name", sa.String(length=80), nullable=False),
        sa.Column("envelope_digest", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_agent_migration_challenges_fingerprint",
        "agent_migration_challenges",
        ["fingerprint"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_agent_migration_challenges_fingerprint",
        table_name="agent_migration_challenges",
    )
    op.drop_table("agent_migration_challenges")
