"""An earlier change, section 1 criterion 8, section 6: the registry's own bounds.

Surface under test (written before it exists):

``mcp_server.registry``
    ``ToolSpec[I: BaseModel, O: BaseModel]`` — frozen dataclass carrying
    ``name`` (the wire name), ``tool: rbac.Tool``, ``description``,
    ``input_model``, ``output_model``, ``event_type``, ``mutating`` and
    ``handler``.
    ``REGISTRY: Mapping[str, ToolSpec[Any, Any]]`` keyed by wire name.
    ``lookup(name) -> ToolSpec[Any, Any]``, raising ``rbac.UnknownToolError``
    (code ``INTERNAL``) for a name that is not registered.
    ``DENIED_EVENT_TYPE``, ``DISABLED_EVENT_TYPE``, ``FAILED_EVENT_TYPE`` —
    the event types the pipeline writes for the non-success cases.

a project rule: a client-supplied string that lands in a store gets a typed
pre-check against the store's own constant, and a test asserts the INCLUSIVE
boundary. Two stores here:

* Redis, which has no length limit at all, so ``TOOL_NAME_MAX_LENGTH`` in
  ``helpdesk/killswitch/switch.py`` is the only backstop. A registered name
  it would refuse is a tool nobody can stop.
* ``audit_events.event_type``, a sized column. A declared event type it would
  truncate is a call nobody can log. The width is read off the column, never
  retyped as a literal, so widening one without the other fails here.

No pytest-asyncio: each case is a sync function driving one ``asyncio.run``.
"""

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from helpdesk.audit.writer import AuditEventInput, append_event
from helpdesk.auth import rbac
from helpdesk.core.db import create_engine, create_session_factory, tenant_session
from helpdesk.errors import ErrorCode
from helpdesk.killswitch import (
    GLOBAL,
    GLOBAL_SEGMENT,
    KEY_PREFIX,
    TOOL_NAME_MAX_LENGTH,
    TOOL_NAME_PATTERN,
    InvalidSwitchTargetError,
    key_for,
)
from helpdesk.models import AuditEvent
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
from mcp_server import registry
from mcp_server.registry import REGISTRY, lookup
from mcp_server.tools import approvals
from mcp_server.tools.base import MutatingToolInput
from redis.asyncio import Redis
from sqlalchemy import String
from sqlalchemy.exc import DBAPIError

# Read off the column, not retyped: the check and the storage must be the same
# number by construction.
_EVENT_TYPE_TYPE = AuditEvent.__table__.c.event_type.type
assert isinstance(_EVENT_TYPE_TYPE, String), _EVENT_TYPE_TYPE
assert _EVENT_TYPE_TYPE.length is not None, "audit_events.event_type must be a sized column"
AUDIT_EVENT_TYPE_LENGTH: int = _EVENT_TYPE_TYPE.length

# Every event type the pipeline can write, success, replay and non-success
# alike.
ALL_EVENT_TYPES: tuple[str, ...] = (
    *sorted({spec.event_type for spec in REGISTRY.values()}),
    registry.DENIED_EVENT_TYPE,
    registry.DISABLED_EVENT_TYPE,
    registry.FAILED_EVENT_TYPE,
    registry.REPLAYED_EVENT_TYPE,
)

UNREGISTERED_NAME = "no_such_tool_registered_here"


def test_the_audit_event_type_column_is_exactly_64_characters_wide() -> None:
    """Pinned exactly: the boundary pair below is only
    meaningful if the width it is derived from is itself pinned."""
    assert AUDIT_EVENT_TYPE_LENGTH == 64
    assert TOOL_NAME_MAX_LENGTH == 64


# --- every registered name is a valid kill-switch target ----------------------


@pytest.mark.parametrize("name", sorted(REGISTRY))
def test_every_registered_name_is_a_valid_killswitch_target(name: str) -> None:
    """A name `key_for` refuses is a tool an operator cannot stop."""
    assert len(name) <= TOOL_NAME_MAX_LENGTH
    assert TOOL_NAME_PATTERN.fullmatch(name) is not None
    assert key_for(GLOBAL, name) == f"{KEY_PREFIX}:{GLOBAL_SEGMENT}:{name}"


def test_the_tool_name_length_boundary_is_inclusive_at_64() -> None:
    """The pass/fail pair: 64 is accepted, 65 is refused. A one-sided
    assertion cannot tell an inclusive bound from an exclusive one."""
    at_limit = "a" * TOOL_NAME_MAX_LENGTH
    over_limit = "a" * (TOOL_NAME_MAX_LENGTH + 1)
    assert len(at_limit) == 64
    assert len(over_limit) == 65

    assert key_for(GLOBAL, at_limit) == f"{KEY_PREFIX}:{GLOBAL_SEGMENT}:{at_limit}"
    with pytest.raises(InvalidSwitchTargetError):
        key_for(GLOBAL, over_limit)


# --- every declared event type fits the column it is stored in ----------------


@pytest.mark.parametrize("event_type", ALL_EVENT_TYPES)
def test_every_declared_event_type_fits_the_audit_column(event_type: str) -> None:
    assert len(event_type) <= AUDIT_EVENT_TYPE_LENGTH, event_type


def test_the_registry_declares_the_non_success_event_types_exactly() -> None:
    assert registry.DENIED_EVENT_TYPE == "tool.denied"
    assert registry.DISABLED_EVENT_TYPE == "tool.disabled"
    assert registry.FAILED_EVENT_TYPE == "tool.failed"
    assert registry.REPLAYED_EVENT_TYPE == "tool.replayed"
    # The declared success events: the reads share `tool.succeeded` except
    # classify, which names its own; each mutating tool
    # names its action.
    assert {spec.event_type for spec in REGISTRY.values()} == {
        "tool.succeeded",
        "user.password_reset",
        "user.account_unlocked",
        "ticket.created",
        "request.classified",
        "request.created",
    }


# --- the three approval tools ---------------------------------------


APPROVAL_TOOL_EXPECTATIONS: tuple[tuple[str, rbac.Tool, str, bool], ...] = (
    (
        "list_pending_approvals",
        rbac.Tool.LIST_PENDING_APPROVALS,
        registry.SUCCESS_EVENT_TYPE,
        False,
    ),
    # Both deciding tools declare the GENERIC success event and append their
    # the design decision event themselves (decision I): the pipeline's event
    # carries a fixed payload and the tool name as its subject, which would
    # record that somebody called a tool and not which request they decided.
    ("approve_request", rbac.Tool.APPROVE_REQUEST, registry.SUCCESS_EVENT_TYPE, True),
    ("reject_request", rbac.Tool.REJECT_REQUEST, registry.SUCCESS_EVENT_TYPE, True),
)


@pytest.mark.parametrize(
    ("name", "tool", "event_type", "mutating"),
    APPROVAL_TOOL_EXPECTATIONS,
    ids=[name for name, _, _, _ in APPROVAL_TOOL_EXPECTATIONS],
)
def test_each_approval_tool_is_registered_once_under_its_matrix_name(
    name: str, tool: rbac.Tool, event_type: str, mutating: bool
) -> None:
    """Catches a tool registered under a wire name the matrix does not know
    (it would need an undeclared alias, and RBAC would answer INTERNAL rather
    than deciding), pointed at the wrong matrix member — `approve_request`
    declared as `reject_request` would let a rejecter approve — or declaring
    the wrong audit event, which files an approval as a rejection in the only
    record the design keeps of the decision.

    The names are written out literally rather than derived from the registry:
    reading the value under test back out of the code under test would assert
    nothing.
    """
    spec = REGISTRY[name]

    assert spec.name == name == tool.value
    assert spec.tool is tool
    assert spec.event_type == event_type
    assert spec.mutating is mutating
    assert name not in registry.WIRE_NAME_ALIASES


def test_the_two_decision_event_types_are_exactly_the_spec_strings() -> None:
    """Pins both values exactly. the design names
    `request.approved` and `request.rejected`, and the handlers append them
    under exactly those names; a typo would produce an event type nothing in
    the audit UI or the golden set knows, with every other test still green
    because they read the same constant."""
    assert approvals.REQUEST_APPROVED_EVENT_TYPE == "request.approved"
    assert approvals.REQUEST_REJECTED_EVENT_TYPE == "request.rejected"


def test_the_queue_bounds_are_exactly_one_fifty_and_two_hundred() -> None:
    """Decision H, pinned exactly: an unbounded list
    is an unbounded response over the wire, and a default that drifts changes
    what every caller who omits `limit` receives. A docstring is not a
    record."""
    assert approvals.LIMIT_MIN == 1
    assert approvals.LIMIT_DEFAULT == 50
    assert approvals.LIMIT_MAX == 200
    assert approvals.COMMENT_MAX_LENGTH == 2000
    assert approvals.REASON_MAX_LENGTH == 2000


def _append(database_url: str, tenant_id: uuid.UUID, event_type: str) -> None:
    async def scenario() -> None:
        engine = create_engine(database_url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, tenant_id) as session:
                await append_event(
                    session,
                    tenant_id,
                    AuditEventInput(
                        occurred_at=datetime.now(UTC),
                        actor_kind="system",
                        actor_ref=None,
                        event_type=event_type,
                        subject_type="tool",
                        subject_id=None,
                    ),
                )
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_the_event_type_column_boundary_is_inclusive_at_64(
    seeded_mcp_db: SeededMcp,  # noqa: F811
) -> None:
    """Asserted against the store itself, not against a constant restated in
    the test: a 64-character event type is stored, a 65-character one is
    refused by Postgres. That is what makes the length check above a
    guarantee rather than a hope."""
    tenant = seeded_mcp_db.tenant_b.tenant_id
    at_limit = "e" * AUDIT_EVENT_TYPE_LENGTH
    over_limit = "e" * (AUDIT_EVENT_TYPE_LENGTH + 1)
    assert len(at_limit) == 64
    assert len(over_limit) == 65

    _append(seeded_mcp_db.database_url, tenant, at_limit)
    with pytest.raises(DBAPIError):
        _append(seeded_mcp_db.database_url, tenant, over_limit)


# --- the specs themselves ------------------------------------------------------


# The tools that mutate, spelled out: a new tool registered with the wrong
# flag either skips the idempotency guard (a mutation retried twice) or
# demands a key from a read, and this set is what catches it.
MUTATING_TOOL_NAMES = frozenset(
    {
        "reset_own_password",
        "unlock_own_account",
        "create_ticket",
        "create_access_request",
        # An earlier change: both record a decision and one of them executes a grant,
        # so both ride the idempotency guard. `list_pending_approvals` is a
        # read and must stay off this list.
        "approve_request",
        "reject_request",
    }
)


def test_the_wire_name_aliases_are_exactly_the_declared_pair() -> None:
    """An earlier change decision J. The alias table is the ONLY licence a wire name
    has to differ from its matrix member's value, so an undeclared alias
    added later must fail here rather than quietly slipping past the pairing
    check below. Written out literally: importing the table and comparing it
    to itself would assert nothing."""
    assert dict(registry.WIRE_NAME_ALIASES) == {
        "create_access_request": rbac.Tool.CREATE_ACCESS_REQUEST_SELF
    }


@pytest.mark.parametrize("name", sorted(REGISTRY))
def test_each_spec_names_a_real_matrix_tool_and_agrees_with_its_key(name: str) -> None:
    spec = REGISTRY[name]
    assert isinstance(spec.tool, rbac.Tool)
    assert spec.tool in set(rbac.Tool)
    if spec.name in registry.WIRE_NAME_ALIASES:
        # the design gives `create_access_request` two matrix rows for one wire
        # name, so identity is impossible there; the declared pairing is what
        # replaces it (decision J).
        assert registry.WIRE_NAME_ALIASES[spec.name] is spec.tool
    else:
        assert spec.name == spec.tool.value
    assert spec.name == name
    assert spec.mutating is (name in MUTATING_TOOL_NAMES)


@pytest.mark.parametrize("name", sorted(REGISTRY))
def test_a_mutating_spec_takes_a_keyed_input_and_a_read_spec_does_not(name: str) -> None:
    """The flag and the input model must agree: a mutating tool whose input
    forgets `MutatingToolInput` would answer INTERNAL on every call (the
    pipeline's wiring-bug guard), caught here instead of at that tool's first
    happy-path test. A read tool carrying a key would demand one for a call
    the guard never protects."""
    spec = REGISTRY[name]
    assert issubclass(spec.input_model, MutatingToolInput) is spec.mutating


@pytest.mark.parametrize("name", sorted(REGISTRY))
def test_lookup_returns_the_registered_spec(name: str) -> None:
    assert lookup(name) is REGISTRY[name]


def test_lookup_refuses_an_unregistered_name_with_internal() -> None:
    """`INTERNAL`, not `FORBIDDEN`: a name outside the registry means the
    caller is wrong, not the user, and reading it as a permission decision
    would hide a wiring bug behind a legitimate-looking denial."""
    with pytest.raises(rbac.UnknownToolError) as refusal:
        lookup(UNREGISTERED_NAME)

    assert refusal.value.code == ErrorCode.INTERNAL


def _redis_keys(redis_url: str, pattern: str) -> list[str]:
    async def scenario() -> list[str]:
        client: Redis = Redis.from_url(redis_url, socket_connect_timeout=2, socket_timeout=2)
        try:
            keys: list[Any] = [key async for key in client.scan_iter(match=pattern)]
            return [key.decode() if isinstance(key, bytes) else str(key) for key in keys]
        finally:
            await client.aclose()

    return asyncio.run(scenario())


def test_an_unregistered_wire_name_touches_no_redis_key(
    mcp_env: SeededMcp,  # noqa: F811
    mcp_redis_url: str,  # noqa: F811
) -> None:
    """Criterion 8: the wire name is validated before it can become a Redis
    key. Redis has no length or shape limit, so an unchecked name would write
    a key nothing reads."""
    assert _redis_keys(mcp_redis_url, f"{KEY_PREFIX}:*") == []

    result = call_tool(bearer(token_for(mcp_env.tenant_a)), UNREGISTERED_NAME, {})

    assert error_code(result) == "INTERNAL"
    assert _redis_keys(mcp_redis_url, f"{KEY_PREFIX}:*") == []
    assert _redis_keys(mcp_redis_url, f"*{UNREGISTERED_NAME}*") == []
