"""The two conversation tables, their pre-check and their
repository helpers.

Expected implementation surface (the implementer must provide exactly this):

``helpdesk.models.conversation`` (re-exported from ``helpdesk.models``)
    ``MAX_MESSAGE_CHARS = 4000`` — also the ``messages.content`` column width,
        so the cap and the column it guards are one number.
    ``check_message_content(value: str) -> str`` — the accepted content, or
        ``MessageTooLongError`` (a ``ToolError`` with
        ``code = ErrorCode.VALIDATION``) for an empty or oversized message.
    ``class MessageRole(StrEnum)`` — exactly ``USER``/``ASSISTANT``/``TOOL``.
    ``class Session(TenantScoped)`` — ``__tablename__ = "sessions"``; columns
        ``user_id`` (FK ``users.id``), ``started_at``, ``last_seen_at``, and
        ``session_metadata`` mapped to the DB column literally named
        ``metadata`` (the attribute cannot be ``metadata``: that name belongs
        to SQLAlchemy's declarative API).
    ``class Message(TenantScoped)`` — ``__tablename__ = "messages"``; columns
        ``session_id`` (FK ``sessions.id``), ``role``, ``content``,
        ``tool_calls``, ``citations`` and ``ordinal``, a table-wide identity
        integer that is the only total order a history window may use.

``helpdesk.core.repository``
    ``HISTORY_LIMIT = 20``
    ``async def get_or_create_session(session, *, session_id, user_id, now) -> Session``
    ``async def append_message(session, *, session_id, role, content,
        tool_calls=None, citations=None) -> Message``
    ``async def list_recent_messages(session, session_id, limit=HISTORY_LIMIT)``
        — the last ``limit`` messages of that session, OLDEST FIRST, by
        ``ordinal``.

This change EXTENDS the hand-kept ``TENANT_SCOPED_TABLES``
allowlist that ``test_models.py`` reads as the schema literal, so
``test_both_tables_are_in_the_tenant_scoped_allowlist`` stays red until
``_seed.py`` names both tables.

One session-scoped database for the whole run
(``conversation_db_url``), schema built once, two obviously fake tenants
seeded once. Per-test isolation is a namespace: every case works under its
own uuid-suffixed session id.
"""

import asyncio
import os
import subprocess
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import BigInteger, Integer, String, UniqueConstraint, text

from helpdesk.shared.core.db import create_engine, create_session_factory, tenant_session
from helpdesk.shared.core.errors import ErrorCode, ToolError
from helpdesk.shared.crud import Repository
from helpdesk.shared.crud.messages import HISTORY_LIMIT, append_message, list_recent_messages
from helpdesk.shared.crud.sessions import get_or_create_session
from helpdesk.shared.models import Base, Message, Session, TenantScoped
from helpdesk.shared.models.conversation import (
    MAX_MESSAGE_CHARS,
    MessageRole,
    MessageTooLongError,
    check_message_content,
)
from tests.conftest import TempDatabaseFactory
from tests.support.seed import TENANT_SCOPED_TABLES, SeededTenant, seed_tenant

REPO_ROOT = Path(__file__).resolve().parents[4]

CONVERSATION_METADATA_PARENT = "d8b2f6c41a97"
"""The revision BELOW `f1a7c0d4e2b9`, the one the downgrade case undoes.

Written out rather than reached with a relative `-1`: that step names whatever
sits under the CURRENT head, so it stops pointing at this revision the moment
a new one lands, and the case then asserts the wrong migration's effect."""

TENANT_A_SLUG = "conversation-tenant-a"
TENANT_B_SLUG = "conversation-tenant-b"

STARTED_AT = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
LATER = datetime(2026, 6, 1, 9, 30, tzinfo=UTC)

PLANTED_MESSAGES = 25
"""More than `HISTORY_LIMIT`, so the window has something to cut off."""


@pytest.fixture(scope="session")
def conversation_db(pg: TempDatabaseFactory) -> Iterator[tuple[str, SeededTenant, SeededTenant]]:
    """One database and two seeded tenants, built exactly once.

    Both tenants are fully seeded because the isolation case asks BOTH of them
    for the same rows: a guard that returned nothing at all would otherwise
    look like a guard that works.
    """
    url = pg.create()

    async def prepare() -> tuple[SeededTenant, SeededTenant]:
        engine = create_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            factory = create_session_factory(engine)
            return (
                await seed_tenant(factory, TENANT_A_SLUG),
                await seed_tenant(factory, TENANT_B_SLUG),
            )
        finally:
            await engine.dispose()

    tenant_a, tenant_b = asyncio.run(prepare())
    yield url, tenant_a, tenant_b
    pg.drop(url)


# --- metadata shape (no database needed) --------------------------------------


def test_both_conversation_tables_are_in_the_metadata() -> None:
    """Catches models declared in their own module but never imported into
    `helpdesk.models`, which leaves `Base.metadata.create_all` and the Alembic
    autogenerate comparison both blind to the two tables."""
    assert "sessions" in Base.metadata.tables
    assert "messages" in Base.metadata.tables
    assert Session.__tablename__ == "sessions"
    assert Message.__tablename__ == "messages"


def test_both_tables_are_in_the_tenant_scoped_allowlist() -> None:
    """Catches a forgotten allowlist entry: two tables added to the metadata
    without extending `_seed.py::TENANT_SCOPED_TABLES`, which leaves
    `test_models.py`'s schema-literal sweep red for a later plan to find."""
    assert "sessions" in TENANT_SCOPED_TABLES
    assert "messages" in TENANT_SCOPED_TABLES


def test_the_session_columns_are_the_spec_6_columns() -> None:
    """Catches a `sessions` table missing `user_id`, `started_at` or
    `last_seen_at` — without the last of these nothing can expire an idle
    conversation, and without `user_id` a session belongs to a tenant but to
    nobody in it."""
    columns = Base.metadata.tables["sessions"].c
    for name in ("tenant_id", "user_id", "started_at", "last_seen_at"):
        assert name in columns, name
        assert not columns[name].nullable, name
    assert {fk.column.table.name for fk in columns.user_id.foreign_keys} == {"users"}


def test_the_session_metadata_column_is_named_metadata_in_the_database() -> None:
    """Catches a renamed attribute: the attribute must be
    `session_metadata` (because `metadata` is SQLAlchemy's own declarative
    attribute) while the COLUMN keeps the name `metadata`. An
    implementation that lets the attribute name reach the DDL ships a column
    no documented query or migration can find."""
    columns = Base.metadata.tables["sessions"].c
    assert "metadata" in columns
    assert "session_metadata" not in columns
    assert Session.__mapper__.columns["session_metadata"].name == "metadata"


def test_the_message_columns_are_the_spec_6_columns() -> None:
    """Catches a `messages` table missing the payload columns the table must have:
    without `citations` a policy-based answer cannot carry what it was based
    on, and without `tool_calls` the transcript cannot show what the turn
    actually did."""
    columns = Base.metadata.tables["messages"].c
    for name in ("tenant_id", "session_id", "role", "content"):
        assert name in columns, name
        assert not columns[name].nullable, name
    for name in ("tool_calls", "citations"):
        assert name in columns, name
        assert columns[name].nullable, name
    assert {fk.column.table.name for fk in columns.session_id.foreign_keys} == {"sessions"}


def test_the_message_tenant_id_is_present_so_the_orm_guard_covers_it() -> None:
    """Catches `tenant_id` being dropped from the literal column list:
    the guard in `helpdesk.core.db` is per-model, so a `messages` table
    without `tenant_id` is an unguarded cross-tenant read whose only
    protection is that every query happens to join its parent."""
    assert "tenant_id" in Base.metadata.tables["messages"].c
    assert issubclass(Message, TenantScoped)
    assert issubclass(Session, TenantScoped)


def test_max_message_chars_is_exactly_the_planned_value() -> None:
    """Catches a cap quietly widened or narrowed, which moves
    the boundary every other case in this file asserts against."""
    assert MAX_MESSAGE_CHARS == 4000
    assert HISTORY_LIMIT == 20


def test_the_content_column_is_exactly_the_cap_wide() -> None:
    """Catches a column sized with its own literal instead of the constant the
    pre-check reads: that is exactly how a message the check accepts becomes a
    truncation error raised half-way through the write."""
    column_type = Base.metadata.tables["messages"].c.content.type
    assert isinstance(column_type, String)
    assert column_type.length == MAX_MESSAGE_CHARS


def test_the_ordinal_column_is_a_unique_identity_integer() -> None:
    """Catches an `ordinal` that is an ordinary nullable integer the caller
    must number itself, or a non-unique one: the history window has no other
    total order, because Postgres `now()` is transaction time and every row a
    turn writes shares `created_at`."""
    table = Base.metadata.tables["messages"]
    column = table.c.ordinal
    assert isinstance(column.type, BigInteger | Integer)
    assert not column.nullable
    assert column.identity is not None, "ordinal must be database-generated, not caller-supplied"
    unique_constraint = any(
        isinstance(constraint, UniqueConstraint) and list(constraint.columns) == [column]
        for constraint in table.constraints
    )
    unique_index = any(index.unique and list(index.columns) == [column] for index in table.indexes)
    assert column.unique or unique_constraint or unique_index


def test_message_role_has_exactly_the_three_spec_members() -> None:
    """Catches the enum drifting from `user`/`assistant`/`tool` — a
    fourth member, or a renamed value, silently changes what a stored
    transcript means and what the history window renders."""
    assert {role.value for role in MessageRole} == {"user", "assistant", "tool"}
    assert MessageRole.USER.value == "user"
    assert MessageRole.ASSISTANT.value == "assistant"
    assert MessageRole.TOOL.value == "tool"


# --- the content pre-check (pure) ---------------------------------------------


def test_check_message_content_accepts_content_of_exactly_the_cap() -> None:
    """The inclusive half of the boundary: catches an off-by-one `>=` that
    refuses the longest message the column can hold, so a legitimate turn is
    answered `VALIDATION`."""
    at_cap = "x" * MAX_MESSAGE_CHARS
    assert check_message_content(at_cap) == at_cap


def test_check_message_content_refuses_one_character_over_the_cap() -> None:
    """The exclusive half: catches a missing pre-check, which turns an
    oversized message into a Postgres truncation error raised after the
    session row was already written."""
    with pytest.raises(MessageTooLongError) as excinfo:
        check_message_content("x" * (MAX_MESSAGE_CHARS + 1))
    assert excinfo.value.code is ErrorCode.VALIDATION


def test_check_message_content_refuses_the_empty_string() -> None:
    """Catches a check that only looks at the upper bound: an empty user turn
    would otherwise be stored and sent to the model as a message with no
    content."""
    with pytest.raises(MessageTooLongError):
        check_message_content("")


def test_the_message_error_is_a_typed_tool_error() -> None:
    """Checks the code, not the number: catches an error raised as
    a bare `ValueError`, which crosses the tool boundary as free text instead
    of `VALIDATION`."""
    assert issubclass(MessageTooLongError, ToolError)
    assert MessageTooLongError.code is ErrorCode.VALIDATION


# --- repository helpers, against Postgres -------------------------------------


def test_get_or_create_session_creates_the_row_under_the_bound_tenant(
    conversation_db: tuple[str, SeededTenant, SeededTenant],
) -> None:
    """Catches a helper that ignores the id it was given and lets the model's
    uuid4 default pick one: the gateway's `X-Session` and the stored row would
    then name different conversations from the first turn onward."""
    url, tenant_a, _ = conversation_db
    session_id = uuid.uuid4()

    async def scenario() -> None:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, tenant_a.tenant_id) as db:
                row = await get_or_create_session(
                    db, session_id=session_id, user_id=tenant_a.user_id, now=STARTED_AT
                )
                assert row.id == session_id
                assert row.tenant_id == tenant_a.tenant_id
                assert row.user_id == tenant_a.user_id
                assert row.last_seen_at == STARTED_AT
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_get_or_create_session_reuses_the_row_and_moves_last_seen_at(
    conversation_db: tuple[str, SeededTenant, SeededTenant],
) -> None:
    """Catches a helper that inserts on every turn instead of loading — the
    conversation would restart at message one each time — and one that loads
    without touching `last_seen_at`, which leaves every session looking idle
    since its first turn."""
    url, tenant_a, _ = conversation_db
    session_id = uuid.uuid4()

    async def scenario() -> None:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, tenant_a.tenant_id) as db:
                first = await get_or_create_session(
                    db, session_id=session_id, user_id=tenant_a.user_id, now=STARTED_AT
                )
                started = first.started_at
            async with tenant_session(factory, tenant_a.tenant_id) as db:
                second = await get_or_create_session(
                    db, session_id=session_id, user_id=tenant_a.user_id, now=LATER
                )
                assert second.id == session_id
                assert second.started_at == started
                assert second.last_seen_at == LATER
            async with tenant_session(factory, tenant_a.tenant_id) as db:
                rows = await Repository(db, Session).list()
                assert [row.id for row in rows].count(session_id) == 1
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_list_recent_messages_returns_the_last_twenty_oldest_first(
    conversation_db: tuple[str, SeededTenant, SeededTenant],
) -> None:
    """Catches a window ordered by `created_at`: all twenty-five rows are
    written in ONE transaction, so Postgres stamps them with one identical
    `now()` and only `ordinal` can order them. The premise is asserted first,
    so the case cannot silently stop being the one that distinguishes the two
    orderings. Also catches a newest-first window, which would feed the model
    the conversation backwards."""
    url, tenant_a, _ = conversation_db
    session_id = uuid.uuid4()

    async def scenario() -> None:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, tenant_a.tenant_id) as db:
                await get_or_create_session(
                    db, session_id=session_id, user_id=tenant_a.user_id, now=STARTED_AT
                )
                for index in range(PLANTED_MESSAGES):
                    await append_message(
                        db,
                        session_id=session_id,
                        role=MessageRole.USER,
                        content=f"planted message {index:02d}",
                    )

            async with tenant_session(factory, tenant_a.tenant_id) as db:
                rows = await list_recent_messages(db, session_id)
                assert len({row.created_at for row in rows}) == 1, (
                    "premise: one transaction stamps one created_at, so ordinal is "
                    "the only order that can distinguish these rows"
                )
                assert len(rows) == HISTORY_LIMIT
                assert [row.content for row in rows] == [
                    f"planted message {index:02d}"
                    for index in range(PLANTED_MESSAGES - HISTORY_LIMIT, PLANTED_MESSAGES)
                ]
                ordinals = [row.ordinal for row in rows]
                assert ordinals == sorted(ordinals)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_another_tenants_sessions_and_messages_are_invisible(
    conversation_db: tuple[str, SeededTenant, SeededTenant],
) -> None:
    """Catches either model declared on plain `Base` rather than
    `TenantScoped`: the per-model ORM guard keys on that base, so a table with
    a `tenant_id` column but the wrong base reads every tenant's conversation
    from any tenant's session. Tenant B holds its own session and message, so
    a guard that returned nothing at all fails the same case."""
    url, tenant_a, tenant_b = conversation_db
    session_a = uuid.uuid4()
    session_b = uuid.uuid4()

    async def scenario() -> None:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, tenant_a.tenant_id) as db:
                await get_or_create_session(
                    db, session_id=session_a, user_id=tenant_a.user_id, now=STARTED_AT
                )
                await append_message(
                    db,
                    session_id=session_a,
                    role=MessageRole.USER,
                    content="tenant a only",
                )
            async with tenant_session(factory, tenant_b.tenant_id) as db:
                await get_or_create_session(
                    db, session_id=session_b, user_id=tenant_b.user_id, now=STARTED_AT
                )
                await append_message(
                    db,
                    session_id=session_b,
                    role=MessageRole.USER,
                    content="tenant b only",
                )

            async with tenant_session(factory, tenant_b.tenant_id) as db:
                assert await Repository(db, Session).get(session_a) is None
                own_sessions = {row.id for row in await Repository(db, Session).list()}
                assert session_b in own_sessions
                assert session_a not in own_sessions
                assert len(await list_recent_messages(db, session_a)) == 0
                contents = {row.content for row in await Repository(db, Message).list()}
                assert contents == {"tenant b only"}
        finally:
            await engine.dispose()

    asyncio.run(scenario())


# --- the metadata column matches the model ------------------


def _alembic(*arguments: str, database_url: str) -> None:
    result = subprocess.run(
        ["uv", "run", "alembic", *arguments],
        cwd=REPO_ROOT / "backend",  # alembic.ini lives in backend/
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"alembic {' '.join(arguments)} failed:\n{result.stderr}"


def _metadata_is_nullable(database_url: str) -> bool:
    """`sessions.metadata` as the DATABASE describes it, not as the model does."""

    async def scenario() -> bool:
        engine = create_engine(database_url)
        try:
            async with engine.connect() as conn:
                answer = (
                    await conn.execute(
                        text(
                            "SELECT is_nullable FROM information_schema.columns "
                            "WHERE table_name = 'sessions' AND column_name = 'metadata'"
                        )
                    )
                ).scalar_one()
            return str(answer) == "YES"
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


@pytest.mark.slow  # 2.3s call, runs the conversation migration up and down
def test_the_metadata_column_is_not_null_after_the_migration_and_nullable_after_its_downgrade(
    fresh_db_url: str,
) -> None:
    """The metadata column matches the model, both directions.

    `models/conversation.py` maps the attribute as `Mapped[dict[str, Any]]`,
    which SQLAlchemy renders NOT NULL, while the conversation revision created
    the column `nullable=True`. Nothing in the suite could see the difference:
    every case here builds its schema from the metadata rather than from
    Alembic, so the drift lived only in a migrated database.

    Catches a revision whose `upgrade` does not reach the column, and one whose
    `downgrade` is not its inverse - a downgrade that left NOT NULL standing
    would make the revision unrepeatable on any database that ran it once.

    The downgrade names the revision BELOW the one under test rather than
    stepping `-1`. A relative step means a different revision on the day a new
    head lands, which is how this case broke once when a revision was added.
    """
    _alembic("upgrade", "head", database_url=fresh_db_url)
    assert _metadata_is_nullable(fresh_db_url) is False

    _alembic("downgrade", CONVERSATION_METADATA_PARENT, database_url=fresh_db_url)
    assert _metadata_is_nullable(fresh_db_url) is True

    _alembic("upgrade", "head", database_url=fresh_db_url)
    assert _metadata_is_nullable(fresh_db_url) is False
