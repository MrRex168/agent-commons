from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_cryptographic_identity"
down_revision: str | None = "0004_agent_structured_profiles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_cryptographic_identities",
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("public_key_multibase", sa.String(length=128), nullable=False),
        sa.Column("fingerprint", sa.String(length=80), nullable=False),
        sa.Column(
            "verified_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"]),
        sa.PrimaryKeyConstraint("agent_id"),
        sa.UniqueConstraint("public_key_multibase"),
        sa.UniqueConstraint("fingerprint"),
    )
    op.create_index(
        "ix_agent_cryptographic_identities_public_key_multibase",
        "agent_cryptographic_identities",
        ["public_key_multibase"],
        unique=True,
    )
    op.create_index(
        "ix_agent_cryptographic_identities_fingerprint",
        "agent_cryptographic_identities",
        ["fingerprint"],
        unique=True,
    )

    op.create_table(
        "agent_identity_challenges",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("public_key_multibase", sa.String(length=128), nullable=False),
        sa.Column("fingerprint", sa.String(length=80), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_agent_identity_challenges_agent_id",
        "agent_identity_challenges",
        ["agent_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_agent_identity_challenges_agent_id", table_name="agent_identity_challenges")
    op.drop_table("agent_identity_challenges")
    op.drop_index(
        "ix_agent_cryptographic_identities_fingerprint",
        table_name="agent_cryptographic_identities",
    )
    op.drop_index(
        "ix_agent_cryptographic_identities_public_key_multibase",
        table_name="agent_cryptographic_identities",
    )
    op.drop_table("agent_cryptographic_identities")
