"""audit chain anchors

Revision ID: d8b2f6c41a97
Revises: c7f4a1d2e8b3
Create Date: 2026-09-14 14:31:22.104553

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d8b2f6c41a97"
down_revision: str | Sequence[str] | None = "c7f4a1d2e8b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_NAME = "audit_chain_anchors"
ROLE_NAME = "helpdesk_app"

# The anchored head lives in its own append-only table, not a file on a volume,
# so the restricted app role (INSERT only) cannot change it. This revision keeps
# its own grant map, because an applied migration is frozen history.
TABLE_GRANTS: dict[str, tuple[str, ...]] = {TABLE_NAME: ("SELECT", "INSERT")}

# Same trigger as on `audit_events`. The grants stop the app role; this still
# stops the table owner.
_APPEND_ONLY_FUNCTION = """
CREATE OR REPLACE FUNCTION audit_chain_anchors_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% is append-only: % is not permitted', TG_TABLE_NAME, TG_OP;
END;
$$ LANGUAGE plpgsql
"""

_APPEND_ONLY_TRIGGER = """
CREATE TRIGGER audit_chain_anchors_append_only
BEFORE UPDATE OR DELETE ON audit_chain_anchors
FOR EACH ROW EXECUTE FUNCTION audit_chain_anchors_append_only()
"""

# TRUNCATE fires no row trigger, and a tenant with no anchor verifies as intact,
# so one statement would switch off the whole check.
_APPEND_ONLY_TRUNCATE_TRIGGER = """
CREATE TRIGGER audit_chain_anchors_no_truncate
BEFORE TRUNCATE ON audit_chain_anchors
FOR EACH STATEMENT EXECUTE FUNCTION audit_chain_anchors_append_only()
"""


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        TABLE_NAME,
        sa.Column("audit_seq", sa.BigInteger(), nullable=False),
        sa.Column("audit_hash", sa.String(length=64), nullable=False),
        sa.Column("anchored_at", sa.DateTime(timezone=True), nullable=False),
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
            ["tenant_id"], ["tenants.id"], name=op.f("fk_audit_chain_anchors_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_chain_anchors")),
    )
    # Unique: two runners whose locks both expired could each store a head at
    # the same seq, and then "the current head" has no answer.
    op.create_index(
        "ix_audit_chain_anchors_tenant_id_audit_seq",
        TABLE_NAME,
        ["tenant_id", "audit_seq"],
        unique=True,
    )
    op.execute(_APPEND_ONLY_FUNCTION)
    op.execute(_APPEND_ONLY_TRIGGER)
    op.execute(_APPEND_ONLY_TRUNCATE_TRIGGER)
    for table_name, privileges in TABLE_GRANTS.items():
        op.execute(f'GRANT {", ".join(privileges)} ON TABLE {table_name} TO "{ROLE_NAME}"')


def downgrade() -> None:
    """Downgrade schema."""
    # Revoke before the drop, so the downgrade undoes the grant on its own.
    for table_name in TABLE_GRANTS:
        op.execute(f'REVOKE ALL ON TABLE {table_name} FROM "{ROLE_NAME}"')
    op.execute(f"DROP TRIGGER IF EXISTS audit_chain_anchors_no_truncate ON {TABLE_NAME}")
    op.execute(f"DROP TRIGGER IF EXISTS audit_chain_anchors_append_only ON {TABLE_NAME}")
    op.execute("DROP FUNCTION IF EXISTS audit_chain_anchors_append_only()")
    op.drop_index("ix_audit_chain_anchors_tenant_id_audit_seq", table_name=TABLE_NAME)
    op.drop_table(TABLE_NAME)
