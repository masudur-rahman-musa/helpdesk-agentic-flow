"""Reset the LOCAL compose database to a known state before an e2e run.

The e2e suite resets once, before the whole run rather than per spec:
flows 4 and 5 destroy state on purpose, and
`audit_events` carries UPDATE, DELETE and TRUNCATE triggers, so there is no
partial cleanup available to a per-flow scheme anyway. Dropping and recreating
the schema is the one erasure those triggers allow.

Because this module's whole job is destruction, its guards run FIRST and its
DDL second, because migrations only ever run against the local database:

* `check_local_database_url` refuses any target whose host is not in
  `LOCAL_HOSTS`, the hostless URL included - that one resolves to the driver's
  default socket, which is a real database on whatever machine ran this.
* `check_tenant_slug` refuses a slug the `tenants.slug` column could not store,
  against the column's own length rather than a copy of it.

Both refuse with `InvalidResetTargetError`, a typed `VALIDATION` error,
so nothing free-text crosses the boundary. That the checks run BEFORE the drop
is the claim that matters, and the guard tests pin it: move either check
below `run_statements` and they go red. Another test checks that an accepted
target runs exactly the drop statements, so a reset that dropped nothing
cannot pass.

`reset` takes its `run_statements` as an argument rather than opening a
connection itself so the guard is drivable with strings and no database at
all; `main` is the only thing here that touches Postgres or spawns anything.
"""

import asyncio
import os
import subprocess
import sys
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import String, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from helpdesk.scheduler.jobs.review_schedule import generate_reviews
from helpdesk.shared.access.directory import SimulatedDirectory
from helpdesk.shared.core.db import (
    create_engine,
    create_session_factory,
    tenant_session,
)
from helpdesk.shared.core.errors import ErrorCode, ToolError
from helpdesk.shared.models.directory import Tenant

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = Path(__file__).resolve().parent
# The Python project lives under `backend/`, so `uv run` and `alembic` must
# run with `backend/` as their working directory. This script sits beside
# `seed.py` in `backend/scripts/`.
_BACKEND_ROOT = _REPO_ROOT / "backend"
_BACKEND_SCRIPTS_DIR = _BACKEND_ROOT / "scripts"

LOCAL_HOSTS: tuple[str, str] = ("localhost", "127.0.0.1")
"""The only hosts this script will drop a schema on."""


def _tenant_slug_cap() -> int:
    """The `tenants.slug` column length, read off the column itself.

    A copy of the number would let a migration widen the column while this
    guard went on refusing a slug the store would accept. A guard test pins it
    against the column.
    """
    column_type = Tenant.__table__.c.slug.type
    if not isinstance(column_type, String) or column_type.length is None:
        raise AssertionError("tenants.slug is not a sized string column")
    return column_type.length


TENANT_SLUG_CAP: int = _tenant_slug_cap()

DROP_STATEMENTS: tuple[str, ...] = (
    "DROP SCHEMA public CASCADE",
    "CREATE SCHEMA public",
)
"""The reset itself, in order: everything goes, then an empty schema is put back.

Recreating in the same batch matters - `alembic upgrade head` runs next and
has nowhere to write without it.
"""


class InvalidResetTargetError(ToolError):
    """The reset was pointed at something it refuses to destroy.

    `VALIDATION` rather than `FORBIDDEN`: the caller's argument is wrong and
    the caller can fix it, which is exactly what the typed error codes reserve this
    code for.
    """

    code = ErrorCode.VALIDATION


StatementRunner = Callable[[Sequence[str]], Awaitable[None]]


def check_local_database_url(url: str) -> str:
    """The URL unchanged, or `InvalidResetTargetError`.

    Returned byte for byte rather than normalised: the caller hands this value
    to the driver, so a rewritten port, user or database name would point the
    reset somewhere nobody asked for. A guard test pins this.
    """
    host = urlsplit(url).hostname
    if host not in LOCAL_HOSTS:
        raise InvalidResetTargetError(
            f"the e2e reset runs against {' or '.join(LOCAL_HOSTS)} only, never {host!r}"
        )
    return url


def check_tenant_slug(slug: str) -> str:
    """The slug unchanged, or `InvalidResetTargetError`.

    The limit is inclusive at `TENANT_SLUG_CAP`. The guard tests pin a slug of
    exactly the cap against one a character over it.
    """
    if len(slug) > TENANT_SLUG_CAP:
        raise InvalidResetTargetError(
            f"a tenant slug is at most {TENANT_SLUG_CAP} characters, not {len(slug)}"
        )
    return slug


async def reset(
    database_url: str,
    tenant_slugs: Sequence[str],
    *,
    run_statements: StatementRunner,
) -> None:
    """Check every argument, then drop and recreate the schema.

    Every check is above the single `run_statements` call on purpose, and that
    ordering is the whole point of this function.
    """
    check_local_database_url(database_url)
    for slug in tenant_slugs:
        check_tenant_slug(slug)
    await run_statements(DROP_STATEMENTS)


async def _run_on(database_url: str, statements: Sequence[str]) -> None:
    """Execute statements on the target, each in its own autocommit block."""
    engine = create_async_engine(database_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            for statement in statements:
                await connection.execute(text(statement))
    finally:
        await engine.dispose()


def _step(script_name: str, *args: str, scripts_dir: Path = _SCRIPTS_DIR) -> None:
    """Run one repo script to completion, or stop the reset with its output.

    The scripts import the Python project, which is under `backend/`, so they
    run with `backend/` as cwd and an absolute path to the script. They live
    beside this one in `backend/scripts/`.
    """
    subprocess.run(
        [sys.executable, str(scripts_dir / script_name), *args],
        cwd=_BACKEND_ROOT,
        check=True,
    )


async def _open_this_quarters_reviews(database_url: str) -> None:
    """Open every due scope's review, the same call the scheduler's job makes.

    Flow 5 revokes a stale grant on an OPEN review item, and nothing else in
    the harness plants one: the seed writes no reviews, and the scheduler is
    not one of the four processes Playwright starts. Rather
    than inventing a review here, this runs
    `helpdesk.scheduler.jobs.review_schedule.generate_reviews` exactly as
    `helpdesk.scheduler.jobs.run_review_generation` does, so the row flow 5 opens is
    the row the product would have produced on its own.

    Per tenant, because the engine reads through the tenant-bound session and
    a review is a tenant-scoped record.
    """
    engine = create_engine(database_url)
    try:
        factory = create_session_factory(engine)
        now = datetime.now(UTC)
        async with factory() as reader:
            tenant_ids = list((await reader.execute(select(Tenant.id))).scalars())
        for tenant_id in tenant_ids:
            async with tenant_session(factory, tenant_id) as session:
                await generate_reviews(session, SimulatedDirectory(session), now=now)
    finally:
        await engine.dispose()


def main() -> None:
    """Reset, migrate, set the checkpointer up, seed, ingest, open reviews.

    In that order because each step consumes the one before: `alembic` needs
    the empty schema, the seed needs the tables, the ingest needs the tenants
    the seed writes, and the review generation needs the grants it flags.
    """
    database_url = os.environ["DATABASE_URL"]
    slugs = [
        slug for slug in os.environ.get("E2E_TENANT_SLUGS", "nordlicht,cuan").split(",") if slug
    ]

    asyncio.run(
        reset(
            database_url,
            slugs,
            run_statements=lambda statements: _run_on(database_url, statements),
        )
    )

    subprocess.run(["uv", "run", "alembic", "upgrade", "head"], cwd=_BACKEND_ROOT, check=True)
    subprocess.run(
        [sys.executable, "-m", "helpdesk.agent_api.cli"],
        cwd=_BACKEND_ROOT,
        check=True,
    )
    _step("seed.py", scripts_dir=_BACKEND_SCRIPTS_DIR)
    _step("ingest.py")
    asyncio.run(_open_this_quarters_reviews(database_url))


if __name__ == "__main__":
    main()
