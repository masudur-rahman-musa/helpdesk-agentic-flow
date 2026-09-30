"""An earlier change §6: the Postgres checkpointer module, with no graph in sight.

Expected implementation surface (the implementer must provide exactly this):

``agent_api.checkpointer``
    ``def psycopg_dsn(url: str) -> str`` — PURE. Turns the settings'
        ``postgresql+asyncpg://…`` URL into the psycopg DSN
        (``postgresql://…``). Any other scheme — a bare ``postgresql://``, a
        ``mysql://``, the empty string — raises a typed
        ``helpdesk.errors.ToolError`` whose ``code`` is
        ``ErrorCode.VALIDATION``, BEFORE any connection is opened.
    ``def build_pool(dsn: str) -> AsyncConnectionPool`` — the CONSTRUCTED,
        not-yet-opened pool, carrying both timeout halves: a connect timeout
        of ``5.0`` seconds as the libpq ``connect_timeout`` connection
        parameter, and a command timeout of ``10.0`` seconds as a
        ``statement_timeout=10000`` server option (a project rule: both are
        asserted on the pool this function returns, never on a constant).
        The caller opens it (``async with pool:``) and closes it.
    ``def build_checkpointer(pool: AsyncConnectionPool) -> AsyncPostgresSaver``
    ``async def setup_checkpointer(pool: AsyncConnectionPool) -> None`` —
        creates LangGraph's own checkpoint tables (decision B); running it
        twice over one database is not an error and changes nothing.
    ``def thread_config(thread_id: str) -> RunnableConfig`` — runs
        ``helpdesk.rag.naming.check_slug`` on the thread id BEFORE returning
        the config any checkpoint write is made under, and returns
        ``{"configurable": {"thread_id": thread_id}}``.

Cost and isolation. The compose Postgres and nothing else: no Redis, no
Qdrant, no gateway, no MCP server, no graph. ONE throwaway database with the
checkpoint tables in it, built once for the whole module;
per-case isolation is a namespace — a unique thread id per case — never a new
database. A POOL is opened per ``asyncio.run``, exactly as
``test_agent_turn.py`` builds an engine per ``asyncio.run``: a connection pool
belongs to the event loop it was opened in, and each case drives its own loop.
What the session fixture builds once is the database and its tables.

Fixtures live in this module rather than a conftest, because a second
``conftest`` under ``services/agent-api/tests`` would collide with
``packages/helpdesk/tests/conftest.py`` under the repo-wide ``mypy`` run.
"""

import asyncio
import socket
import uuid
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from agent_api.checkpointer import (
    CHECKPOINT_PRIVILEGES,
    CHECKPOINT_TABLES,
    RESTRICTED_ROLE_NAME,
    build_pool,
    psycopg_dsn,
    setup_checkpointer,
    thread_config,
)
from helpdesk.core.settings import load_settings
from helpdesk.errors import ErrorCode, ToolError
from helpdesk.rag.naming import SLUG_LENGTH
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

CONNECT_TIMEOUT_SECONDS = 5.0
"""What the pool's libpq `connect_timeout` must be, exactly (plan §1).

a project rule, 11 and 14: the value is the plan's, it is written out here
as a literal, and it is asserted on the CONSTRUCTED pool rather than on a
constant the implementation exports.
"""

COMMAND_TIMEOUT_MILLISECONDS = 10_000
"""What the pool's `statement_timeout` server option must be, exactly.

The plan's read/command half is `10.0` seconds; Postgres takes
`statement_timeout` in milliseconds, so the same number is written here in the
unit the wire uses.
"""

CHECKPOINTS_TABLE = "checkpoints"
"""LangGraph's own checkpoint table: what `setup_checkpointer` must create."""

CHECKPOINT_MIGRATIONS_TABLE = "checkpoint_migrations"
"""The library's version record, which the restricted role must NOT be granted."""

_UNREACHABLE_MSG = (
    "Postgres is unreachable at the configured DATABASE_URL.\n"
    "Plan 18a checkpointer tests require the compose stack: run `docker compose up -d` first.\n"
    "Original error: {error!r}"
)


# --- the throwaway database ---------------------------------------------------


async def _admin_execute(admin_url: str, sql: str) -> None:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT", pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            await conn.execute(text(sql))
    finally:
        await engine.dispose()


@dataclass(frozen=True)
class CheckpointEnv:
    """The one database every case in this file shares, and its psycopg DSN."""

    database_url: str
    dsn: str


@pytest.fixture(scope="session")
def checkpoint_env() -> Iterator[CheckpointEnv]:
    """One database with LangGraph's checkpoint tables in it, built ONCE.

    Deliberately a throwaway database rather than the compose one: an earlier change's
    risk section names `test_models.py` and `test_seed.py`, both of which
    assert on a database's full table list, and a `setup_checkpointer` run
    against a database they read would break them.

    Fails loudly (never skips) when Postgres is down.
    """
    try:
        settings = load_settings()
    except ValidationError as exc:
        pytest.fail(
            f"DATABASE_URL is not configured (env or .env). Copy .env.example to .env.\n{exc}",
            pytrace=False,
        )
    admin_url = settings.database_url
    try:
        asyncio.run(_admin_execute(admin_url, "SELECT 1"))
    except Exception as exc:
        pytest.fail(_UNREACHABLE_MSG.format(error=exc), pytrace=False)

    name = f"helpdesk_ckpt_test_{uuid.uuid4().hex[:12]}"
    asyncio.run(_admin_execute(admin_url, f'CREATE DATABASE "{name}"'))
    url = make_url(admin_url).set(database=name).render_as_string(hide_password=False)
    env = CheckpointEnv(database_url=url, dsn=psycopg_dsn(url))

    async def prepare(pool: Any) -> None:
        await setup_checkpointer(pool)

    _with_pool(env.dsn, prepare)
    yield env
    assert name.startswith("helpdesk_ckpt_test_")
    asyncio.run(_admin_execute(admin_url, f'DROP DATABASE "{name}" WITH (FORCE)'))


def _with_pool[T](dsn: str, scenario: Callable[[Any], Awaitable[T]]) -> T:
    """Run one scenario over a pool opened and closed inside its own loop.

    The explicit close matters beyond tidiness: the plan's risk section names
    a second driver leaking connections into the same compose database, and a
    fixture that left its pool open would be the first place that happened.
    """

    async def runner() -> T:
        pool = build_pool(dsn)
        async with pool:
            return await scenario(pool)

    return asyncio.run(runner())


async def _public_tables(pool: Any) -> frozenset[str]:
    """Every table name in the throwaway database's public schema."""
    async with pool.connection() as conn:
        cursor = await conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        )
        rows = await cursor.fetchall()
    return frozenset(str(row[0]) for row in rows)


async def _checkpoint_rows(pool: Any, thread_id: str) -> int:
    """How many checkpoints this thread id has in LangGraph's own table."""
    async with pool.connection() as conn:
        cursor = await conn.execute(
            f"SELECT count(*) FROM {CHECKPOINTS_TABLE} WHERE thread_id = %s", (thread_id,)
        )
        row = await cursor.fetchone()
    assert row is not None
    return int(row[0])


# --- psycopg_dsn --------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "postgresql+asyncpg://fake_user:fake_pw@db.invalid:5432/helpdesk",
            "postgresql://fake_user:fake_pw@db.invalid:5432/helpdesk",
        ),
        (
            "postgresql+asyncpg://db.invalid/helpdesk",
            "postgresql://db.invalid/helpdesk",
        ),
    ],
)
def test_psycopg_dsn_converts_the_asyncpg_url(url: str, expected: str) -> None:
    """Catches a `psycopg_dsn` that hands the settings' URL through unchanged:
    psycopg does not understand SQLAlchemy's `+asyncpg` driver suffix, so
    every checkpoint write would fail at connection time on a URL that reads
    correct. The second row catches a conversion done by slicing a fixed
    number of characters or by splitting on `:5432`, which the port-less form
    has nothing to split on."""
    assert psycopg_dsn(url) == expected


@pytest.mark.parametrize(
    "url",
    ["postgresql://fake_user:fake_pw@db.invalid:5432/helpdesk", "mysql://db.invalid/helpdesk", ""],
)
def test_a_url_of_any_other_scheme_is_refused_before_a_connection_is_opened(
    url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a `psycopg_dsn` that passes a non-`postgresql+asyncpg` string
    straight through: the bare `postgresql://` row is the one that matters,
    because it is a DSN psycopg WILL accept, so a permissive implementation
    connects to whatever host a misconfigured `DATABASE_URL` names instead of
    refusing it — and the empty row catches a check written as "does not
    contain asyncpg" over a string that contains nothing at all.

    Both socket entry points are poisoned for the duration, so a version that
    validated by TRYING to connect fails here rather than passing on a machine
    where the host happens to resolve."""

    def _no_socket(*args: object, **kwargs: object) -> Any:
        raise AssertionError("psycopg_dsn opened a socket instead of refusing the URL")

    monkeypatch.setattr(socket, "getaddrinfo", _no_socket)
    monkeypatch.setattr(socket.socket, "connect", _no_socket)

    with pytest.raises(ToolError) as caught:
        psycopg_dsn(url)

    assert caught.value.code is ErrorCode.VALIDATION


# --- the pool's two timeout halves -------------------------


def test_the_pool_carries_both_timeout_halves(checkpoint_env: CheckpointEnv) -> None:
    """Catches a pool built with one half of the pair, or with neither: without
    `connect_timeout` a checkpoint write against a wedged Postgres hangs the
    turn for the OS default, and without `statement_timeout` a connection that
    did open holds a query open for ever, which on a second driver into the
    same compose database is the leak the plan's risk section names.

    Read off the CONSTRUCTED pool, so a module that defines the two constants
    and passes neither to psycopg is red."""
    pool = build_pool(checkpoint_env.dsn)

    kwargs = pool.kwargs
    assert isinstance(kwargs, dict), "premise: the pool was built with a kwargs mapping"

    assert float(kwargs["connect_timeout"]) == CONNECT_TIMEOUT_SECONDS
    assert f"statement_timeout={COMMAND_TIMEOUT_MILLISECONDS}" in str(kwargs["options"])


# --- setup_checkpointer -------------------------------------------------------


def test_setup_is_idempotent(checkpoint_env: CheckpointEnv) -> None:
    """Catches a `setup_checkpointer` whose DDL is not re-runnable — a plain
    `CREATE TABLE`, or a version row inserted unconditionally: the fixture has
    already run it once, so the two calls here raise, and every deployment
    whose migrate target ran twice would fail on the second run.

    The table set is compared across the two calls, which is what tells an
    idempotent setup from one that quietly drops and recreates."""

    async def scenario(pool: Any) -> tuple[frozenset[str], frozenset[str]]:
        await setup_checkpointer(pool)
        after_first = await _public_tables(pool)
        await setup_checkpointer(pool)
        return after_first, await _public_tables(pool)

    after_first, after_second = _with_pool(checkpoint_env.dsn, scenario)

    assert CHECKPOINTS_TABLE in after_first
    assert after_second == after_first


def test_setup_grants_the_restricted_role_access_to_every_checkpoint_table(
    checkpoint_env: CheckpointEnv,
) -> None:
    """Catches a `setup_checkpointer` that creates the tables and grants
    nothing: the three services connect as `helpdesk_app`, which holds no
    privilege on a table it does not own, so every turn fails on its first
    checkpoint read with `InsufficientPrivilege` while `make migrate` reports
    success. That is an earlier change F1, found only when the MCP transport stopped
    refusing the call that reached it.

    Read out of `information_schema` per table and compared as an exact SET, so
    a grant that covers `checkpoints` and forgets `checkpoint_writes` is red,
    and so is one that hands over more than a turn needs. `checkpoint_migrations`
    is asserted to carry NO grant: only the owner's `setup()` touches it."""

    async def scenario(pool: Any) -> dict[str, frozenset[str]]:
        await setup_checkpointer(pool)
        granted: dict[str, frozenset[str]] = {}
        for table_name in (*CHECKPOINT_TABLES, CHECKPOINT_MIGRATIONS_TABLE):
            async with pool.connection() as conn:
                cursor = await conn.execute(
                    "SELECT privilege_type FROM information_schema.table_privileges "
                    "WHERE grantee = %s AND table_name = %s",
                    (RESTRICTED_ROLE_NAME, table_name),
                )
                granted[table_name] = frozenset(row[0] for row in await cursor.fetchall())
        return granted

    granted = _with_pool(checkpoint_env.dsn, scenario)

    for table_name in CHECKPOINT_TABLES:
        assert granted[table_name] == frozenset(CHECKPOINT_PRIVILEGES), table_name
    assert granted[CHECKPOINT_MIGRATIONS_TABLE] == frozenset()


# --- the thread id, pre-checked -----------------------------


def test_a_thread_id_that_is_not_a_slug_is_refused_before_any_checkpoint_write(
    checkpoint_env: CheckpointEnv,
) -> None:
    """Catches a `thread_config` that hands an unchecked string to LangGraph as
    the thread id: the thread id is the only value this plan puts into a store
    key, `checkpoints.thread_id` has no pattern and no width of its own, and a
    trailing newline is the case a project rule exists for — it would make one
    session two checkpoint threads.

    The row count afterwards is corroboration, not the kill: nothing may have
    been written under a name the check refused."""
    bad_thread_id = "not a slug\n"

    with pytest.raises(ToolError) as caught:
        thread_config(bad_thread_id)

    assert caught.value.code is ErrorCode.VALIDATION
    assert _with_pool(checkpoint_env.dsn, lambda pool: _checkpoint_rows(pool, bad_thread_id)) == 0


@pytest.mark.parametrize(("length", "accepted"), [(SLUG_LENGTH, True), (SLUG_LENGTH + 1, False)])
def test_the_thread_id_bound_is_check_slugs_own_inclusive_bound(
    length: int, accepted: bool
) -> None:
    """Catches a thread-id check with a bound of its own — a `< 64`, or a cap
    this module hard-codes — instead of `check_slug`: at exactly 64 characters
    the two answers differ, so a stricter local bound refuses a thread id the
    naming rules accept and a looser one lets a 65-character key through.

    64 accepted and 65 refused is `check_slug`'s inclusive boundary, read off
    `SLUG_LENGTH` so the pair cannot drift from the check it mirrors."""
    thread_id = "a" * length

    if accepted:
        assert thread_config(thread_id)["configurable"]["thread_id"] == thread_id
        return

    with pytest.raises(ToolError) as caught:
        thread_config(thread_id)
    assert caught.value.code is ErrorCode.VALIDATION
