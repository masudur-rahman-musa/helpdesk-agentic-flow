"""add access reviews

Revision ID: 248ffc758941
Revises: 1923c1ae5a60
Create Date: 2026-09-07 02:40:53.670584

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "248ffc758941"
down_revision: str | Sequence[str] | None = "1923c1ae5a60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "access_reviews",
        sa.Column("reviewer_user_id", sa.Uuid(), nullable=False),
        sa.Column("department_id", sa.Uuid(), nullable=True),
        sa.Column("system_id", sa.Uuid(), nullable=True),
        sa.Column("period", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint(
            "(department_id IS NULL) <> (system_id IS NULL)",
            name=op.f("ck_access_reviews_exactly_one_scope"),
        ),
        sa.ForeignKeyConstraint(
            ["department_id"],
            ["departments.id"],
            name=op.f("fk_access_reviews_department_id_departments"),
        ),
        sa.ForeignKeyConstraint(
            ["reviewer_user_id"],
            ["users.id"],
            name=op.f("fk_access_reviews_reviewer_user_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["system_id"], ["systems.id"], name=op.f("fk_access_reviews_system_id_systems")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_access_reviews_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_access_reviews")),
    )
    op.create_index(
        "ix_access_reviews_tenant_id_status",
        "access_reviews",
        ["tenant_id", "status"],
        unique=False,
    )
    op.create_table(
        "review_items",
        sa.Column("review_id", sa.Uuid(), nullable=False),
        sa.Column("grant_id", sa.Uuid(), nullable=False),
        sa.Column("flag", sa.String(length=16), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
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
            ["grant_id"], ["grants.id"], name=op.f("fk_review_items_grant_id_grants")
        ),
        sa.ForeignKeyConstraint(
            ["review_id"],
            ["access_reviews.id"],
            name=op.f("fk_review_items_review_id_access_reviews"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_review_items_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_review_items")),
    )
    op.create_index(
        "ix_review_items_tenant_id_review_id",
        "review_items",
        ["tenant_id", "review_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_review_items_tenant_id_review_id", table_name="review_items")
    op.drop_table("review_items")
    op.drop_index("ix_access_reviews_tenant_id_status", table_name="access_reviews")
    op.drop_table("access_reviews")
