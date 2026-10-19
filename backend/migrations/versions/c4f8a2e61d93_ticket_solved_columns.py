"""ticket solved columns

Revision ID: c4f8a2e61d93
Revises: b7e3d91a4c2f
Create Date: 2026-10-19 14:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4f8a2e61d93"
down_revision: str | Sequence[str] | None = "b7e3d91a4c2f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Both columns stay empty while a ticket is open, so old rows need no backfill.


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("tickets", sa.Column("solved_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("tickets", sa.Column("solved_by_user_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_tickets_solved_by_user_id_users"),
        "tickets",
        "users",
        ["solved_by_user_id"],
        ["id"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(op.f("fk_tickets_solved_by_user_id_users"), "tickets", type_="foreignkey")
    op.drop_column("tickets", "solved_by_user_id")
    op.drop_column("tickets", "solved_at")
