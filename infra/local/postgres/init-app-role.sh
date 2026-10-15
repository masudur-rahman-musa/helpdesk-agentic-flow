#!/bin/sh
# Give the restricted application role a password, on a fresh pgdata volume.
#
# An earlier change decision A splits this in two. The Alembic migration owns the
# role's EXISTENCE and every grant, because grants are schema and because an
# init script runs only when the data directory is empty -- grants written here
# would silently skip every database that already exists. The password is the
# half that cannot live in a migration: a password in a migration is a secret
# in code, so it comes from the environment, here.
#
# The role is created LOGIN here and NOLOGIN by the migration, and whichever
# runs first wins on that flag. That is deliberate: on a fresh volume this runs
# first and the migration then finds the role present and leaves it alone, so
# the services can connect. Where an existing volume skipped this script the
# migration's NOLOGIN role is what exists, and a service started against it is
# refused at connect rather than falling back to the owner DSN.
#
# The SQL is a separate file mounted outside the init directory: the postgres
# entrypoint runs every .sql it finds there, and a second run with no
# app_password set would fail the whole container init.

set -eu

if [ -z "${APP_DB_PASSWORD:-}" ]; then
	echo "init-app-role.sh: APP_DB_PASSWORD is unset; set it in .env" >&2
	exit 1
fi

# -v plus :'app_password' is what quotes the value. Interpolating it into the
# SQL text directly would make any quote in the password a syntax error at
# best, and a project rule keeps the literal out of this file either way.
psql -v ON_ERROR_STOP=1 \
	--username "$POSTGRES_USER" \
	--dbname "$POSTGRES_DB" \
	-v app_password="$APP_DB_PASSWORD" \
	-f /etc/helpdesk/init-app-role.sql
