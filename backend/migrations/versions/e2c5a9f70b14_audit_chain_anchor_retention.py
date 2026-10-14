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

# An earlier change decision A: the purge is possible at all because the trigger, not
# the grants, decides which row may go. `helpdesk_app` holding DELETE can only
# ever remove a row the function below has already agreed to, which is a row
# past the window that is not its tenant's head. The guarantee that replaces
# an earlier change's "no delete, unless you are the owner" is the stronger one: the
# head AT THE TIME OF THE STATEMENT, and every recent anchor, cannot be
# deleted by anyone, owner included. The qualifier is load-bearing: the role
# also holds INSERT, so a row written above a head demotes it.
#
# An earlier change decision D: this revision carries its OWN grant map, like plan
# 30bb's beside it. An applied migration is frozen history, so
# `test_audit_db_role.py` unions the two maps per table rather than merging
# them, and each revision keeps saying for itself what it granted.
TABLE_GRANTS: dict[str, tuple[str, ...]] = {TABLE_NAME: ("DELETE",)}

# Decision D again, from the other side: the window is written here as a SQL
# literal and in `helpdesk/audit/retention.py` as a Python constant, and the
# two are held together by
# `test_audit_anchor_retention.py::test_the_migrations_sql_interval_equals_the_library_constant`,
# which parses this file. A value shared at runtime would let a later edit
# silently rewrite what an already-applied revision claims to enforce.
#
# The DELETE branch is nested rather than folded into one condition: the same
# function also backs the statement-level TRUNCATE trigger, where OLD is not
# assigned and reading it would raise the wrong error.
#
# `SET search_path` is what makes the EXISTS trustworthy, and it is new with
# this revision: an earlier change's body read no table at all. plpgsql resolves an
# unqualified relation against the CALLING session's search_path, and
# an unlisted `pg_temp` is searched FIRST, before even `pg_catalog`. Every
# role holds TEMPORARY on the database by default, so without this clause
# `helpdesk_app` could create a temp table of the same name holding one high
# `audit_seq` row and delete a tenant's real head past the EXISTS. Naming
# `pg_temp` LAST is what moves it behind `public`; a path that merely omits it
# does not, which is the trap here. Pinned by `test_audit_anchor_retention.py
# ::test_a_pg_temp_shadow_of_the_anchor_table_does_not_unlock_the_head`.
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

# An earlier change's body, restored verbatim by the downgrade. Kept as its own
# literal here rather than imported from that revision: the downgrade has to
# state what it puts back, and an import would make this revision's meaning
# depend on a file it is allowed to assume nothing about.
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
    # REVOKE of exactly this revision's privilege, never ALL: an earlier change's
    # SELECT and INSERT are its own to revoke, and taking them here would
    # leave the role unable to read or anchor after a downgrade of one step.
    for table_name, privileges in TABLE_GRANTS.items():
        op.execute(f'REVOKE {", ".join(privileges)} ON TABLE {table_name} FROM "{ROLE_NAME}"')
    op.execute(_APPEND_ONLY_FUNCTION)
