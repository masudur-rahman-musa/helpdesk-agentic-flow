"""restricted application db role

Revision ID: c7f4a1d2e8b3
Revises: a4d71c9b3e50
Create Date: 2026-09-14 10:12:03.881204

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c7f4a1d2e8b3"
down_revision: str | Sequence[str] | None = "a4d71c9b3e50"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# A fixed name, never config: a configurable role name would be an identifier
# put straight into SQL.
ROLE_NAME = "helpdesk_app"

# The app role gets only INSERT and SELECT on the audit log. While the app user
# owned the database, its grants meant nothing and it could even disable the
# append-only trigger. This role is not the owner, so an UPDATE, DELETE or
# TRUNCATE on `audit_events` is refused by the privilege check (42501) before
# the trigger is reached.
_APPEND_ONLY = ("SELECT", "INSERT")
_READ_WRITE = ("SELECT", "INSERT", "UPDATE")
_READ_WRITE_DELETE = ("SELECT", "INSERT", "UPDATE", "DELETE")

# Explicit grants per table, never ALTER DEFAULT PRIVILEGES, which would give a
# future audit-like table UPDATE and DELETE on day one. A new table needs a
# line here. Only `idempotency_keys` (expired keys purged) and `policy_chunks`
# (replaced on reingest) need DELETE.
TABLE_GRANTS: dict[str, tuple[str, ...]] = {
    "tenants": _READ_WRITE,
    "departments": _READ_WRITE,
    "users": _READ_WRITE,
    "systems": _READ_WRITE,
    "roles": _READ_WRITE,
    "role_templates": _READ_WRITE,
    "sod_rules": _READ_WRITE,
    "grants": _READ_WRITE,
    "tickets": _READ_WRITE,
    "audit_events": _APPEND_ONLY,
    "idempotency_keys": _READ_WRITE_DELETE,
    "access_requests": _READ_WRITE,
    "access_reviews": _READ_WRITE,
    "review_items": _READ_WRITE,
    "policy_documents": _READ_WRITE,
    "policy_chunks": _READ_WRITE_DELETE,
    "sessions": _READ_WRITE,
    "messages": _READ_WRITE,
    "notifications": _READ_WRITE,
}

# A table grant does not cover the identity column's sequence, so INSERT would
# fail without this.
AUDIT_SEQUENCE_NAME = "audit_events_seq_seq"

# Roles are cluster-wide, so a comment on the role records that this migration
# made it. A role provisioned elsewhere has no comment and the downgrade leaves
# it alone.
_CREATED_MARKER = f"created by alembic revision {revision}"

# NOLOGIN on purpose: a password here would be a secret in code. A local init
# script grants LOGIN from the environment. If it never ran, services fail on
# connect, loudly, instead of quietly using the owner login.
_CREATE_ROLE = f"""
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{ROLE_NAME}') THEN
        CREATE ROLE "{ROLE_NAME}" NOLOGIN;
        COMMENT ON ROLE "{ROLE_NAME}" IS '{_CREATED_MARKER}';
    END IF;
END
$$
"""

# `%I` quotes whatever name `current_database()` reports.
_GRANT_CONNECT = f"""
DO $$
BEGIN
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO "{ROLE_NAME}"', current_database());
END
$$
"""

_REVOKE_CONNECT = f"""
DO $$
BEGIN
    EXECUTE format('REVOKE CONNECT ON DATABASE %I FROM "{ROLE_NAME}"', current_database());
END
$$
"""

# Dropping fails while another database in the cluster still grants to the
# role. Then the role is still in use and keeping it is right. Only that one
# error is caught, so a permissions problem still raises.
_DROP_ROLE_IF_CREATED = f"""
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_roles
        WHERE rolname = '{ROLE_NAME}'
          AND shobj_description(oid, 'pg_authid') = '{_CREATED_MARKER}'
    ) THEN
        BEGIN
            DROP ROLE "{ROLE_NAME}";
        EXCEPTION WHEN dependent_objects_still_exist THEN
            RAISE NOTICE 'role {ROLE_NAME} is still granted in another database, keeping it';
        END;
    END IF;
END
$$
"""


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(_CREATE_ROLE)
    # PUBLIC usually has these, but a hardened setup revokes them.
    op.execute(_GRANT_CONNECT)
    op.execute(f'GRANT USAGE ON SCHEMA public TO "{ROLE_NAME}"')
    for table_name, privileges in TABLE_GRANTS.items():
        op.execute(f'GRANT {", ".join(privileges)} ON TABLE {table_name} TO "{ROLE_NAME}"')
    op.execute(f'GRANT USAGE ON SEQUENCE {AUDIT_SEQUENCE_NAME} TO "{ROLE_NAME}"')


def downgrade() -> None:
    """Downgrade schema."""
    for table_name in TABLE_GRANTS:
        op.execute(f'REVOKE ALL ON TABLE {table_name} FROM "{ROLE_NAME}"')
    op.execute(f'REVOKE ALL ON SEQUENCE {AUDIT_SEQUENCE_NAME} FROM "{ROLE_NAME}"')
    op.execute(f'REVOKE USAGE ON SCHEMA public FROM "{ROLE_NAME}"')
    op.execute(_REVOKE_CONNECT)
    op.execute(_DROP_ROLE_IF_CREATED)
