"""An earlier change, sections 1 and 6: the three helpdesk mutating tools, through the transport.

Surface under test (written before it exists):

``mcp_server.tools.helpdesk``
    ``reset_own_password(idempotency_key)`` — simulated reset (the design):
    sets ``users.password_reset_requested_at`` on the TOKEN's user and returns
    a one-time token display placeholder that is obviously fake. Declared
    audit event ``user.password_reset``.
    ``unlock_own_account(idempotency_key)`` — sets
    ``users.account_unlocked_at`` on the token's user. Declared audit event
    ``user.account_unlocked``.
    ``create_ticket(kind, title, body, idempotency_key)`` — persists a ticket
    row carrying the token's ``tenant_id`` and ``user_id``, status ``open``.
    ``kind`` is exact enum membership over the four the design values, ``title``
    is at most 256 (the column's own bound), ``body`` at most 10_000 (plan
    10b decision C). Declared audit event ``ticket.created``.

    All three input models forbid extra fields: a ``user_id``
    or ``tenant_id`` in the arguments is ``VALIDATION`` with no effect run.

``mcp_server.pipeline`` (the ``mutating`` branch)
    Every mutating call requires ``idempotency_key`` and runs its body inside
    ``run_once``: a replay with the same key and arguments returns the
    byte-identical response, runs no second effect and writes
    ``tool.replayed`` — never the declared event a
    second time. The kill switch stays ahead of the guard, so a stopped call
    burns no key.

Fixture cost: everything rides an earlier change's session-scoped
``mcp_fixtures`` seam — one seeded throwaway database, one dedicated Redis
index flushed per test. Per-test isolation inside the shared database is a
namespace: a unique idempotency key and a unique ticket title per test.

No pytest-asyncio: each case is a sync function driving one ``asyncio.run``.
"""

import asyncio
import json
import uuid
from typing import Any

import pytest
from mcp_fixtures import (  # noqa: F401 - fixtures are resolved by name
    SeededMcp,
    bearer,
    call_tool,
    error_code,
    mcp_env,
    mcp_pg,
    mcp_redis_base_url,
    mcp_redis_url,
    seeded_mcp_db,
    token_for,
)
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from helpdesk.shared.killswitch import GLOBAL, KillSwitch
from helpdesk.shared.models.idempotency import KEY_LENGTH

# The three wire names, spelled once. Sent as strings through the transport on
# purpose: an unregistered name fails at the registry, which is the right
# failure for a not-yet-implemented tool.
RESET = "reset_own_password"
UNLOCK = "unlock_own_account"
CREATE_TICKET = "create_ticket"

# The declared audit event types.
RESET_EVENT = "user.password_reset"
UNLOCK_EVENT = "user.account_unlocked"
TICKET_EVENT = "ticket.created"
REPLAYED_EVENT = "tool.replayed"

# The two flag columns decision A adds to `users`. An allowlist, not free
# text: `_user_flag` interpolates one of exactly these into SQL.
RESET_FLAG = "password_reset_requested_at"
UNLOCK_FLAG = "account_unlocked_at"
_FLAG_COLUMNS = frozenset({RESET_FLAG, UNLOCK_FLAG})

# The caps under test, asserted at their exact inclusive boundaries
#. Title is the column's String(256); body is plan
# 10b decision C's Pydantic max_length.
TITLE_MAX = 256
BODY_MAX = 10_000


def _key() -> str:
    """A unique idempotency key: the per-test namespace inside the shared DB."""
    return f"fake-key-{uuid.uuid4().hex}"


def _title() -> str:
    """A unique ticket title: how each test finds only its own rows."""
    return f"fake ticket {uuid.uuid4().hex}"


def _ticket_args(
    title: str, *, key: str, kind: str = "general", body: str = "fake body"
) -> dict[str, Any]:
    return {"kind": kind, "title": title, "body": body, "idempotency_key": key}


def _structured(result: Any) -> dict[str, Any]:
    assert result.is_error is False, result.content
    assert result.structured_content is not None, "a successful call carries structured content"
    content: dict[str, Any] = result.structured_content
    return content


def _response_text(result: Any) -> str:
    """The single text block of a successful result, for byte-identity checks."""
    assert result.is_error is False, result.content
    assert len(result.content) == 1, result.content
    return str(result.content[0].text)


# --- raw-SQL probes (reading around the tool, never through it) ----------------


def _rows(database_url: str, sql: str, params: dict[str, Any]) -> list[Any]:
    async def scenario() -> list[Any]:
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as conn:
                result = await conn.execute(text(sql), params)
                return list(result)
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _audit_event_types(database_url: str, tenant_id: uuid.UUID) -> list[str]:
    rows = _rows(
        database_url,
        "SELECT event_type FROM audit_events WHERE tenant_id = :tenant ORDER BY seq",
        {"tenant": tenant_id},
    )
    return [str(row[0]) for row in rows]


def _user_flag(database_url: str, user_id: uuid.UUID, column: str) -> Any:
    assert column in _FLAG_COLUMNS
    rows = _rows(
        database_url,
        f"SELECT {column} FROM users WHERE id = :id",
        {"id": user_id},
    )
    assert len(rows) == 1
    return rows[0][0]


def _tickets_titled(database_url: str, title: str) -> list[Any]:
    return _rows(
        database_url,
        "SELECT tenant_id, user_id, kind, title, body, status FROM tickets WHERE title = :title",
        {"title": title},
    )


def _idempotency_row_count(database_url: str, key: str) -> int:
    rows = _rows(
        database_url,
        "SELECT count(*) FROM idempotency_keys WHERE key = :key",
        {"key": key},
    )
    return int(rows[0][0])


def _trip_global_switch(redis_url: str, tool: str) -> None:
    async def scenario() -> None:
        client: Redis = Redis.from_url(redis_url, socket_connect_timeout=2, socket_timeout=2)
        try:
            await KillSwitch(client).trip(
                GLOBAL, tool, "fake incident 0002, tools paused", "fake-admin@example.invalid"
            )
        finally:
            await client.aclose()

    asyncio.run(scenario())


# --- happy paths (each also pins its declared event, written exactly once) -----


def test_reset_own_password_flags_the_token_user_and_returns_a_fake_placeholder(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches an implementation that registers the tool but never writes
    `password_reset_requested_at` on the caller's row, returns a placeholder
    that does not announce itself as fake, or writes an event other than one
    `user.password_reset`."""
    caller = mcp_env.probe
    flag_before = _user_flag(mcp_env.database_url, caller.user_id, RESET_FLAG)
    events_before = _audit_event_types(mcp_env.database_url, caller.tenant_id)

    result = call_tool(bearer(token_for(caller)), RESET, {"idempotency_key": _key()})

    payload = _structured(result)
    # the design "simulated reset": the placeholder must be obviously fake, so
    # it has to say so — a realistic-looking one-time token fails here.
    assert "fake" in json.dumps(payload).lower(), payload
    flag_after = _user_flag(mcp_env.database_url, caller.user_id, RESET_FLAG)
    assert flag_after is not None
    assert flag_after != flag_before
    events_after = _audit_event_types(mcp_env.database_url, caller.tenant_id)
    assert events_after == [*events_before, RESET_EVENT]


def test_unlock_own_account_flags_the_token_user(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches an implementation that never writes `account_unlocked_at` on
    the caller's own row (or writes it on some other user, whose caller-row
    flag then stays unset), or writes an event other than one
    `user.account_unlocked`."""
    caller = mcp_env.tenant_a
    flag_before = _user_flag(mcp_env.database_url, caller.user_id, UNLOCK_FLAG)
    events_before = _audit_event_types(mcp_env.database_url, caller.tenant_id)

    result = call_tool(bearer(token_for(caller)), UNLOCK, {"idempotency_key": _key()})

    assert result.is_error is False, result.content
    flag_after = _user_flag(mcp_env.database_url, caller.user_id, UNLOCK_FLAG)
    assert flag_after is not None
    assert flag_after != flag_before
    events_after = _audit_event_types(mcp_env.database_url, caller.tenant_id)
    assert events_after == [*events_before, UNLOCK_EVENT]


def test_create_ticket_persists_a_row_for_the_token_user(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches an implementation that stores the wrong tenant or user on the
    row, a status other than `open`, mangles kind/title/body on the way to
    the table, or writes an event other than one `ticket.created`."""
    caller = mcp_env.probe
    title = _title()
    events_before = _audit_event_types(mcp_env.database_url, caller.tenant_id)

    result = call_tool(
        bearer(token_for(caller)),
        CREATE_TICKET,
        _ticket_args(title, key=_key(), body="fake ticket body"),
    )

    assert result.is_error is False, result.content
    rows = _tickets_titled(mcp_env.database_url, title)
    assert len(rows) == 1, rows
    row = rows[0]
    assert uuid.UUID(str(row.tenant_id)) == caller.tenant_id
    assert uuid.UUID(str(row.user_id)) == caller.user_id
    assert row.kind == "general"
    assert row.title == title
    assert row.body == "fake ticket body"
    assert row.status == "open"
    events_after = _audit_event_types(mcp_env.database_url, caller.tenant_id)
    assert events_after == [*events_before, TICKET_EVENT]


# --- a project rule / a project rule ---------------------------------------------


@pytest.mark.parametrize("tool", [RESET, UNLOCK, CREATE_TICKET])
def test_a_user_id_in_the_arguments_is_refused_with_no_effect(
    mcp_env: SeededMcp,  # noqa: F811
    tool: str,
) -> None:
    """Catches an input model that ignores (or honours) a `user_id` and
    `tenant_id` argument instead of forbidding extras: identity comes only
    from the JWT.

    The caller and the named user are in the SAME tenant,
    so an implementation that honoured the argument would flag or file for
    the colleague rather than being saved by the tenant guard.
    """
    caller = mcp_env.tenant_a
    colleague = mcp_env.probe
    assert caller.tenant_id == colleague.tenant_id, "the point of this test"
    assert caller.user_id != colleague.user_id
    key = _key()
    title = _title()
    flags_before = {
        (user.user_id, column): _user_flag(mcp_env.database_url, user.user_id, column)
        for user in (caller, colleague)
        for column in _FLAG_COLUMNS
    }
    arguments: dict[str, Any] = (
        _ticket_args(title, key=key) if tool == CREATE_TICKET else {"idempotency_key": key}
    )
    arguments["user_id"] = str(colleague.user_id)
    arguments["tenant_id"] = str(colleague.tenant_id)

    result = call_tool(bearer(token_for(caller)), tool, arguments)

    assert error_code(result) == "VALIDATION"
    for (user_id, column), before in flags_before.items():
        assert _user_flag(mcp_env.database_url, user_id, column) == before, (user_id, column)
    assert _tickets_titled(mcp_env.database_url, title) == []
    assert _idempotency_row_count(mcp_env.database_url, key) == 0


# --- exact boundaries ----------------------------------------------------------


@pytest.mark.parametrize(
    ("length", "accepted"),
    [
        pytest.param(TITLE_MAX, True, id="title-256-ok"),
        pytest.param(TITLE_MAX + 1, False, id="title-257-refused"),
    ],
)
def test_the_title_boundary_is_inclusive_at_256(
    mcp_env: SeededMcp,  # noqa: F811
    length: int,
    accepted: bool,
) -> None:
    """Catches a title cap that is missing, off by one, or narrower than the
    column: 257 left unchecked would abort the flush after the effect as a
    free-text database error."""
    title = f"fake title {uuid.uuid4().hex} ".ljust(length, "x")
    assert len(title) == length

    result = call_tool(
        bearer(token_for(mcp_env.probe)), CREATE_TICKET, _ticket_args(title, key=_key())
    )

    rows = _tickets_titled(mcp_env.database_url, title)
    if accepted:
        assert result.is_error is False, result.content
        assert len(rows) == 1
    else:
        assert error_code(result) == "VALIDATION"
        assert rows == []


@pytest.mark.parametrize(
    ("length", "accepted"),
    [
        pytest.param(BODY_MAX, True, id="body-10000-ok"),
        pytest.param(BODY_MAX + 1, False, id="body-10001-refused"),
    ],
)
def test_the_body_boundary_is_inclusive_at_10000(
    mcp_env: SeededMcp,  # noqa: F811
    length: int,
    accepted: bool,
) -> None:
    """Catches decision C being dropped (the column is unbounded Text, so
    nothing else refuses an oversized body) or its cap drifting off 10_000."""
    title = _title()
    body = "b" * length

    result = call_tool(
        bearer(token_for(mcp_env.probe)), CREATE_TICKET, _ticket_args(title, key=_key(), body=body)
    )

    rows = _tickets_titled(mcp_env.database_url, title)
    if accepted:
        assert result.is_error is False, result.content
        assert len(rows) == 1
        assert len(rows[0].body) == BODY_MAX
    else:
        assert error_code(result) == "VALIDATION"
        assert rows == []


def test_a_key_at_exactly_128_is_accepted(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches a key pre-check that undercuts the column: the boundary is
    inclusive at the guard's own KEY_LENGTH."""
    assert KEY_LENGTH == 128
    key = f"fake-key-{uuid.uuid4().hex}".ljust(KEY_LENGTH, "k")
    assert len(key) == KEY_LENGTH
    title = _title()

    result = call_tool(
        bearer(token_for(mcp_env.probe)), CREATE_TICKET, _ticket_args(title, key=key)
    )

    assert result.is_error is False, result.content
    assert len(_tickets_titled(mcp_env.database_url, title)) == 1
    assert _idempotency_row_count(mcp_env.database_url, key) == 1


def test_an_oversized_key_is_refused_before_the_effect_and_stores_nothing(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches the guard's pre-check running after the effect (a ticket row
    would exist) or the refusal leaving a key row behind: at 129 the answer
    is VALIDATION with no effect run and nothing stored."""
    key = f"fake-key-{uuid.uuid4().hex}".ljust(KEY_LENGTH + 1, "k")
    assert len(key) == KEY_LENGTH + 1
    title = _title()

    result = call_tool(
        bearer(token_for(mcp_env.probe)), CREATE_TICKET, _ticket_args(title, key=key)
    )

    assert error_code(result) == "VALIDATION"
    assert _tickets_titled(mcp_env.database_url, title) == []
    assert _idempotency_row_count(mcp_env.database_url, key) == 0


@pytest.mark.parametrize(
    "kind",
    ["bad", "general\n", "general\r\n", "general ", ""],
    ids=["unknown", "trailing-newline", "trailing-crlf", "trailing-space", "empty"],
)
def test_a_kind_outside_the_enum_is_refused_with_no_row(
    mcp_env: SeededMcp,  # noqa: F811
    kind: str,
) -> None:
    """Catches kind checked by anything weaker than exact enum membership: a
    prefix match, a stripped compare, or a `$`-anchored regex each admit one
    of these rows (a project rule's shape)."""
    title = _title()

    result = call_tool(
        bearer(token_for(mcp_env.probe)), CREATE_TICKET, _ticket_args(title, key=_key(), kind=kind)
    )

    assert error_code(result) == "VALIDATION"
    assert _tickets_titled(mcp_env.database_url, title) == []


# --- idempotency through the pipeline ------------------------------------------


def test_a_replayed_create_ticket_is_byte_identical_and_files_one_ticket(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches the pipeline ignoring the `mutating` flag (the effect runs
    twice, two rows), a replay response rebuilt rather than replayed (bytes
    differ), or a replay writing `ticket.created` a second time — a second
    creation event for one ticket is a false record (decision B)."""
    caller = mcp_env.probe
    key = _key()
    title = _title()
    arguments = _ticket_args(title, key=key)
    events_before = _audit_event_types(mcp_env.database_url, caller.tenant_id)

    first = call_tool(bearer(token_for(caller)), CREATE_TICKET, arguments)
    second = call_tool(bearer(token_for(caller)), CREATE_TICKET, arguments)

    assert first.is_error is False, first.content
    assert second.is_error is False, second.content
    assert _response_text(second) == _response_text(first)
    assert second.structured_content == first.structured_content
    assert len(_tickets_titled(mcp_env.database_url, title)) == 1
    events_after = _audit_event_types(mcp_env.database_url, caller.tenant_id)
    assert events_after == [*events_before, TICKET_EVENT, REPLAYED_EVENT]
    delta = events_after[len(events_before) :]
    assert delta.count(TICKET_EVENT) == 1, "the declared event must never be written twice"


def test_a_replayed_reset_writes_the_flag_once(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches a replay that re-runs the flag write (the timestamp moves) or
    logs the declared event again instead of `tool.replayed`."""
    caller = mcp_env.probe
    key = _key()
    arguments = {"idempotency_key": key}
    events_before = _audit_event_types(mcp_env.database_url, caller.tenant_id)

    first = call_tool(bearer(token_for(caller)), RESET, arguments)
    flag_after_first = _user_flag(mcp_env.database_url, caller.user_id, RESET_FLAG)
    second = call_tool(bearer(token_for(caller)), RESET, arguments)

    assert first.is_error is False, first.content
    assert second.is_error is False, second.content
    assert _response_text(second) == _response_text(first)
    assert flag_after_first is not None
    assert _user_flag(mcp_env.database_url, caller.user_id, RESET_FLAG) == flag_after_first
    events_after = _audit_event_types(mcp_env.database_url, caller.tenant_id)
    assert events_after == [*events_before, RESET_EVENT, REPLAYED_EVENT]


def test_the_same_key_with_different_arguments_is_a_conflict(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches a fingerprint built from the key alone (a changed request
    would replay the wrong response) or a conflict that still runs the
    effect: one key means one request, so the second title never lands."""
    caller = mcp_env.probe
    key = _key()
    first_title = _title()
    second_title = _title()

    first = call_tool(bearer(token_for(caller)), CREATE_TICKET, _ticket_args(first_title, key=key))
    second = call_tool(
        bearer(token_for(caller)), CREATE_TICKET, _ticket_args(second_title, key=key)
    )

    assert first.is_error is False, first.content
    assert error_code(second) == "IDEMPOTENCY_CONFLICT"
    assert len(_tickets_titled(mcp_env.database_url, first_title)) == 1
    assert _tickets_titled(mcp_env.database_url, second_title) == []
    assert _idempotency_row_count(mcp_env.database_url, key) == 1


@pytest.mark.parametrize("tool", [RESET, UNLOCK, CREATE_TICKET])
def test_a_missing_idempotency_key_is_refused(
    mcp_env: SeededMcp,  # noqa: F811
    tool: str,
) -> None:
    """Catches `idempotency_key` made optional or defaulted in any mutating
    input model: an unguarded retry of a keyless call is exactly the double
    effect the design forbids."""
    title = _title()
    arguments: dict[str, Any] = (
        {"kind": "general", "title": title, "body": "fake body"} if tool == CREATE_TICKET else {}
    )

    result = call_tool(bearer(token_for(mcp_env.probe)), tool, arguments)

    assert error_code(result) == "VALIDATION"
    assert _tickets_titled(mcp_env.database_url, title) == []


def test_a_disabled_mutating_call_burns_no_idempotency_key(
    mcp_env: SeededMcp,  # noqa: F811
    mcp_redis_url: str,  # noqa: F811
) -> None:
    """Catches the idempotency branch placed ahead of the kill switch: a
    stopped call must store nothing, or the caller's retry after the switch
    clears would replay a refusal-era response instead of running."""
    _trip_global_switch(mcp_redis_url, CREATE_TICKET)
    key = _key()
    title = _title()

    result = call_tool(
        bearer(token_for(mcp_env.probe)), CREATE_TICKET, _ticket_args(title, key=key)
    )

    assert error_code(result) == "TOOL_DISABLED"
    assert _idempotency_row_count(mcp_env.database_url, key) == 0
    assert _tickets_titled(mcp_env.database_url, title) == []
