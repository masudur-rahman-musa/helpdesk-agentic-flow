"""`list_kill_switches`, through the transport.

Surface under test (written before it exists):

``mcp_server.tools.killswitch``
    ``ListKillSwitchesInput(BaseModel)`` — NO fields at all and
    ``extra="forbid"``: no tenant, no user, no tool name. The tenant is the
    token's and the tool list is the server's own registry, so
    there is nothing for a caller to choose and nothing to trust.
    ``ListKillSwitchesOutput(BaseModel)`` — one field, ``switches``, a list of
    rows carrying exactly ``tool``, ``protected``, ``stopped``,
    ``global_state``, ``tenant_state`` and ``unreadable``. Each state is
    ``null`` or ``{reason, actor, at}``.
    ``list_kill_switches`` — a registered READ tool (never mutating, no
    idempotency key) whose wire name is ``list_kill_switches`` and whose
    registry ``event_type`` is the generic ``tool.succeeded``: the board
    writes nothing, so there is no domain event to append.

``helpdesk.killswitch``
    ``KillSwitch.list_states(tenant_id, tools)``, the one bulk read behind it
    (its own cases live in ``packages/helpdesk/tests/test_killswitch.py``).

The matrix gives this row ONE cell, ``platform_admin`` at ``Scope.ANY``, the
same single cell as ``set_kill_switch`` and the same audience as the
``/admin/killswitch`` page. There is NO scope helper on this tool, so the
pipeline's matrix stage IS the whole permission check and the six-principal
case below is its kill (the handler never re-runs
``rbac.authorize``, so there is no second copy of that guard to hide behind).

Everything here is a READ: no key is derived from client input, nothing is
written, and the only Redis command the handler issues is one ``MGET``.

Audit deltas are asserted as exact ordered event-type lists against a before
snapshot, never as counts: a duplicated summary, a domain event this tool has
no business writing, or a denial filed as an outage is invisible to a count.

Fixture cost: everything rides the session-scoped
``mcp_fixtures`` seam — one seeded throwaway database, one dedicated Redis
index flushed per test. Nothing here plants a database row, so no seeded
tenant is resolved by name: both tenant handles come from
``mcp_env`` itself. Per-test isolation is the flushed Redis index, and every
case that trips a switch deletes its key in a ``finally`` — a stopped tool
left behind in the shared suite would answer ``TOOL_DISABLED`` in somebody
else's file.

No pytest-asyncio: each case is a sync function driving one ``asyncio.run``.
"""

import asyncio
import json
import uuid
from typing import Any

import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from helpdesk.mcp_server.tools.registry import REGISTRY
from tests.support.mcp import (  # noqa: F401 - fixtures are resolved by name
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

LIST_KILL_SWITCHES = "list_kill_switches"
SET_KILL_SWITCH = "set_kill_switch"

# The audit contract as literals, never imported from the module under test:
# reading these strings off the code would let a rename pass unnoticed.
SUCCEEDED_EVENT = "tool.succeeded"
DENIED_EVENT = "tool.denied"
DISABLED_EVENT = "tool.disabled"

# The key shapes, as literals for the same reason.
KEY_PREFIX_LITERAL = "killswitch"
GLOBAL_SEGMENT_LITERAL = "global"

# The envelope and the row, written out: what the page parses
# and renders. An extra key is as much a defect as a missing one — a row
# that grew a `reason` beside its two states would be a second, ambiguous
# answer to the question `global_state` already answers.
ENVELOPE_KEYS = {"switches"}
ROW_KEYS = {"tool", "protected", "stopped", "global_state", "tenant_state", "unreadable"}
STATE_KEYS = {"reason", "actor", "at"}

# The `list_kill_switches` matrix row: ONE cell, the same audience as the write
# it is the read half of. Checked against `set_kill_switch`'s row in
# `helpdesk/auth/rbac.py` before it was written.
ALLOWED_PRINCIPAL = "platform_admin"
DENIED_PRINCIPALS = ("employee", "manager", "system_owner", "hr", "it_admin", "auditor")

# The tool whose switch these cases flip. A registered READ permitted to every
# principal the matrix has, so a `TOOL_DISABLED` on it can only come from the kill
# switch and never from the matrix.
TARGET = "get_me"

# Port 9 (discard) is closed, so every client fails to connect. Same address
# `test_mcp_readiness.py` uses for the same reason.
CLOSED_REDIS_URL = "redis://127.0.0.1:9/0"

# Obviously fake switch provenance, planted by hand so the stored shape is
# pinned independently of whatever `set_kill_switch` happens to serialise.
PLANTED_ACTOR = "fake-admin@example.invalid"
PLANTED_AT = "2026-09-04T10:00:00+00:00"


def _reason(label: str) -> str:
    return f"fake {label} reason {uuid.uuid4().hex}"


def _state_json(reason: str) -> str:
    return json.dumps({"reason": reason, "actor": PLANTED_ACTOR, "at": PLANTED_AT})


def _structured(result: Any) -> dict[str, Any]:
    assert result.is_error is False, result.content
    assert result.structured_content is not None, "a successful call carries structured content"
    content: dict[str, Any] = result.structured_content
    return content


def _admin_token(env: SeededMcp) -> dict[str, str]:
    """A tenant-A token carrying ONLY the single matrix cell on this row."""
    return bearer(token_for(env.tenant_a, principals=(ALLOWED_PRINCIPAL,)))


def _board(env: SeededMcp) -> list[dict[str, Any]]:
    """The whole board as the one permitted principal reads it."""
    payload = _structured(call_tool(_admin_token(env), LIST_KILL_SWITCHES, {}))
    assert set(payload) == ENVELOPE_KEYS, payload
    rows: list[dict[str, Any]] = payload["switches"]
    return rows


def _row(rows: list[dict[str, Any]], tool: str) -> dict[str, Any]:
    matches = [row for row in rows if row["tool"] == tool]
    assert len(matches) == 1, f"expected exactly one row for {tool}, got {len(matches)}"
    return matches[0]


# --- probes around the tool, never through it ------------------------------------


def _rows(database_url: str, sql: str, params: dict[str, Any]) -> list[Any]:
    async def scenario() -> list[Any]:
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as conn:
                return list(await conn.execute(text(sql), params))
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


def _audit_rows(database_url: str, tenant_id: uuid.UUID) -> list[Any]:
    """Every (event_type, actor_ref, subject_type, subject_id) of one tenant."""
    return _rows(
        database_url,
        "SELECT event_type, actor_ref, subject_type, subject_id FROM audit_events"
        " WHERE tenant_id = :tenant ORDER BY seq",
        {"tenant": tenant_id},
    )


def _set_key(redis_url: str, key: str, value: str) -> None:
    async def scenario() -> None:
        client: Redis = Redis.from_url(redis_url, socket_connect_timeout=2, socket_timeout=2)
        try:
            await client.set(key, value)
        finally:
            await client.aclose()

    asyncio.run(scenario())


def _delete_key(redis_url: str, key: str) -> None:
    """Clean-up around the tool: no case may leave a stopped tool behind."""

    async def scenario() -> None:
        client: Redis = Redis.from_url(redis_url, socket_connect_timeout=2, socket_timeout=2)
        try:
            await client.delete(key)
        finally:
            await client.aclose()

    asyncio.run(scenario())


def _tenant_key(tenant_id: uuid.UUID, tool_name: str) -> str:
    return f"{KEY_PREFIX_LITERAL}:{tenant_id}:{tool_name}"


def _global_key(tool_name: str) -> str:
    return f"{KEY_PREFIX_LITERAL}:{GLOBAL_SEGMENT_LITERAL}:{tool_name}"


# --- the happy path --------------------------------------------


def test_the_board_answers_the_envelope_and_an_exact_six_key_row(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """The literal the page parses and renders.

    KILL: a handler answering a bare list, a mapping of tool to state, or a
    row short of a field — `protected` left for the page to retype,
    `unreadable` omitted because the happy path has no bad key, or the two
    layers collapsed into one `state`. Each of those reads plausibly and none
    of them can drive the toggle the page owes: the envelope key, the row key
    set and the values on a CLEAN namespace are asserted together, so a board
    that reports every tool stopped when nothing is stopped fails here too.
    """
    rows = _board(mcp_env)

    assert rows, "the board is never empty: the registry always holds tools"
    for row in rows:
        assert set(row) == ROW_KEYS, row
        assert row["stopped"] is False, row["tool"]
        assert row["global_state"] is None, row["tool"]
        assert row["tenant_state"] is None, row["tool"]
        assert row["unreadable"] is False, row["tool"]
        assert isinstance(row["protected"], bool), row["tool"]


def test_the_listed_names_are_exactly_the_registry_in_its_own_order(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Where the tool list comes from.

    KILL: a handler built from a Redis `SCAN` over `killswitch:*` or from the
    `rbac.Tool` members instead of the registry's own names. `SCAN` shows only
    the keys that EXIST, so on the clean namespace here it answers an empty
    board — the page could never say "this tool is running". The matrix
    members are the wrong vocabulary: `check` is called with `spec.name`, and
    the matrix splits `create_access_request` into two members with one wire
    name, so a board built from them lists a name no key is ever written
    under. The equality is with `set(REGISTRY)` rather than a literal on
    purpose: a tool registered by a LATER change must not be able to go missing
    from the admin page.

    Each name appears exactly once, and the order is the registry's, so a
    board rendered in a different order on every render (a set, a dict of
    keys read back from Redis) fails as well.
    """
    rows = _board(mcp_env)

    names = [row["tool"] for row in rows]
    assert names == list(REGISTRY)
    assert len(names) == len(set(names)) == len(REGISTRY)


def test_the_tool_takes_no_arguments_at_all(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """The input model carries NO fields.

    KILL: an input that accepts a `tenant_id` (or a `tool`, or a `user_id`) so
    a caller can choose whose board they read. The declared field set is
    asserted EMPTY, which no later field can ride along into, and the wire
    half proves `extra="forbid"` rather than `ignore`: an ignored `tenant_id`
    is a caller who believes they are reading another tenant's switches and
    is being answered with their own.

    The `tenant_id` sent below is the caller's OWN, so the
    refusal cannot be the scope or tenant guard answering on the model's
    behalf — this caller holds the one cell the matrix gives the row, every other
    stage passes, and nothing but the empty model can refuse it.
    """
    spec = REGISTRY[LIST_KILL_SWITCHES]

    assert spec.input_model.model_fields == {}

    result = call_tool(
        _admin_token(mcp_env),
        LIST_KILL_SWITCHES,
        {"tenant_id": str(mcp_env.tenant_a.tenant_id)},
    )

    assert error_code(result) == "VALIDATION"


def test_protected_is_true_for_the_one_tool_no_switch_may_stop(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """The flag that disables the page's toggle.

    KILL: a handler that hardcodes `protected` to `False`, or one that
    retypes the protected list instead of reading `PROTECTED_TOOLS`. Either
    gives the page a toggle that looks live on `set_kill_switch` and answers
    `POLICY_BLOCKED` when an admin uses it — a second copy of a
    security-relevant list, which is what the tenant rule exists to stop. The
    exact True row AND every False row are asserted, so `protected=True`
    everywhere (a toggle nobody can use) fails too.

    `PROTECTED_TOOLS` is compared against its literal contents as well, so a
    tool added to that frozenset by a later change without a thought for this
    page is a red row rather than a silently disabled toggle.
    """
    from helpdesk.shared.services.killswitch import PROTECTED_TOOLS

    assert set(PROTECTED_TOOLS) == {SET_KILL_SWITCH}
    assert LIST_KILL_SWITCHES not in PROTECTED_TOOLS, (
        "only the tool that can UNDO a switch is protected (criterion 11)"
    )

    rows = _board(mcp_env)

    assert _row(rows, SET_KILL_SWITCH)["protected"] is True
    for row in rows:
        if row["tool"] == SET_KILL_SWITCH:
            continue
        assert row["protected"] is False, row["tool"]


# --- the two layers and the precedence they must agree with --------


@pytest.mark.parametrize(
    ("plant_global", "plant_tenant"),
    [
        pytest.param(True, False, id="global-only"),
        pytest.param(False, True, id="tenant-only"),
        pytest.param(True, True, id="both-layers"),
    ],
)
def test_a_stopped_tool_reads_stopped_on_the_layer_it_was_stopped_on(
    mcp_env: SeededMcp,  # noqa: F811
    mcp_redis_url: str,  # noqa: F811
    plant_global: bool,
    plant_tenant: bool,
) -> None:
    """The board and `check` must never part company.

    KILL: `stopped` derived from the tenant layer alone, or from whichever
    key came back non-null last, instead of the global-first precedence
    `check` uses. The `global-only` row is the one that
    catches the tenant-only reading; the `both-layers` row catches an
    effective-state board, where the tenant switch is invisible
    behind the global one and an admin clearing it is never told the tool is
    still held down.

    The agreement is asserted against the pipeline's own `KillSwitch.check`,
    not against a recomputed expectation: a second caller calls the stopped
    tool in the same state and must be refused `TOOL_DISABLED`. A board that
    says `stopped: false` while every call is denied — or `true` while the
    calls go through — fails on one side or the other.

    The two reasons differ, so a row that reads the right VALUE into the
    wrong layer fails as well as one that reads a single key.
    """
    tenant_id = mcp_env.tenant_a.tenant_id
    global_reason = _reason("global stop")
    tenant_reason = _reason("tenant stop")
    keys: list[str] = []

    try:
        if plant_global:
            _set_key(mcp_redis_url, _global_key(TARGET), _state_json(global_reason))
            keys.append(_global_key(TARGET))
        if plant_tenant:
            _set_key(mcp_redis_url, _tenant_key(tenant_id, TARGET), _state_json(tenant_reason))
            keys.append(_tenant_key(tenant_id, TARGET))

        row = _row(_board(mcp_env), TARGET)

        assert row["stopped"] is True
        assert row["unreadable"] is False
        if plant_global:
            assert row["global_state"] is not None
            assert set(row["global_state"]) == STATE_KEYS
            assert row["global_state"]["reason"] == global_reason
            assert row["global_state"]["actor"] == PLANTED_ACTOR
        else:
            assert row["global_state"] is None
        if plant_tenant:
            assert row["tenant_state"] is not None
            assert set(row["tenant_state"]) == STATE_KEYS
            assert row["tenant_state"]["reason"] == tenant_reason
        else:
            assert row["tenant_state"] is None

        refused = call_tool(bearer(token_for(mcp_env.probe)), TARGET, {})

        assert error_code(refused) == "TOOL_DISABLED"
    finally:
        for key in keys:
            _delete_key(mcp_redis_url, key)


def test_another_tenants_switch_is_invisible_to_this_caller(
    mcp_env: SeededMcp,  # noqa: F811
    mcp_redis_url: str,  # noqa: F811
) -> None:
    """`tenant_state` reads the token's tenant and no other.

    KILL: a handler that reads the tenant layer with a glob
    (`killswitch:*:{tool}`) or from a tenant that came in any way other than
    the token. The ONLY switch in the namespace belongs to tenant B, so a
    board for tenant A that shows anything at all is showing a stop this
    admin cannot clear and nobody in their tenant set. Every
    row is asserted clear, not just the target's, so a leak that lands on the
    wrong row fails too.
    """
    foreign_key = _tenant_key(mcp_env.tenant_b.tenant_id, TARGET)

    try:
        _set_key(mcp_redis_url, foreign_key, _state_json(_reason("other tenant")))

        rows = _board(mcp_env)

        assert [row["tool"] for row in rows] == list(REGISTRY)
        for row in rows:
            assert row["stopped"] is False, row["tool"]
            assert row["global_state"] is None, row["tool"]
            assert row["tenant_state"] is None, row["tool"]
            assert row["unreadable"] is False, row["tool"]
    finally:
        _delete_key(mcp_redis_url, foreign_key)


# --- the bad key an admin has to be able to see --------------------


def test_an_unreadable_value_marks_its_row_and_the_board_still_answers(
    mcp_env: SeededMcp,  # noqa: F811
    mcp_redis_url: str,  # noqa: F811
) -> None:
    """One bad key may not blank the board.

    KILL: a handler that lets `_read`'s `ToolDisabledError` out — the way
    `check` answers an unparseable value — so the whole call fails and the
    admin loses the only surface from which that key can be seen and cleared.
    It equally catches the opposite, a parse failure swallowed as "no switch":
    `stopped` and `unreadable` are asserted TOGETHER as True with a null
    state, so a row reading "running" while `check` denies every call to that
    tool fails.

    Every other row is asserted to answer normally in the same call, which is
    the half that separates "the board survived" from "the board answered".
    """
    corrupt_key = _global_key(TARGET)

    try:
        _set_key(mcp_redis_url, corrupt_key, "not json at all")

        rows = _board(mcp_env)

        bad = _row(rows, TARGET)
        assert bad["unreadable"] is True
        assert bad["stopped"] is True
        assert bad["global_state"] is None
        assert bad["tenant_state"] is None
        assert [row["tool"] for row in rows] == list(REGISTRY)
        for row in rows:
            if row["tool"] == TARGET:
                continue
            assert row["unreadable"] is False, row["tool"]
            assert row["stopped"] is False, row["tool"]
    finally:
        _delete_key(mcp_redis_url, corrupt_key)


# --- the six empty matrix cells ------------------------------------


@pytest.mark.parametrize("principal", DENIED_PRINCIPALS)
def test_the_six_denied_principals_read_no_board(
    mcp_env: SeededMcp,  # noqa: F811
    principal: str,
) -> None:
    """The six empty matrix cells on this row, and the record they leave.

    KILL: adding this principal to the `list_kill_switches` cell of the RBAC
    matrix. This handler runs no scope helper and therefore never re-runs
    `rbac.authorize`, so the pipeline's matrix stage is the only
    guard that can refuse here and deleting it hands every one of these six
    the platform's switch board (there is no
    second copy of the guard). `it_admin` and `auditor` are the cells that
    matter most — `it_admin` holds one on `rollback_request` and `auditor`
    one on `search_audit`, so a row copied from either by habit fails.

    The call carries no arguments at all, which are the only valid arguments
    this tool has, so no validation stage can produce this answer.
    `tool.denied` rather than `tool.failed`: a permission decision filed as an
    outage is a false record, and a count-only assertion cannot tell them
    apart.
    """
    tenant_id = mcp_env.tenant_a.tenant_id
    events_before = _audit_event_types(mcp_env.database_url, tenant_id)

    result = call_tool(
        bearer(token_for(mcp_env.tenant_a, principals=(principal,))),
        LIST_KILL_SWITCHES,
        {},
    )

    assert error_code(result) == "FORBIDDEN"
    assert _audit_event_types(mcp_env.database_url, tenant_id) == [
        *events_before,
        DENIED_EVENT,
    ]


def test_a_denied_caller_is_refused_before_any_switch_is_read(
    mcp_env: SeededMcp,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The second half: the matrix decides with no Redis read.

    The handler's own Redis accessor is staged to a port nothing listens on,
    so ANY read it issues raises rather than answering. The pipeline's own
    `KillSwitch.check` is untouched and still reaches the real Redis, which
    is why this case can tell the two apart at all.

    KILL: a handler that reads the board before (or regardless of) the
    permission decision — a "read it once and filter the rows" shape, or a
    refusal raised after the `MGET`. That implementation answers `INTERNAL`
    here, where the matrix stage running first answers `FORBIDDEN`. Both
    codes are asserted, one positively and one negatively, because an
    `INTERNAL` would otherwise read as a plausible refusal.
    """
    from helpdesk.mcp_server.tools.context import ToolClients

    def _closed(self: ToolClients) -> Any:
        return Redis.from_url(CLOSED_REDIS_URL, socket_connect_timeout=1, socket_timeout=1)

    # `raising=True` (the default) is deliberate: the handler reaches Redis
    # through `ctx.clients.redis()`, while the pipeline's own check calls
    # `get_redis` directly. A handler that reached Redis some other way makes
    # this case fail loudly here rather than pass while staging nothing.
    monkeypatch.setattr(ToolClients, "redis", _closed)

    result = call_tool(
        bearer(token_for(mcp_env.tenant_a, principals=("auditor",))),
        LIST_KILL_SWITCHES,
        {},
    )

    assert error_code(result) == "FORBIDDEN"


# --- the board can be stopped like any other tool -----------------


def test_a_switch_on_the_board_itself_answers_tool_disabled(
    mcp_env: SeededMcp,  # noqa: F811
    mcp_redis_url: str,  # noqa: F811
) -> None:
    """The board is not protected, and it is not special.

    The switch is set through `set_kill_switch` rather than planted in Redis,
    which is what makes this two kills in one:

    * adding `list_kill_switches` to `PROTECTED_TOOLS` by habit —
      only the tool that can UNDO a switch is protected — turns the
      first call into `POLICY_BLOCKED` and this case red;
    * a board wired around the pipeline (its own route, its own handler, a
      registry entry the switch stage skips) answers the second call with
      data, where every other registered tool would answer `TOOL_DISABLED`.

    The `tool.disabled` record is asserted as well, so a stop filed as an
    outage — or as a denial — fails: an auditor must be able to tell the
    platform stopping a tool from the platform failing to run it.
    """
    tenant_id = mcp_env.tenant_a.tenant_id
    key = _tenant_key(tenant_id, LIST_KILL_SWITCHES)

    try:
        trip = call_tool(
            _admin_token(mcp_env),
            SET_KILL_SWITCH,
            {
                "tool_name": LIST_KILL_SWITCHES,
                "enabled": True,
                "reason": _reason("board stop"),
                "scope": str(tenant_id),
                "idempotency_key": f"fake-key-{uuid.uuid4().hex}",
            },
        )
        assert trip.is_error is False, trip.content

        events_before = _audit_event_types(mcp_env.database_url, tenant_id)
        stopped = call_tool(_admin_token(mcp_env), LIST_KILL_SWITCHES, {})

        assert error_code(stopped) == "TOOL_DISABLED"
        assert _audit_event_types(mcp_env.database_url, tenant_id) == [
            *events_before,
            DISABLED_EVENT,
        ]
    finally:
        _delete_key(mcp_redis_url, key)


# --- what the record says -------------------------------------


def test_one_call_writes_exactly_one_event_naming_the_tool_and_the_caller(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """What the audit log actually carries for a board read.

    KILL: a read that declares a domain event type of its own, or appends one
    beside the summary. A `killswitch.changed` per render would put a
    governance record in the hash chain for a call that changed nothing, and
    `search_audit` would show the switch board being "changed" every time an
    admin opened the page. The delta is the exact ordered list, so a
    duplicated summary fails too.

    The subject is `list_kill_switches` and the actor is the caller's user id:
    an event whose subject is some other tool would answer the wrong question
    for an auditor asking who read the board and when.
    """
    tenant_id = mcp_env.tenant_a.tenant_id
    events_before = _audit_rows(mcp_env.database_url, tenant_id)

    _board(mcp_env)

    delta = _audit_rows(mcp_env.database_url, tenant_id)[len(events_before) :]

    assert [str(row.event_type) for row in delta] == [SUCCEEDED_EVENT]
    assert str(delta[0].subject_type) == "tool"
    assert str(delta[0].subject_id) == LIST_KILL_SWITCHES
    assert str(delta[0].actor_ref) == str(mcp_env.tenant_a.user_id)
