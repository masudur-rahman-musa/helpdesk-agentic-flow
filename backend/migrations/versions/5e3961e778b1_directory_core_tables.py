"""directory core tables

Revision ID: 5e3961e778b1
Revises: 8296bb27ccad
Create Date: 2026-09-03 05:46:55.166015

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "5e3961e778b1"
down_revision: str | Sequence[str] | None = "8296bb27ccad"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "tenants",
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("residency", sa.String(length=16), nullable=False),
        sa.Column("settings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tenants")),
        sa.UniqueConstraint("slug", name=op.f("uq_tenants_slug")),
    )
    op.create_table(
        "departments",
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("manager_user_id", sa.Uuid(), nullable=True),
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
            ["tenant_id"], ["tenants.id"], name=op.f("fk_departments_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_departments")),
    )
    op.create_index(
        "ix_departments_tenant_id_name", "departments", ["tenant_id", "name"], unique=True
    )
    op.create_table(
        "users",
        sa.Column("department_id", sa.Uuid(), nullable=True),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("display_name", sa.String(length=256), nullable=False),
        sa.Column("password_hash", sa.String(length=256), nullable=False),
        sa.Column("principals", postgresql.ARRAY(sa.String(length=64)), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=True),
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
            ["department_id"], ["departments.id"], name=op.f("fk_users_department_id_departments")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_users_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
    )
    op.create_index("ix_users_tenant_id_email", "users", ["tenant_id", "email"], unique=True)
    op.create_table(
        "systems",
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("sensitivity", sa.String(length=16), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=True),
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
            ["owner_user_id"], ["users.id"], name=op.f("fk_systems_owner_user_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_systems_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_systems")),
    )
    op.create_index("ix_systems_tenant_id_slug", "systems", ["tenant_id", "slug"], unique=True)
    op.create_table(
        "tickets",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=True),
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
            ["tenant_id"], ["tenants.id"], name=op.f("fk_tickets_tenant_id_tenants")
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_tickets_user_id_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tickets")),
    )
    op.create_index(
        "ix_tickets_tenant_id_user_id", "tickets", ["tenant_id", "user_id"], unique=False
    )
    op.create_table(
        "roles",
        sa.Column("system_id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("privilege", sa.String(length=16), nullable=False),
        sa.Column("default_expiry_days", sa.Integer(), nullable=False),
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
            ["system_id"], ["systems.id"], name=op.f("fk_roles_system_id_systems")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_roles_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_roles")),
    )
    op.create_index(
        "ix_roles_tenant_id_system_id_slug",
        "roles",
        ["tenant_id", "system_id", "slug"],
        unique=True,
    )
    op.create_table(
        "grants",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=True),
        sa.Column("granted_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoke_reason", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
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
            ["granted_by_user_id"], ["users.id"], name=op.f("fk_grants_granted_by_user_id_users")
        ),
        sa.ForeignKeyConstraint(["role_id"], ["roles.id"], name=op.f("fk_grants_role_id_roles")),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_grants_tenant_id_tenants")
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_grants_user_id_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_grants")),
    )
    op.create_index("ix_grants_tenant_id_user_id", "grants", ["tenant_id", "user_id"], unique=False)
    op.create_table(
        "role_templates",
        sa.Column("department_id", sa.Uuid(), nullable=False),
        sa.Column("role_id", sa.Uuid(), nullable=False),
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
            ["department_id"],
            ["departments.id"],
            name=op.f("fk_role_templates_department_id_departments"),
        ),
        sa.ForeignKeyConstraint(
            ["role_id"], ["roles.id"], name=op.f("fk_role_templates_role_id_roles")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_role_templates_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_role_templates")),
    )
    op.create_index(
        "ix_role_templates_tenant_id_department_id_role_id",
        "role_templates",
        ["tenant_id", "department_id", "role_id"],
        unique=True,
    )
    op.create_table(
        "sod_rules",
        sa.Column("role_a_id", sa.Uuid(), nullable=False),
        sa.Column("role_b_id", sa.Uuid(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("policy_ref", sa.String(length=128), nullable=False),
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
            ["role_a_id"], ["roles.id"], name=op.f("fk_sod_rules_role_a_id_roles")
        ),
        sa.ForeignKeyConstraint(
            ["role_b_id"], ["roles.id"], name=op.f("fk_sod_rules_role_b_id_roles")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_sod_rules_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sod_rules")),
    )
    op.create_index(
        "ix_sod_rules_tenant_id_role_a_id_role_b_id",
        "sod_rules",
        ["tenant_id", "role_a_id", "role_b_id"],
        unique=True,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_sod_rules_tenant_id_role_a_id_role_b_id", table_name="sod_rules")
    op.drop_table("sod_rules")
    op.drop_index("ix_role_templates_tenant_id_department_id_role_id", table_name="role_templates")
    op.drop_table("role_templates")
    op.drop_index("ix_grants_tenant_id_user_id", table_name="grants")
    op.drop_table("grants")
    op.drop_index("ix_roles_tenant_id_system_id_slug", table_name="roles")
    op.drop_table("roles")
    op.drop_index("ix_tickets_tenant_id_user_id", table_name="tickets")
    op.drop_table("tickets")
    op.drop_index("ix_systems_tenant_id_slug", table_name="systems")
    op.drop_table("systems")
    op.drop_index("ix_users_tenant_id_email", table_name="users")
    op.drop_table("users")
    op.drop_index("ix_departments_tenant_id_name", table_name="departments")
    op.drop_table("departments")
    op.drop_table("tenants")
