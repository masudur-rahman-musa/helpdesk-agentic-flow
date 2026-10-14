"""idempotency keys

Revision ID: 873703211e26
Revises: b356a38098b6
Create Date: 2026-09-04 06:57:38.462132

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "873703211e26"
down_revision: str | Sequence[str] | None = "b356a38098b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "idempotency_keys",
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("tool_name", sa.String(length=64), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("response", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
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
            ["tenant_id"], ["tenants.id"], name=op.f("fk_idempotency_keys_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_idempotency_keys")),
    )
    op.create_index(
        "ix_idempotency_keys_expires_at", "idempotency_keys", ["expires_at"], unique=False
    )
    # The backstop behind the guard's advisory lock: two rows for one key
    # must be impossible even if the lock is ever bypassed.
    op.create_index(
        "uq_idempotency_keys_tenant_id_key", "idempotency_keys", ["tenant_id", "key"], unique=True
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("uq_idempotency_keys_tenant_id_key", table_name="idempotency_keys")
    op.drop_index("ix_idempotency_keys_expires_at", table_name="idempotency_keys")
    op.drop_table("idempotency_keys")
