"""add access requests and system category

Revision ID: 1923c1ae5a60
Revises: 31ca97109faf
Create Date: 2026-09-05 20:46:29.914232

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "1923c1ae5a60"
down_revision: str | Sequence[str] | None = "31ca97109faf"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "access_requests",
        sa.Column("requester_user_id", sa.Uuid(), nullable=False),
        sa.Column("target_user_id", sa.Uuid(), nullable=False),
        sa.Column("role_id", sa.Uuid(), nullable=False),
        sa.Column("justification", sa.Text(), nullable=False),
        sa.Column("requested_expiry_days", sa.Integer(), nullable=True),
        sa.Column("risk_class", sa.String(length=16), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("decision_reasons", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("citations", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("approver_user_ids", postgresql.ARRAY(sa.Uuid()), nullable=False),
        sa.Column("approvals", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=True),
        sa.Column("trace_id", sa.String(length=128), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
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
            ["requester_user_id"],
            ["users.id"],
            name=op.f("fk_access_requests_requester_user_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["role_id"], ["roles.id"], name=op.f("fk_access_requests_role_id_roles")
        ),
        sa.ForeignKeyConstraint(
            ["target_user_id"], ["users.id"], name=op.f("fk_access_requests_target_user_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_access_requests_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_access_requests")),
    )
    op.create_index(
        "ix_access_requests_tenant_id_requester_user_id",
        "access_requests",
        ["tenant_id", "requester_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_access_requests_tenant_id_target_user_id",
        "access_requests",
        ["tenant_id", "target_user_id"],
        unique=False,
    )
    op.create_foreign_key(
        op.f("fk_grants_request_id_access_requests"),
        "grants",
        "access_requests",
        ["request_id"],
        ["id"],
    )
    op.add_column(
        "systems",
        sa.Column("category", sa.String(length=16), server_default="other", nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("systems", "category")
    op.drop_constraint(op.f("fk_grants_request_id_access_requests"), "grants", type_="foreignkey")
    op.drop_index("ix_access_requests_tenant_id_target_user_id", table_name="access_requests")
    op.drop_index("ix_access_requests_tenant_id_requester_user_id", table_name="access_requests")
    op.drop_table("access_requests")
