"""The Postgres checkpointer, and the only module that imports psycopg.

The LangGraph saver needs psycopg while the app uses asyncpg, so the second
driver stays here with its own pool, built once per process by the caller.
"""

import re
import uuid
from typing import Any, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool

from helpdesk.shared.core.errors import ErrorCode, ToolError

__all__ = [
    "CHECKPOINT_PRIVILEGES",
    "CHECKPOINT_TABLES",
    "COMMAND_TIMEOUT_MILLISECONDS",
    "CONNECT_TIMEOUT_SECONDS",
    "RESTRICTED_ROLE_NAME",
    "InvalidCheckpointerUrlError",
    "InvalidThreadIdError",
    "build_checkpointer",
    "build_pool",
    "grant_checkpoint_access",
    "psycopg_dsn",
    "setup_checkpointer",
    "thread_config",
    "turn_thread_id",
]

RESTRICTED_ROLE_NAME = "helpdesk_app"
"""The role the services connect as; kept in step with the role migration
by hand, so app code never imports a migration."""

ASYNCPG_SCHEME = "postgresql+asyncpg://"
"""The only URL scheme accepted; anything else is a misconfiguration."""

PSYCOPG_SCHEME = "postgresql://"
"""What psycopg understands; it does not know the `+asyncpg` suffix."""

CONNECT_TIMEOUT_SECONDS = 5.0
"""Stops a stuck handshake from hanging the turn for minutes."""

COMMAND_TIMEOUT_MILLISECONDS = 10_000
"""A server-side `statement_timeout`, so no query stays open for ever."""

_THREAD_ID_KEY = "thread_id"


class InvalidCheckpointerUrlError(ToolError):
    """A URL refused before connecting. A bare `postgresql://` is a valid DSN,
    so a loose check would connect wherever a wrong `DATABASE_URL` points."""

    code = ErrorCode.VALIDATION


def psycopg_dsn(url: str) -> str:
    """The settings' asyncpg URL as a psycopg DSN, or a typed refusal."""
    if not url.startswith(ASYNCPG_SCHEME):
        raise InvalidCheckpointerUrlError(
            f"the checkpointer needs a {ASYNCPG_SCHEME!r} URL, not {url[:32]!r}"
        )
    return PSYCOPG_SCHEME + url[len(ASYNCPG_SCHEME) :]


def build_pool(dsn: str) -> AsyncConnectionPool:
    """The pool, built but not opened. `autocommit` is required: `setup()` runs
    `CREATE INDEX CONCURRENTLY`, which fails inside a transaction."""
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
    """The saver over the caller's pool. The cast is safe: the saver opens its
    own `dict_row` cursors and never reads the pool's row factory."""
    return AsyncPostgresSaver(conn=cast(Any, pool))


CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")
"""The tables the services use. Not `checkpoint_migrations`: only `setup()`
touches that, as the owner."""

CHECKPOINT_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE")
"""DELETE is there because the saver deletes whole threads."""


async def grant_checkpoint_access(pool: AsyncConnectionPool, role_name: str) -> None:
    """Grant the services' role its rights on the checkpoint tables. Not a
    migration, since the tables exist only after `setup()`. Safe to re-run; the
    role name is an identifier, so pass the constant, never config."""
    async with pool.connection() as conn:
        for table_name in CHECKPOINT_TABLES:
            await conn.execute(
                f'GRANT {", ".join(CHECKPOINT_PRIVILEGES)} ON TABLE {table_name} TO "{role_name}"'
            )


async def setup_checkpointer(pool: AsyncConnectionPool) -> None:
    """Create LangGraph's own tables with its `setup()`, never a migration, so
    they track library upgrades. Then grant the services' role. Safe to re-run."""
    await build_checkpointer(pool).setup()
    await grant_checkpoint_access(pool, RESTRICTED_ROLE_NAME)


_THREAD_ID = re.compile(
    r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}:[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
)
"""A turn's thread: the chat's id, a colon, the turn's id."""


class InvalidThreadIdError(ToolError):
    """A thread id that is not `<session id>:<turn id>`."""

    code = ErrorCode.VALIDATION


def turn_thread_id(session_id: uuid.UUID, turn_id: uuid.UUID) -> str:
    """One turn's thread, so a saved turn never mixes with the next one."""
    return f"{session_id}:{turn_id}"


def thread_config(thread_id: str) -> RunnableConfig:
    """The config a checkpoint is saved under. The id is checked first: the
    column has no format, and a stray newline would split one turn in two."""
    if _THREAD_ID.fullmatch(thread_id) is None:
        raise InvalidThreadIdError(f"not a turn thread id: {thread_id[:80]!r}")
    return {"configurable": {_THREAD_ID_KEY: thread_id}}
