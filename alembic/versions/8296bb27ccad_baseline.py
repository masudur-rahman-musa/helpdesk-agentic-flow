"""baseline

Revision ID: 8296bb27ccad
Revises:
Create Date: 2026-09-03 02:59:59.469095

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "8296bb27ccad"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
