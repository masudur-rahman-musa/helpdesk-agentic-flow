"""systems about

Revision ID: a9d3e5b17c40
Revises: c4f8a2e61d93
Create Date: 2026-10-20 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a9d3e5b17c40"
down_revision: str | Sequence[str] | None = "c4f8a2e61d93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Old rows read the empty default until the seed fills in their phrases; the
# migration must not name system slugs, so it holds no phrases of its own.


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "systems",
        sa.Column("about", sa.String(length=512), server_default="", nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("systems", "about")
