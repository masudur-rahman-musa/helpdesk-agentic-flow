"""add policy corpus tables

Revision ID: cad6e13337e7
Revises: 248ffc758941
Create Date: 2026-09-07 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "cad6e13337e7"
down_revision: str | Sequence[str] | None = "248ffc758941"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "policy_documents",
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("version", sa.String(length=8), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("source_path", sa.String(length=256), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
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
            ["tenant_id"], ["tenants.id"], name=op.f("fk_policy_documents_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_policy_documents")),
    )
    op.create_index(
        "ix_policy_documents_tenant_id_slug",
        "policy_documents",
        ["tenant_id", "slug"],
        unique=False,
    )
    op.create_table(
        "policy_chunks",
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("heading_path", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("char_start", sa.Integer(), nullable=False),
        sa.Column("char_end", sa.Integer(), nullable=False),
        sa.Column("qdrant_point_id", sa.Uuid(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
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
            ["document_id"],
            ["policy_documents.id"],
            name=op.f("fk_policy_chunks_document_id_policy_documents"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_policy_chunks_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_policy_chunks")),
    )
    op.create_index(
        "ix_policy_chunks_tenant_id_document_id",
        "policy_chunks",
        ["tenant_id", "document_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_policy_chunks_tenant_id_document_id", table_name="policy_chunks")
    op.drop_table("policy_chunks")
    op.drop_index("ix_policy_documents_tenant_id_slug", table_name="policy_documents")
    op.drop_table("policy_documents")
