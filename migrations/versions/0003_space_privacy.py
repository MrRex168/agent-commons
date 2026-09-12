from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_space_privacy"
down_revision: str | None = "0002_notifications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "spaces",
        sa.Column(
            "visibility",
            sa.String(length=20),
            server_default="public",
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("spaces", "visibility")
