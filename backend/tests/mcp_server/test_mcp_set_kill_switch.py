"""`set_kill_switch`, through the transport.

Surface under test (written before it exists):

``mcp_server.tools.killswitch``
    ``SetKillSwitchInput(MutatingToolInput)`` — keyed like every other write
    so it carries a
    required ``idempotency_key`` and forbids extras.
    Fields: ``tool_name: str``, the WIRE name of the tool to stop;
    ``enabled: bool``, describing the SWITCH and not the tool, so
    ``enabled=True`` means the target is STOPPED;
    ``reason: str`` bounded ``1..REASON_MAX_LENGTH`` with the constant
    IMPORTED from ``helpdesk.killswitch`` rather than retyped (so the two
    limits cannot drift), required in BOTH directions; and
    ``scope: Literal["global"] | uuid.UUID``, REQUIRED — a UUID
    that is not the token's tenant is refused ``FORBIDDEN``.
    ``SetKillSwitchOutput(BaseModel)`` — ``tool_name``, ``stopped``, ``scope``
    (``"global"`` or the tenant uuid as a string) and ``changed_at``.
    ``set_kill_switch`` — a registered MUTATING tool (it runs through ``run_once``) whose
    wire name is ``set_kill_switch`` and whose registry ``event_type`` is the
    generic ``tool.succeeded``; the domain appends ``killswitch.changed``
    itself, and that event's SUBJECT is the TARGET tool.

``helpdesk.killswitch``
    ``change_switch(...)`` and ``KILLSWITCH_CHANGED_EVENT_TYPE``, exported
    from the package ``__init__`` (the protected-tool guard, the
    event and the Redis write live together, so a later admin page cannot
    reach the write without the guards).

Refusals: ``FORBIDDEN`` (+ ``tool.denied``) for the six principals the permission matrix
gives no cell on this row and for a ``scope`` naming another tenant,
``POLICY_BLOCKED`` for ``tool_name="set_kill_switch"`` (one call
would otherwise lock the platform out of the only tool that can undo it), and
``VALIDATION`` for a name outside the registry, a name over
``TOOL_NAME_MAX_LENGTH`` and a reason outside ``1..REASON_MAX_LENGTH``.

There is NO scope helper on this tool: the permission matrix gives the row `platform_admin`
alone, at `Scope.ANY`, so the pipeline's matrix stage IS the whole permission
check and the six-principal case below is its kill (the
handler never re-runs `rbac.authorize`, so there is no second copy of that
guard to hide behind). The scope check against the token is a SEPARATE guard,
and its own case is called by a `platform_admin`, the one principal the matrix
admits, so the matrix cannot be what refuses it.

Audit deltas are asserted as exact ordered event-type lists against a before
snapshot, never as counts: a missing domain event, a duplicated one, or a
denial filed as an outage is invisible to a count.

Fixture cost: everything rides the session-scoped
``mcp_fixtures`` seam — one seeded throwaway database, one dedicated Redis
index flushed per test. Nothing here plants a database row, so no seeded
tenant is resolved by name: both tenant handles come from
``mcp_env`` itself. Per-test isolation is the flushed Redis index plus a
target tool chosen per case, and every case that actually trips a switch
deletes its key in a ``finally`` — a stopped tool left behind in the shared
suite would answer ``TOOL_DISABLED`` in somebody else's file.

No pytest-asyncio: each case is a sync function driving one ``asyncio.run``.
"""

import asyncio
import uuid
from datetime import datetime
from typing import Any

import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.support.mcp import (
    SeededMcp,
    bearer,
    call_tool,
    error_code,
    token_for,
)

SET_KILL_SWITCH = "set_kill_switch"

# The audit contract as literals, never imported from the module under test:
# reading these strings off the code would let a rename pass unnoticed.
KILLSWITCH_CHANGED_EVENT = "killswitch.changed"
SUCCEEDED_EVENT = "tool.succeeded"
DENIED_EVENT = "tool.denied"
DISABLED_EVENT = "tool.disabled"
FAILED_EVENT = "tool.failed"

# The error contract key shapes, as literals for the same reason.
KEY_PREFIX_LITERAL = "killswitch"
GLOBAL_SEGMENT_LITERAL = "global"

# The bounds the wire model must carry. Written out
# here and compared against `helpdesk.killswitch`'s own constants inside the
# boundary cases, so each number is pinned in the test rather than borrowed.
REASON_MAX_LENGTH = 500
TOOL_NAME_MAX_LENGTH = 64

# The targets. All three are registered READ tools that no other case in this
# file touches at the same time, and only the first two are ever tripped.
TARGET = "get_me"
CLEAR_ONLY_TARGET = "list_my_access"
SUBJECT_TARGET = "list_my_requests"

# The permission matrix `set_kill_switch` row, the NARROWEST in the matrix: ONE cell.
# Checked against `helpdesk/auth/rbac.py::Tool.SET_KILL_SWITCH` before it was
# written — `platform_admin: Scope.ANY` and nothing else.
ALLOWED_PRINCIPAL = "platform_admin"
DENIED_PRINCIPALS = ("employee", "manager", "system_owner", "hr", "it_admin", "auditor")

# Port 9 (discard) is closed, so every client fails to connect. Same address
# `test_mcp_readiness.py` uses for the same reason.
CLOSED_REDIS_URL = "redis://127.0.0.1:9/0"

# The exact payload of a `killswitch.changed` event: what
# an auditor gets is the target tool, where it was stopped, which way, why and
# by whom. An extra key is as much a defect as a missing one — `actor` is
# `str(user_id)` and an email appearing beside it would leak an admin's
# identity into a record every audit reader can see.
CHANGED_PAYLOAD_KEYS = {"tool", "scope", "stopped", "reason", "actor"}


def _reason(label: str) -> str:
    return f"fake {label} reason {uuid.uuid4().hex}"


def _args(
    tool_name: str,
    *,
    enabled: bool,
    scope: str,
    reason: str | None = None,
    key: str | None = None,
) -> dict[str, Any]:
    """Minimally valid arguments, with a fresh idempotency key."""
    return {
        "tool_name": tool_name,
        "enabled": enabled,
        "reason": _reason("switch") if reason is None else reason,
        "scope": scope,
        "idempotency_key": key if key is not None else f"fake-key-{uuid.uuid4().hex}",
    }


def _structured(result: Any) -> dict[str, Any]:
    assert result.is_error is False, result.content
    assert result.structured_content is not None, "a successful call carries structured content"
    content: dict[str, Any] = result.structured_content
    return content


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
    """Every (event_type, subject_type, subject_id, payload) of one tenant."""
    return _rows(
        database_url,
        "SELECT event_type, subject_type, subject_id, payload FROM audit_events"
        " WHERE tenant_id = :tenant ORDER BY seq",
        {"tenant": tenant_id},
    )


def _redis_keys(redis_url: str, pattern: str) -> list[str]:
    async def scenario() -> list[str]:
        client: Redis = Redis.from_url(redis_url, socket_connect_timeout=2, socket_timeout=2)
        try:
            keys: list[Any] = [key async for key in client.scan_iter(match=pattern)]
            return [key.decode() if isinstance(key, bytes) else str(key) for key in keys]
        finally:
            await client.aclose()

    return asyncio.run(scenario())


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


def _admin_token(env: SeededMcp) -> dict[str, str]:
    """A tenant-A token carrying ONLY the permission matrix's single cell on this row."""
    return bearer(token_for(env.tenant_a, principals=(ALLOWED_PRINCIPAL,)))


# --- the happy path ----------------------------------------


def test_a_tripped_tool_answers_tool_disabled_with_the_admins_reason(
    mcp_env: SeededMcp,
    mcp_redis_url: str,
) -> None:
    """To end: the trip, the key, and the stop it produces.

    KILL: a handler that reports `stopped` without writing the key the
    pipeline's `KillSwitch.check` reads — either because it writes nothing, or
    because it writes a key of a different shape, or because the
    polarity is inverted so `enabled=True` clears instead of trips. The second
    caller's `TOOL_DISABLED` is what catches all three: `get_me` is permitted
    to every principal the permission matrix has, so the kill switch is the only stage in the
    pipeline that can refuse them.

    The stop is read by a DIFFERENT caller than the admin who set it, so a
    switch that somehow only applies to its author fails here. The admin's
    reason and `str(user_id)` as the actor are read off the `tool.disabled`
    event's `detail`, which is the only place the underlying message is kept:
    the wire carries the code alone. The admin's EMAIL is asserted
    absent from that detail — `check` copies `actor` into a message every
    caller of the stopped tool sees, so an email there would leak an admin's
    identity to the whole tenant.
    """
    tenant_id = mcp_env.tenant_a.tenant_id
    key = _tenant_key(tenant_id, TARGET)
    reason = _reason("stop")
    assert _redis_keys(mcp_redis_url, f"{KEY_PREFIX_LITERAL}:*") == []

    try:
        payload = _structured(
            call_tool(
                _admin_token(mcp_env),
                SET_KILL_SWITCH,
                _args(TARGET, enabled=True, scope=str(tenant_id), reason=reason),
            )
        )

        assert payload["tool_name"] == TARGET
        assert payload["stopped"] is True
        assert payload["scope"] == str(tenant_id)
        changed_at = datetime.fromisoformat(str(payload["changed_at"]))
        assert changed_at.tzinfo is not None, "an audit timestamp without an offset is ambiguous"
        assert _redis_keys(mcp_redis_url, f"{KEY_PREFIX_LITERAL}:*") == [key]

        stopped = call_tool(bearer(token_for(mcp_env.probe)), TARGET, {})

        assert error_code(stopped) == "TOOL_DISABLED"
        last = _audit_rows(mcp_env.database_url, tenant_id)[-1]
        assert str(last.event_type) == DISABLED_EVENT
        assert str(last.subject_id) == TARGET
        detail = str(dict(last.payload)["detail"])
        assert reason in detail, detail
        assert str(mcp_env.tenant_a.user_id) in detail, detail
        assert mcp_env.tenant_a.email not in detail, "decision I: the actor is an id, not an email"
    finally:
        _delete_key(mcp_redis_url, key)


def test_the_global_scope_stops_the_tool_in_the_other_tenant_too(
    mcp_env: SeededMcp,
    mcp_redis_url: str,
) -> None:
    """The `"global"` scope, planted and read across
    both seeded tenants.

    KILL: a handler that ignores `scope` and keys every write on the token's
    tenant. It would write `killswitch:{tenant_a}:get_me`, tenant B's caller
    would sail through, and both assertions below flip — the key shape one and
    the refusal one. The reader is in the OTHER tenant precisely so no
    tenant-scoped key can produce this answer: `killswitch:global:{tool}` is
    the only key `check` reads before the caller's own.

    The tenant-scoped key is asserted ABSENT as well, so a handler that writes
    BOTH — stopping the tool everywhere but leaving a per-tenant key nothing
    will ever clear — fails rather than passing on the refusal alone.
    """
    key = _global_key(TARGET)

    try:
        payload = _structured(
            call_tool(
                _admin_token(mcp_env),
                SET_KILL_SWITCH,
                _args(TARGET, enabled=True, scope=GLOBAL_SEGMENT_LITERAL),
            )
        )

        assert payload["scope"] == GLOBAL_SEGMENT_LITERAL
        assert payload["stopped"] is True
        assert _redis_keys(mcp_redis_url, f"{KEY_PREFIX_LITERAL}:*") == [key]

        elsewhere = call_tool(bearer(token_for(mcp_env.tenant_b)), TARGET, {})

        assert error_code(elsewhere) == "TOOL_DISABLED"
    finally:
        _delete_key(mcp_redis_url, key)


def test_clearing_a_switch_that_was_never_set_succeeds_and_still_writes_the_event(
    mcp_env: SeededMcp,
    mcp_redis_url: str,
) -> None:
    """The second half: an admin's action is recorded whether or not
    it changed anything.

    KILL: a handler that reads `status` first and returns early — or refuses
    `NOT_FOUND` — when there is no switch to clear. The call would still
    answer plausibly, and only the audit delta catches the missing
    `killswitch.changed`: a governance action that leaves no record is
    indistinguishable from one that never happened, and turning a tool back on
    is as much a governance act as turning it off.

    The target is never tripped by any case in this file, so the no-op is real
    rather than an artefact of ordering, and the delta is the exact ordered
    pair — the domain's event, then the pipeline's summary — so an
    implementation declaring `killswitch.changed` as its registry event type
    (one event, subject `set_kill_switch`) fails here too.
    """
    tenant_id = mcp_env.tenant_a.tenant_id
    key = _tenant_key(tenant_id, CLEAR_ONLY_TARGET)
    assert _redis_keys(mcp_redis_url, key) == []
    events_before = _audit_event_types(mcp_env.database_url, tenant_id)

    payload = _structured(
        call_tool(
            _admin_token(mcp_env),
            SET_KILL_SWITCH,
            _args(CLEAR_ONLY_TARGET, enabled=False, scope=str(tenant_id)),
        )
    )

    assert payload["tool_name"] == CLEAR_ONLY_TARGET
    assert payload["stopped"] is False
    assert payload["scope"] == str(tenant_id)
    assert _redis_keys(mcp_redis_url, key) == []
    assert _audit_event_types(mcp_env.database_url, tenant_id) == [
        *events_before,
        KILLSWITCH_CHANGED_EVENT,
        SUCCEEDED_EVENT,
    ]


# --- what the record says ----------


def test_the_event_names_the_target_tool_as_its_subject(
    mcp_env: SeededMcp,
    mcp_redis_url: str,
) -> None:
    """Writing an event is not writing a record.

    KILL: declaring `killswitch.changed` as the tool's REGISTRY event type
    instead of appending it in the domain. The pipeline writes its event with
    `subject_id = spec.name`, so the log would say `set_kill_switch` was the
    subject — and an auditor asking "which tool was stopped, and why" would
    read the name of the stopper on every row. Only the subject assertion
    below separates the two wirings: the event type, the tenant and the actor
    are identical under both.

    The payload key SET is asserted exactly, not just its contents: a missing
    `reason` makes the record unanswerable and an extra key is how an email or
    a token claim leaks into a record every audit reader can see. The
    `killswitch.changed` literal is compared against the package's own export,
    so a rename that misses one side fails rather than drifting.
    """
    from helpdesk.shared.services.killswitch import KILLSWITCH_CHANGED_EVENT_TYPE

    assert KILLSWITCH_CHANGED_EVENT_TYPE == KILLSWITCH_CHANGED_EVENT

    tenant_id = mcp_env.tenant_a.tenant_id
    key = _tenant_key(tenant_id, SUBJECT_TARGET)
    reason = _reason("subject")
    events_before = _audit_rows(mcp_env.database_url, tenant_id)

    try:
        call_tool(
            _admin_token(mcp_env),
            SET_KILL_SWITCH,
            _args(SUBJECT_TARGET, enabled=True, scope=str(tenant_id), reason=reason),
        )

        delta = _audit_rows(mcp_env.database_url, tenant_id)[len(events_before) :]
        assert [str(row.event_type) for row in delta] == [
            KILLSWITCH_CHANGED_EVENT,
            SUCCEEDED_EVENT,
        ]
        changed = delta[0]
        assert str(changed.subject_type) == "tool"
        assert str(changed.subject_id) == SUBJECT_TARGET
        payload = dict(changed.payload)
        assert set(payload) == CHANGED_PAYLOAD_KEYS
        assert payload["tool"] == SUBJECT_TARGET
        assert payload["scope"] == str(tenant_id)
        assert payload["stopped"] is True
        assert payload["reason"] == reason
        assert payload["actor"] == str(mcp_env.tenant_a.user_id)
        assert str(delta[1].subject_id) == SET_KILL_SWITCH, (
            "the pipeline's summary names the tool that was CALLED, which is why the "
            "domain event has to name the tool that was STOPPED"
        )
    finally:
        _delete_key(mcp_redis_url, key)


# --- The permission matrix six empty cells -------------------------------------


@pytest.mark.parametrize("principal", DENIED_PRINCIPALS)
def test_the_six_denied_principals_change_no_switch(
    mcp_env: SeededMcp,
    mcp_redis_url: str,
    principal: str,
) -> None:
    """The permission matrix six empty cells on this row, and the record they leave.

    KILL: adding this principal to the `set_kill_switch` cell of the RBAC
    matrix. This handler runs no scope helper and therefore never re-runs
    `rbac.authorize`, so the pipeline's matrix stage is the only
    guard that can refuse here and deleting it hands every one of these six
    the platform's emergency stop (there is
    no second copy of the guard). `it_admin` is included and matters most — it
    holds a cell on `rollback_request` and none here, so a row copied from
    that tool by habit fails.

    Every argument is valid, the target is a registered tool, the reason is
    inside the bound and the scope is the caller's OWN tenant, so no other
    guard can produce this answer. The Redis key is asserted absent
    afterwards, because a refusal that still wrote the key would leave the
    tenant's tool stopped by somebody the matrix denied. `tool.denied` rather
    than `tool.failed`: a permission decision filed as an outage is a false
    record, and a count-only assertion cannot tell them apart.
    """
    tenant_id = mcp_env.tenant_a.tenant_id
    events_before = _audit_event_types(mcp_env.database_url, tenant_id)

    result = call_tool(
        bearer(token_for(mcp_env.tenant_a, principals=(principal,))),
        SET_KILL_SWITCH,
        _args(TARGET, enabled=True, scope=str(tenant_id)),
    )

    assert error_code(result) == "FORBIDDEN"
    assert _redis_keys(mcp_redis_url, f"{KEY_PREFIX_LITERAL}:*") == []
    assert _audit_event_types(mcp_env.database_url, tenant_id) == [
        *events_before,
        DENIED_EVENT,
    ]


# --- the two refusals a permitted admin can still earn -------------


def test_another_tenants_scope_is_forbidden(
    mcp_env: SeededMcp,
    mcp_redis_url: str,
) -> None:
    """A UUID scope that is not the token's tenant.

        KILL: deleting the comparison of `scope` against the token's tenant, so
        the handler hands the client's uuid straight to `key_for`. The caller
        holds `platform_admin`, the ONE principal the permission matrix gives this row, so the
        matrix cannot be what refuses this call and the scope check is the only
        guard left standing between the argument and tenant B's Redis key
    . The key is asserted absent in tenant B's namespace as well
        as globally: a handler that wrote it and then errored would still have
        stopped a tool for a tenant this token has no business reaching.

        Recorded `tool.denied`, not `tool.failed`: it is a permission decision
        even though it is raised inside the tool body.
    """
    tenant_id = mcp_env.tenant_a.tenant_id
    foreign = mcp_env.tenant_b.tenant_id
    events_before = _audit_event_types(mcp_env.database_url, tenant_id)

    result = call_tool(
        _admin_token(mcp_env),
        SET_KILL_SWITCH,
        _args(TARGET, enabled=True, scope=str(foreign)),
    )

    assert error_code(result) == "FORBIDDEN"
    assert _redis_keys(mcp_redis_url, _tenant_key(foreign, TARGET)) == []
    assert _redis_keys(mcp_redis_url, f"{KEY_PREFIX_LITERAL}:*") == []
    assert _audit_event_types(mcp_env.database_url, tenant_id) == [
        *events_before,
        DENIED_EVENT,
    ]


def test_setting_the_kill_switch_on_set_kill_switch_is_policy_blocked(
    mcp_env: SeededMcp,
    mcp_redis_url: str,
) -> None:
    """The stopper cannot stop itself.

    KILL: dropping the protected-tool guard in `change.py`. Nothing else in
    the pipeline refuses this call — the name IS registered, the caller holds
    the row's one cell, the scope is their own tenant and the reason is inside
    the bound — so without the guard the write lands and the platform is
    locked out of the only tool that can undo it, recoverable only by a manual
    `DEL` against production Redis.

    `POLICY_BLOCKED` and not `VALIDATION`: the argument is well formed and the
    refusal is a policy decision about what it names.
    """
    tenant_id = mcp_env.tenant_a.tenant_id
    events_before = _audit_event_types(mcp_env.database_url, tenant_id)

    result = call_tool(
        _admin_token(mcp_env),
        SET_KILL_SWITCH,
        _args(SET_KILL_SWITCH, enabled=True, scope=str(tenant_id)),
    )

    assert error_code(result) == "POLICY_BLOCKED"
    assert _redis_keys(mcp_redis_url, f"{KEY_PREFIX_LITERAL}:*:{SET_KILL_SWITCH}") == []
    assert _redis_keys(mcp_redis_url, f"{KEY_PREFIX_LITERAL}:*") == []
    assert _audit_event_types(mcp_env.database_url, tenant_id) == [
        *events_before,
        FAILED_EVENT,
    ]


# --- the pre-checks -------------------------------------------


def test_an_unregistered_tool_name_is_refused_before_any_key_is_written(
    mcp_env: SeededMcp,
    mcp_redis_url: str,
) -> None:
    """The first half: a name no `check` will ever ask about.

    KILL: dropping the registry lookup from the handler. The name below is a
    LEGAL kill-switch key target — lower case, underscores only, well inside
    `TOOL_NAME_MAX_LENGTH` — so `key_for`'s own shape and length gates accept
    it and the registry check is the only thing that can refuse it. Without
    that check the write lands under a key nothing reads, and the admin
    surface would show a tool stopped while every call to it went through.

    `VALIDATION`, not `INTERNAL`: the caller supplied a bad argument, unlike
    `registry.lookup`'s own refusal of an unregistered WIRE name, which means
    the caller's client is wrong about what exists.
    """
    from helpdesk.shared.services.killswitch import KEY_PREFIX

    assert KEY_PREFIX == KEY_PREFIX_LITERAL

    tenant_id = mcp_env.tenant_a.tenant_id
    unregistered = f"not_a_registered_tool_{uuid.uuid4().hex[:8]}"
    assert len(unregistered) <= TOOL_NAME_MAX_LENGTH
    events_before = _audit_event_types(mcp_env.database_url, tenant_id)

    result = call_tool(
        _admin_token(mcp_env),
        SET_KILL_SWITCH,
        _args(unregistered, enabled=True, scope=str(tenant_id)),
    )

    assert error_code(result) == "VALIDATION"
    assert _redis_keys(mcp_redis_url, f"{KEY_PREFIX_LITERAL}:*:{unregistered}") == []
    assert _redis_keys(mcp_redis_url, f"*{unregistered}*") == []
    assert _audit_event_types(mcp_env.database_url, tenant_id) == [
        *events_before,
        FAILED_EVENT,
    ]


@pytest.mark.parametrize(
    ("length", "accepted"),
    [
        pytest.param(1, True, id="reason-1-ok"),
        pytest.param(REASON_MAX_LENGTH, True, id="reason-500-ok"),
        pytest.param(0, False, id="reason-empty-refused"),
        pytest.param(REASON_MAX_LENGTH + 1, False, id="reason-501-refused"),
    ],
)
def test_the_reason_boundary_is_inclusive_at_1_and_500(
    mcp_env: SeededMcp,
    mcp_redis_url: str,
    length: int,
    accepted: bool,
) -> None:
    """The reason half: both ends, both directions, exact number.

    KILL: `Field(min_length=1)` with no `max_length` on `SetKillSwitchInput`,
    or a `gt`/`lt` where the limit is inclusive. The reason lands in the
    `SwitchState` that `check` reads on EVERY call of the stopped tool and in
    the unbounded `Text` payload of `killswitch.changed`, so this Pydantic
    bound is the only backstop there is, and a one-sided
    assertion cannot tell an inclusive bound from an exclusive one. The lower
    bound is 1 and not 0 because a platform stop nobody can explain is one
    nobody can review.

    The bound is read off `helpdesk.killswitch.REASON_MAX_LENGTH`, never
    retyped in the admin module: the store's own
    cap and the wire's cap drifting apart would let a reason the wire accepts
    be refused by `SwitchState` at the moment of the write. Because of that,
    500 is asserted explicitly, once, so a widened constant is a decision
    somebody has to make rather than a number that quietly moves.

    Every row CLEARS a switch that is never tripped, so an accepted reason
    leaves nothing behind; the refused rows are checked to have written no
    event at all, which proves the check runs before any effect.
    """
    from helpdesk.mcp_server.tools import killswitch
    from helpdesk.shared.services.killswitch import REASON_MAX_LENGTH as WIRED_REASON_MAX

    assert WIRED_REASON_MAX == REASON_MAX_LENGTH == 500
    assert killswitch.SetKillSwitchInput.model_fields["reason"].metadata[-1].max_length == (
        WIRED_REASON_MAX
    )

    tenant_id = mcp_env.tenant_a.tenant_id
    reason = "r" * length
    events_before = _audit_event_types(mcp_env.database_url, tenant_id)

    result = call_tool(
        _admin_token(mcp_env),
        SET_KILL_SWITCH,
        _args(TARGET, enabled=False, scope=str(tenant_id), reason=reason),
    )

    if accepted:
        assert result.is_error is False, result.content
        assert _audit_event_types(mcp_env.database_url, tenant_id) == [
            *events_before,
            KILLSWITCH_CHANGED_EVENT,
            SUCCEEDED_EVENT,
        ]
        changed = _audit_rows(mcp_env.database_url, tenant_id)[-2]
        assert dict(changed.payload)["reason"] == reason
    else:
        assert error_code(result) == "VALIDATION"
        assert _audit_event_types(mcp_env.database_url, tenant_id) == [
            *events_before,
            FAILED_EVENT,
        ]
    assert _redis_keys(mcp_redis_url, f"{KEY_PREFIX_LITERAL}:*") == []


def test_the_tool_name_length_boundary_is_inclusive_at_64(
    mcp_env: SeededMcp,
    mcp_redis_url: str,
) -> None:
    """The second half, and the honest limit of it.

        The two rows are refused by DIFFERENT checks, which is the point:
        * the 64-character name is a LEGAL key target and is refused by the
          REGISTRY lookup, because no tool is registered under it;
        * the 65-character name is refused by the LENGTH check
          (`TOOL_NAME_MAX_LENGTH` on the wire model, and `key_for`'s own gate
          behind it) before the registry is ever consulted.

        KILL: deleting the length bound. The 65-character row would then reach the
        registry, which refuses it too — with the SAME `VALIDATION` code — so what
        the 65 row actually kills is narrower than it looks: it pins that an
        over-long name never reaches Redis and never reads `INTERNAL`. NO KILL
        CLAIM for the inclusivity of 64 itself: no registered tool name is 64
        characters long, so the accepted side of that boundary is unreachable over
        the wire, and both rows here read `VALIDATION` whether the bound is `<= 64`
        or `< 64`. The value is pinned instead by asserting the constant exactly,
    and by `packages/helpdesk`'s own switch tests, which
        reach `key_for` directly.

        Neither name may leave a key behind: Redis imposes no length limit at all,
        so an unchecked name writes a key nothing will ever read or clear.
    """
    from helpdesk.shared.services.killswitch import TOOL_NAME_MAX_LENGTH as WIRED_TOOL_NAME_MAX

    assert WIRED_TOOL_NAME_MAX == TOOL_NAME_MAX_LENGTH == 64

    tenant_id = mcp_env.tenant_a.tenant_id
    at_limit = "t" * TOOL_NAME_MAX_LENGTH
    over_limit = "t" * (TOOL_NAME_MAX_LENGTH + 1)
    token = _admin_token(mcp_env)

    at_limit_result = call_tool(
        token, SET_KILL_SWITCH, _args(at_limit, enabled=True, scope=str(tenant_id))
    )
    over_limit_result = call_tool(
        token, SET_KILL_SWITCH, _args(over_limit, enabled=True, scope=str(tenant_id))
    )

    assert error_code(at_limit_result) == "VALIDATION", "64 characters: refused by the registry"
    assert error_code(over_limit_result) == "VALIDATION", "65 characters: refused by the length"
    assert _redis_keys(mcp_redis_url, f"{KEY_PREFIX_LITERAL}:*") == []
    assert _redis_keys(mcp_redis_url, f"*{at_limit}*") == []
    assert _redis_keys(mcp_redis_url, f"*{over_limit}*") == []


# --- the outage ----------------------------------------------------


def test_an_unreachable_redis_reads_internal_and_writes_no_change_event(
    mcp_env: SeededMcp,
    mcp_redis_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The switch state is unknown, so nothing is claimed.

    NO KILL CLAIM for the append-before-write ordering. The
    pipeline's own `KillSwitch.check` stage reaches the SAME unreachable Redis
    before the handler is ever entered, so this call is refused before the
    append could run either way: the case passes with the event appended after
    the Redis write just as it does with the specified ordering.
    The window is pinned where it can be — around
    `change_switch` itself, in `packages/helpdesk`'s own tests, with a client
    that fails only on the write.

    What this case DOES pin is the outcome an operator sees: `INTERNAL` and
    not `TOOL_DISABLED` (the tool is not known to be off, the platform is not
    able to say), a `tool.failed` record and not a `killswitch.changed` one,
    so the audit log never carries a change that did not happen. The audit
    write itself goes to Postgres, which is still up, so the absence below is
    a real absence and not a second outage.
    """
    tenant_id = mcp_env.tenant_a.tenant_id
    events_before = _audit_event_types(mcp_env.database_url, tenant_id)

    monkeypatch.setenv("REDIS_URL", CLOSED_REDIS_URL)
    try:
        result = call_tool(
            _admin_token(mcp_env),
            SET_KILL_SWITCH,
            _args(TARGET, enabled=True, scope=str(tenant_id)),
        )
    finally:
        monkeypatch.setenv("REDIS_URL", mcp_redis_url)

    assert error_code(result) == "INTERNAL"
    delta = _audit_event_types(mcp_env.database_url, tenant_id)[len(events_before) :]
    assert delta == [FAILED_EVENT]
    assert KILLSWITCH_CHANGED_EVENT not in delta
    assert _redis_keys(mcp_redis_url, f"{KEY_PREFIX_LITERAL}:*") == []


# --- the key -------------------------------------------------

REPLAYED_EVENT = "tool.replayed"


def test_a_replayed_set_kill_switch_returns_the_stored_response(
    mcp_env: SeededMcp,
    mcp_redis_url: str,
) -> None:
    """Catches a `set_kill_switch` still registered as non-mutating, so
    dispatch never reaches `run_once`: the second call with the same key would
    run the toggle again (a second `killswitch.changed`, a fresh `changed_at`)
    instead of returning the stored response and writing `tool.replayed`.

    The two answers are compared whole, including `changed_at`, so a response
    rebuilt on the second call (a new clock read) differs from the stored one.
    """
    tenant_id = mcp_env.tenant_a.tenant_id
    key = _tenant_key(tenant_id, SUBJECT_TARGET)
    arguments = _args(SUBJECT_TARGET, enabled=True, scope=str(tenant_id), key="fake-key-replay-73")
    events_before = _audit_event_types(mcp_env.database_url, tenant_id)

    try:
        first = call_tool(_admin_token(mcp_env), SET_KILL_SWITCH, arguments)
        second = call_tool(_admin_token(mcp_env), SET_KILL_SWITCH, arguments)

        assert _structured(second) == _structured(first)
        delta = _audit_event_types(mcp_env.database_url, tenant_id)[len(events_before) :]
        assert delta == [KILLSWITCH_CHANGED_EVENT, SUCCEEDED_EVENT, REPLAYED_EVENT]
        assert delta.count(KILLSWITCH_CHANGED_EVENT) == 1, "one switch change only"
        assert _redis_keys(mcp_redis_url, f"{KEY_PREFIX_LITERAL}:*") == [key]
    finally:
        _delete_key(mcp_redis_url, key)


def test_set_kill_switch_with_no_idempotency_key_is_validation_with_no_write(
    mcp_env: SeededMcp,
    mcp_redis_url: str,
) -> None:
    """Catches an `idempotency_key` that is optional or defaulted on
    `SetKillSwitchInput`: a keyless call would still trip the switch, so a
    retried toggle would be unguarded exactly as it was before the key became required.

    Every other argument is valid and the caller holds the row's one cell, so
    the missing key is the only thing that can produce `VALIDATION` here.
    """
    tenant_id = mcp_env.tenant_a.tenant_id
    arguments = _args(TARGET, enabled=True, scope=str(tenant_id))
    del arguments["idempotency_key"]

    result = call_tool(_admin_token(mcp_env), SET_KILL_SWITCH, arguments)

    assert error_code(result) == "VALIDATION"
    assert _redis_keys(mcp_redis_url, f"{KEY_PREFIX_LITERAL}:*") == []
