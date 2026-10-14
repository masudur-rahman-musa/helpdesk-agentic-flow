"""add user helpdesk flags

Revision ID: 31ca97109faf
Revises: 873703211e26
Create Date: 2026-09-05 19:30:27.538671

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "31ca97109faf"
down_revision: str | Sequence[str] | None = "873703211e26"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "users",
        sa.Column("password_reset_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("account_unlocked_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("users", "account_unlocked_at")
    op.drop_column("users", "password_reset_requested_at")
