"""add conversation tables

Revision ID: 9f4c2a7b1de0
Revises: cad6e13337e7
Create Date: 2026-09-08 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "9f4c2a7b1de0"
down_revision: str | Sequence[str] | None = "cad6e13337e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "sessions",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_sessions_tenant_id_tenants")
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_sessions_user_id_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sessions")),
    )
    op.create_index(
        "ix_sessions_tenant_id_user_id", "sessions", ["tenant_id", "user_id"], unique=False
    )
    op.create_table(
        "messages",
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.String(length=4000), nullable=False),
        sa.Column("tool_calls", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("citations", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        # Identity, not a caller-supplied integer: it is the only total order
        # the history window has, because `now()` is transaction time and
        # every message one turn writes shares `created_at`.
        sa.Column("ordinal", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["session_id"], ["sessions.id"], name=op.f("fk_messages_session_id_sessions")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_messages_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
        sa.UniqueConstraint("ordinal", name=op.f("uq_messages_ordinal")),
    )
    op.create_index(
        "ix_messages_tenant_id_session_id_ordinal",
        "messages",
        ["tenant_id", "session_id", "ordinal"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_messages_tenant_id_session_id_ordinal", table_name="messages")
    op.drop_table("messages")
    op.drop_index("ix_sessions_tenant_id_user_id", table_name="sessions")
    op.drop_table("sessions")
