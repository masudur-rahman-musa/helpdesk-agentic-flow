"""The Postgres checkpointer, and the only module that knows psycopg exists.

An earlier change decision C: `langgraph-checkpoint-postgres` is psycopg3-only while
every other database access in this repo is SQLAlchemy over asyncpg. Rather
than hand-writing a saver against the library's checkpoint protocol, psycopg is
carried for the checkpointer alone, with its own small pool, and it is confined
to this file so the second driver cannot spread: nothing else in `agent_api`
imports psycopg, and `build.py` takes a saver it is handed rather than building
one.

Decision B: the checkpoint tables are LangGraph's own, created by the library's
`setup()` from the migrate target (`make migrate`), never hand-copied into an
Alembic revision. A revision would drift on every library upgrade and would
make the library's own migration path unusable; running it from the migrate
target rather than from application startup is also what keeps several starting
processes from racing on `CREATE TABLE`.

The pool carries BOTH timeout halves: a connect timeout, so
a checkpoint write against a wedged Postgres cannot hang the turn for the OS
default, and a `statement_timeout`, so a connection that did open cannot hold a
query open for ever. Both are asserted on the pool this module CONSTRUCTS by
`test_agent_checkpointer.py::test_the_pool_carries_both_timeout_halves`.

The pool is deliberately NOT opened here (decision D): one process-wide pool is
built at startup and passed into `run_turn`, and the caller owns opening and
closing it. A pool per turn would be a connection storm and a fresh handshake
per message; a pool opened in this function would connect during a call the
plan's own tests need to make no connection at all.
"""

from typing import Any, cast

from helpdesk.errors import ErrorCode, ToolError
from helpdesk.rag.naming import check_slug
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool

__all__ = [
    "COMMAND_TIMEOUT_MILLISECONDS",
    "CONNECT_TIMEOUT_SECONDS",
    "InvalidCheckpointerUrlError",
    "build_checkpointer",
    "build_pool",
    "psycopg_dsn",
    "setup_checkpointer",
    "thread_config",
]

ASYNCPG_SCHEME = "postgresql+asyncpg://"
"""The one URL scheme the settings carry and this module converts.

A prefix rather than a parse: the settings' `database_url` is what
`helpdesk.core.db.create_engine` is given, so anything else here is a
misconfiguration, not a second supported driver.
"""

PSYCOPG_SCHEME = "postgresql://"
"""What psycopg understands. SQLAlchemy's `+asyncpg` suffix it does not."""

CONNECT_TIMEOUT_SECONDS = 5.0
"""How long one connection attempt may take.

Passed to libpq as `connect_timeout`. Without it a checkpoint write against a
Postgres that accepts the TCP connection and never completes the handshake
hangs the turn for the operating system's default, which is minutes.
Pinned on the CONSTRUCTED pool by
`test_agent_checkpointer.py::test_the_pool_carries_both_timeout_halves`.
"""

COMMAND_TIMEOUT_MILLISECONDS = 10_000
"""How long one statement may run before the server cancels it.

The read half of the pair, expressed the way Postgres takes it: a
`statement_timeout` server option, in milliseconds, so ten seconds is 10000.
It is a SERVER-side cancel rather than a client-side wait, which is what makes
it also the answer to the plan's second risk — a second driver into the same
database whose connection sits on an open query for ever. Pinned on the
constructed pool by the same test as the connect half.
"""

_THREAD_ID_KEY = "thread_id"


class InvalidCheckpointerUrlError(ToolError):
    """A database URL this module will not turn into a psycopg DSN.

    Raised BEFORE any connection is opened, which is the whole point: a bare
    `postgresql://` URL is a DSN psycopg would happily accept, so a permissive
    conversion would connect to whatever host a misconfigured `DATABASE_URL`
    names instead of refusing it. Pinned, with both socket entry points
    poisoned, by
    `test_agent_checkpointer.py::test_a_url_of_any_other_scheme_is_refused_before_a_connection_is_opened`.
    """

    code = ErrorCode.VALIDATION


def psycopg_dsn(url: str) -> str:
    """The settings' asyncpg URL as the DSN psycopg takes, or a typed refusal.

    Pure: a prefix swap and nothing else, so a URL with no port, no password or
    no path converts the same way a full one does. Pinned by
    `test_agent_checkpointer.py::test_psycopg_dsn_converts_the_asyncpg_url`.
    """
    if not url.startswith(ASYNCPG_SCHEME):
        raise InvalidCheckpointerUrlError(
            f"the checkpointer needs a {ASYNCPG_SCHEME!r} URL, not {url[:32]!r}"
        )
    return PSYCOPG_SCHEME + url[len(ASYNCPG_SCHEME) :]


def build_pool(dsn: str) -> AsyncConnectionPool:
    """The checkpointer's own pool, constructed and NOT opened.

    `open=False` on purpose: the caller opens it (`async with pool:`) and closes
    it, so the process-wide pool of decision D has one owner, and so
    `build_pool` itself makes no connection.

    `autocommit=True` is a requirement of the library, not a preference:
    `AsyncPostgresSaver.setup()` runs `CREATE INDEX CONCURRENTLY`, which
    Postgres refuses inside a transaction block (a project rule — a library
    default this diff relies on is a decision, recorded next to the call).
    The row factory is deliberately NOT set here: the saver opens its own
    cursors with `row_factory=dict_row`, so setting it on the pool would only
    change what every other reader of this pool sees.
    """
    return AsyncConnectionPool(
        conninfo=dsn,
        open=False,
        kwargs={
            "autocommit": True,
            "connect_timeout": CONNECT_TIMEOUT_SECONDS,
            "options": f"-c statement_timeout={COMMAND_TIMEOUT_MILLISECONDS}",
        },
    )


def build_checkpointer(pool: AsyncConnectionPool) -> AsyncPostgresSaver:
    """The saver `build_graph` compiles with, over a pool the caller owns.

    The cast is the price of `build_pool` leaving the row factory alone. The
    saver's annotation asks for a pool of `dict_row` connections, because the
    constructor it ships with builds one that way — but every statement it runs
    opens its OWN `conn.cursor(row_factory=dict_row)`, so the pool's factory is
    not what it reads its rows through. Setting `dict_row` on the pool to
    satisfy the annotation would change what every other reader of this pool
    sees, for a property the library does not actually rely on.
    """
    return AsyncPostgresSaver(conn=cast(Any, pool))


async def setup_checkpointer(pool: AsyncConnectionPool) -> None:
    """Create LangGraph's own checkpoint tables (decision B).

    Idempotent, because the library's migrations are: every statement is an
    `IF NOT EXISTS` and the applied version is read back from
    `checkpoint_migrations` before anything runs. Pinned by
    `test_agent_checkpointer.py::test_setup_is_idempotent`, which compares the
    table set across two calls.
    """
    await build_checkpointer(pool).setup()


def thread_config(thread_id: str) -> RunnableConfig:
    """The config a checkpoint is written under, thread id pre-checked.

    The thread id is the only value this plan puts into a store key, and
    `checkpoints.thread_id` has no pattern and no width of its own, so
    `check_slug` runs BEFORE the config exists rather than after a write has
    landed under a name nothing can address again (a project rule and 4: a
    trailing newline would make one session two checkpoint threads). The bound
    is `check_slug`'s own, not a local one, pinned inclusively at 64 and 65 by
    `test_agent_checkpointer.py::test_the_thread_id_bound_is_check_slugs_own_inclusive_bound`,
    and the refusal-before-write half by
    `::test_a_thread_id_that_is_not_a_slug_is_refused_before_any_checkpoint_write`.
    """
    return {"configurable": {_THREAD_ID_KEY: check_slug(thread_id)}}
