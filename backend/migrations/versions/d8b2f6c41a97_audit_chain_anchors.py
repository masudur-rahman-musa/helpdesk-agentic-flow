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

# An earlier change decision A: the anchored head lives in its own append-only table
# rather than a file on a volume. The restricted role of an earlier change cannot
# delete or update a row it holds only INSERT on, so the guarantee is the one
# this repo has already built and tested.
#
# An earlier change decision D: this revision carries its OWN grant map rather than
# editing an earlier change's or sharing a constant with it. An applied migration is
# frozen history, and `test_audit_db_role.py` unions the two maps so each
# revision keeps saying for itself what it granted.
TABLE_GRANTS: dict[str, tuple[str, ...]] = {TABLE_NAME: ("SELECT", "INSERT")}

# Same shape as `audit_events`' trigger and for the same reason: an UPDATE
# from any connection is refused by the database rather than trusted to be
# well-behaved. The grants above are the first refusal (SQLSTATE 42501 for
# `helpdesk_app`); this is what still stops the table OWNER, and what a
# superuser has to go out of its way to disable. `TG_TABLE_NAME` is used so
# the message names the table without the literal being repeated.
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

# TRUNCATE fires no row-level trigger, and a tenant with no anchor verifies as
# intact, so one statement would switch off the whole detection this revision
# adds. Statement-level, same function.
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
    # Unique rather than merely indexed: two runners whose Redis lock both
    # expired would otherwise each store a head at the same seq, and two rows
    # at one seq make "the current head" a question the table cannot answer.
    # A unique INDEX rather than a UniqueConstraint, so the composite index
    # every tenant-scoped table owes (`test_models.py`) is the same object.
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
    # The REVOKE runs before the DROP: dropping the table takes its grants
    # with it, but a downgrade that only dropped would leave nothing for
    # `test_audit_db_role.py` to tell apart from a revoke that never ran.
    for table_name in TABLE_GRANTS:
        op.execute(f'REVOKE ALL ON TABLE {table_name} FROM "{ROLE_NAME}"')
    op.execute(f"DROP TRIGGER IF EXISTS audit_chain_anchors_no_truncate ON {TABLE_NAME}")
    op.execute(f"DROP TRIGGER IF EXISTS audit_chain_anchors_append_only ON {TABLE_NAME}")
    op.execute("DROP FUNCTION IF EXISTS audit_chain_anchors_append_only()")
    op.drop_index("ix_audit_chain_anchors_tenant_id_audit_seq", table_name=TABLE_NAME)
    op.drop_table(TABLE_NAME)
