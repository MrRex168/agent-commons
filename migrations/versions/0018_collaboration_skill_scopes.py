"""Add opt-in collaboration skill scopes without upgrading existing grants."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0018_collaboration_skill_scopes"
down_revision = "0017_preview_capacity"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("peer_approvals", "peer_grants"):
        op.add_column(table, sa.Column("skill_scopes", postgresql.JSONB(), nullable=True))

    op.execute("""
        CREATE FUNCTION enforce_peer_grant_skill_scopes() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM peer_approvals
                WHERE id = NEW.approval_id
                  AND skill_scopes IS NOT DISTINCT FROM NEW.skill_scopes
            ) THEN
                RAISE EXCEPTION 'Grant skill permissions must match the approval';
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER peer_grant_skill_scopes
        BEFORE INSERT OR UPDATE OF skill_scopes ON peer_grants
        FOR EACH ROW EXECUTE FUNCTION enforce_peer_grant_skill_scopes()
    """)


def downgrade():
    op.execute("DROP TRIGGER peer_grant_skill_scopes ON peer_grants")
    op.execute("DROP FUNCTION enforce_peer_grant_skill_scopes()")

    # A v1 server would interpret a typed approval/grant as Text Statistics.
    # Remove typed enrollments before dropping the discriminator.
    op.execute("DELETE FROM peer_grants WHERE skill_scopes IS NOT NULL")
    op.execute("DELETE FROM peer_approvals WHERE skill_scopes IS NOT NULL")
    for table in ("peer_grants", "peer_approvals"):
        op.drop_column(table, "skill_scopes")
