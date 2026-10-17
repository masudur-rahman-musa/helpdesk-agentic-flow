"""audit chain anchor retention

Revision ID: e2c5a9f70b14
Revises: f1a7c0d4e2b9
Create Date: 2026-09-20 09:12:47.882014

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e2c5a9f70b14"
down_revision: str | Sequence[str] | None = "f1a7c0d4e2b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_NAME = "audit_chain_anchors"
ROLE_NAME = "helpdesk_app"

# The trigger, not the grants, decides which row may be deleted. The app role
# holds DELETE but can only remove an anchor older than the window that is not
# its tenant's head. The head and every recent anchor cannot be deleted by
# anyone, owner included. This revision keeps its own grant map, because an
# applied migration is frozen history.
TABLE_GRANTS: dict[str, tuple[str, ...]] = {TABLE_NAME: ("DELETE",)}

# The 90-day window is a SQL literal here and a Python constant in the audit
# retention module. It is copied, not shared, so a later edit cannot change
# what an applied revision enforces.
#
# The DELETE branch is nested because the same function backs the TRUNCATE
# trigger, where OLD is not set.
#
# `SET search_path` with `pg_temp` LAST matters. Otherwise `pg_temp` is searched
# first, and the app role could create a temp table of the same name holding a
# high `audit_seq` row, then delete a tenant's real head past the EXISTS.
_RETENTION_FUNCTION = """
CREATE OR REPLACE FUNCTION audit_chain_anchors_append_only() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.anchored_at < now() - INTERVAL '90 days'
           AND EXISTS (
               SELECT 1 FROM audit_chain_anchors
               WHERE tenant_id = OLD.tenant_id
                 AND audit_seq > OLD.audit_seq
           )
        THEN
            RETURN OLD;
        END IF;
    END IF;
    RAISE EXCEPTION '% is append-only: % is not permitted', TG_TABLE_NAME, TG_OP;
END;
$$ LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp
"""

# The earlier body, restored by the downgrade. Copied rather than imported so
# this revision says for itself what it puts back.
_APPEND_ONLY_FUNCTION = """
CREATE OR REPLACE FUNCTION audit_chain_anchors_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% is append-only: % is not permitted', TG_TABLE_NAME, TG_OP;
END;
$$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(_RETENTION_FUNCTION)
    for table_name, privileges in TABLE_GRANTS.items():
        op.execute(f'GRANT {", ".join(privileges)} ON TABLE {table_name} TO "{ROLE_NAME}"')


def downgrade() -> None:
    """Downgrade schema."""
    # Revoke only this revision's DELETE. SELECT and INSERT belong to the
    # earlier revision, and the role still needs them after one step down.
    for table_name, privileges in TABLE_GRANTS.items():
        op.execute(f'REVOKE {", ".join(privileges)} ON TABLE {table_name} FROM "{ROLE_NAME}"')
    op.execute(_APPEND_ONLY_FUNCTION)
