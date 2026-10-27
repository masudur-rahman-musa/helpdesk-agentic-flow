"""users gateway user id

Revision ID: a7d3f19c5e20
Revises: e2b7c4a91f06
Create Date: 2026-10-27 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7d3f19c5e20"
down_revision: str | Sequence[str] | None = "e2b7c4a91f06"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Empty on every old row; the chat fills it on a user's first model call.
# The check is pinned by test_the_database_refuses_a_bad_id.


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("users", sa.Column("gateway_user_id", sa.String(length=34), nullable=True))
    op.create_unique_constraint(op.f("uq_users_gateway_user_id"), "users", ["gateway_user_id"])
    op.create_check_constraint(
        op.f("ck_users_gateway_user_id_shape"),
        "users",
        "gateway_user_id ~ '^u_[0-9a-f]{32}$'",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(op.f("ck_users_gateway_user_id_shape"), "users", type_="check")
    op.drop_constraint(op.f("uq_users_gateway_user_id"), "users", type_="unique")
    op.drop_column("users", "gateway_user_id")
