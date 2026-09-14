"""immutable recovery policy history

Revision ID: 0010_recovery_policy_history
Revises: 0009_offline_identity_recovery
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010_recovery_policy_history"
down_revision = "0009_offline_identity_recovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_recovery_policy_statements",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("identity_sequence", sa.Integer(), nullable=False),
        sa.Column("current_public_key_multibase", sa.String(length=128), nullable=False),
        sa.Column("recovery_public_key_multibase", sa.String(length=128), nullable=False),
        sa.Column("recovery_fingerprint", sa.String(length=80), nullable=False),
        sa.Column("statement_payload", sa.Text(), nullable=False),
        sa.Column("active_signature_multibase", sa.String(length=256), nullable=False),
        sa.Column("recovery_signature_multibase", sa.String(length=256), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "agent_id",
            "revision",
            name="uq_agent_recovery_policy_statement_revision",
        ),
    )
    op.create_index(
        "ix_agent_recovery_policy_statements_agent_id",
        "agent_recovery_policy_statements",
        ["agent_id"],
        unique=False,
    )

    # Older installations only retain the current policy. Preserve that evidence
    # as the first immutable history row available after this migration.
    op.execute(
        sa.text(
            """
            INSERT INTO agent_recovery_policy_statements (
                id,
                agent_id,
                revision,
                identity_sequence,
                current_public_key_multibase,
                recovery_public_key_multibase,
                recovery_fingerprint,
                statement_payload,
                active_signature_multibase,
                recovery_signature_multibase,
                created_at
            )
            SELECT
                gen_random_uuid(),
                p.agent_id,
                p.revision,
                CAST(
                    split_part(
                        split_part(p.statement_payload, 'identity_sequence:', 2),
                        E'\\n',
                        1
                    ) AS INTEGER
                ),
                split_part(
                    split_part(p.statement_payload, 'current_key:', 2),
                    E'\\n',
                    1
                ),
                p.recovery_public_key_multibase,
                p.recovery_fingerprint,
                p.statement_payload,
                p.active_signature_multibase,
                p.recovery_signature_multibase,
                p.updated_at
            FROM agent_recovery_policies p
            """
        )
    )


def downgrade() -> None:
    op.drop_index(
        "ix_agent_recovery_policy_statements_agent_id",
        table_name="agent_recovery_policy_statements",
    )
    op.drop_table("agent_recovery_policy_statements")
