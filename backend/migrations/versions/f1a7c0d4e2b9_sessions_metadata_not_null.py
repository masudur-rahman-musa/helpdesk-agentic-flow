"""sessions metadata not null

Revision ID: f1a7c0d4e2b9
Revises: d8b2f6c41a97
Create Date: 2026-09-14 18:05:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "f1a7c0d4e2b9"
down_revision: str | Sequence[str] | None = "d8b2f6c41a97"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_NAME = "sessions"
COLUMN_NAME = "metadata"

# The model says this value is never None, but the column was created
# nullable. The column is brought in line with the model. The backfill comes
# first, because `SET NOT NULL` fails on any NULL row, and `{}` matches the
# model's default.


def upgrade() -> None:
    op.execute(
        sa.text(
            f"UPDATE {TABLE_NAME} SET {COLUMN_NAME} = '{{}}'::jsonb WHERE {COLUMN_NAME} IS NULL"
        )
    )
    op.alter_column(
        TABLE_NAME,
        COLUMN_NAME,
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        TABLE_NAME,
        COLUMN_NAME,
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        nullable=True,
    )
