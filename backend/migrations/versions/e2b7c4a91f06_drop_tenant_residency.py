"""drop tenant residency

Revision ID: e2b7c4a91f06
Revises: a9d3e5b17c40
Create Date: 2026-10-26 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e2b7c4a91f06"
down_revision: str | Sequence[str] | None = "a9d3e5b17c40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The old values are lost on upgrade; a downgrade fills every row with `eu`.
# The `eu` default stays after a downgrade so code at the older revisions, which
# no longer names the column, can still add a tenant.


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_column("tenants", "residency")


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column(
        "tenants",
        sa.Column("residency", sa.String(length=16), server_default="eu", nullable=False),
    )
