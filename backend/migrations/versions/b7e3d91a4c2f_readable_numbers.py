"""readable numbers

Revision ID: b7e3d91a4c2f
Revises: e2c5a9f70b14
Create Date: 2026-10-19 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import Connection

# revision identifiers, used by Alembic.
revision: str = "b7e3d91a4c2f"
down_revision: str | Sequence[str] | None = "e2c5a9f70b14"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE_NAME = "helpdesk_app"

# This revision keeps its own grant map, because an applied migration is
# frozen history. The counter is read and bumped, never deleted.
TABLE_GRANTS: dict[str, tuple[str, ...]] = {"reference_counters": ("SELECT", "INSERT", "UPDATE")}

# Table name to the stored `kind` word. Copied, not imported from the model,
# so a later rename cannot change what this revision wrote.
NUMBERED_TABLES: dict[str, str] = {"tickets": "ticket", "access_requests": "access_request"}


def backfill_numbers(connection: Connection) -> None:
    """Number every old row per company by creation time, then set the counters.

    The id breaks a tie in `created_at`, so the order is the same every run.
    The counter takes the highest number, so the next new row never repeats one.
    """
    for table_name, kind in NUMBERED_TABLES.items():
        connection.execute(
            sa.text(
                f"""
                UPDATE {table_name} AS t SET number = ordered.n
                FROM (
                    SELECT id, row_number() OVER (
                        PARTITION BY tenant_id ORDER BY created_at, id
                    ) AS n
                    FROM {table_name}
                ) AS ordered
                WHERE t.id = ordered.id
                """
            )
        )
        connection.execute(
            sa.text(
                f"""
                INSERT INTO reference_counters (id, tenant_id, kind, last)
                SELECT gen_random_uuid(), tenant_id, :kind, max(number)
                FROM {table_name}
                GROUP BY tenant_id
                ON CONFLICT (tenant_id, kind) DO UPDATE SET last = EXCLUDED.last
                """
            ),
            {"kind": kind},
        )


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "reference_counters",
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("last", sa.Integer(), nullable=False),
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
            ["tenant_id"], ["tenants.id"], name=op.f("fk_reference_counters_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reference_counters")),
    )
    op.create_index(
        "uq_reference_counters_tenant_id_kind",
        "reference_counters",
        ["tenant_id", "kind"],
        unique=True,
    )
    for table_name in NUMBERED_TABLES:
        op.add_column(table_name, sa.Column("number", sa.Integer(), nullable=True))
    backfill_numbers(op.get_bind())
    for table_name in NUMBERED_TABLES:
        op.alter_column(table_name, "number", nullable=False)
        op.create_index(
            f"uq_{table_name}_tenant_id_number", table_name, ["tenant_id", "number"], unique=True
        )
    for table_name, privileges in TABLE_GRANTS.items():
        op.execute(f'GRANT {", ".join(privileges)} ON TABLE {table_name} TO "{ROLE_NAME}"')


def downgrade() -> None:
    """Downgrade schema."""
    for table_name in TABLE_GRANTS:
        op.execute(f'REVOKE ALL ON TABLE {table_name} FROM "{ROLE_NAME}"')
    for table_name in NUMBERED_TABLES:
        op.drop_index(f"uq_{table_name}_tenant_id_number", table_name=table_name)
        op.drop_column(table_name, "number")
    op.drop_index("uq_reference_counters_tenant_id_kind", table_name="reference_counters")
    op.drop_table("reference_counters")
