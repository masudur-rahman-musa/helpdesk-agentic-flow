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

# An earlier change decision F. `models/conversation.py` declares the attribute as
# `Mapped[dict[str, Any]]`, which says the value is never None, while the
# conversation revision created the column `nullable=True`. The model is the
# side every reader is written against, so the column is brought up to it
# rather than the annotation widened to `| None`.
#
# The backfill is what makes the ALTER apply to an existing database: a row
# written before this revision may hold NULL, and `SET NOT NULL` on a column
# holding one fails outright. `{}` is the model's own `default=dict`, so a
# backfilled row reads exactly as a row written today.


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
