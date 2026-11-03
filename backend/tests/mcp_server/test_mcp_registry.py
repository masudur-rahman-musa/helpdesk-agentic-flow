"""The registry's own bounds.

Surface under test (written before it exists):

``mcp_server.tools.registry``
    ``ToolSpec[I: BaseModel, O: BaseModel]`` — frozen dataclass carrying
    ``name`` (the wire name), ``tool: rbac.Tool``, ``description``,
    ``input_model``, ``output_model``, ``event_type``, ``mutating`` and
    ``handler``.
    ``REGISTRY: Mapping[str, ToolSpec[Any, Any]]`` keyed by wire name.
    ``lookup(name) -> ToolSpec[Any, Any]``, raising ``rbac.UnknownToolError``
    (code ``INTERNAL``) for a name that is not registered.
    ``DENIED_EVENT_TYPE``, ``DISABLED_EVENT_TYPE``, ``FAILED_EVENT_TYPE`` —
    the event types the pipeline writes for the non-success cases.

A client-supplied string that lands in a store gets a typed
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
from redis.asyncio import Redis
from sqlalchemy import String
from sqlalchemy.exc import DBAPIError

from helpdesk.mcp_server.tools import approvals, registry, reviews
from helpdesk.mcp_server.tools.base import MutatingToolInput
from helpdesk.mcp_server.tools.registry import REGISTRY, lookup
from helpdesk.shared.access import reviews as reviews_domain
from helpdesk.shared.audit.writer import AuditEventInput, append_event
from helpdesk.shared.auth import rbac
from helpdesk.shared.core.db import create_engine, create_session_factory, tenant_session
from helpdesk.shared.core.errors import ErrorCode
from helpdesk.shared.models import PERIOD_LENGTH, AuditEvent
from helpdesk.shared.services.killswitch import (
    GLOBAL,
    GLOBAL_SEGMENT,
    KEY_PREFIX,
    TOOL_NAME_MAX_LENGTH,
    TOOL_NAME_PATTERN,
    InvalidSwitchTargetError,
    key_for,
)
from tests.support.mcp import (
    SeededMcp,
    bearer,
    call_tool,
    error_code,
    token_for,
)

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
        "ticket.solved",
        # The corpus is what every access decision cites,
        # so a change to it is a governance record and not a generic
        # `tool.succeeded`. The audit event list is explicitly a minimum.
        "policy.reingested",
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
    # decision event themselves: the pipeline's event
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
    record kept of the decision.

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
    """Pins both values exactly. The audit log names
    `request.approved` and `request.rejected`, and the handlers append them
    under exactly those names; a typo would produce an event type nothing in
    the audit UI or the golden set knows, with every other test still green
    because they read the same constant."""
    assert approvals.REQUEST_APPROVED_EVENT_TYPE == "request.approved"
    assert approvals.REQUEST_REJECTED_EVENT_TYPE == "request.rejected"


def test_the_queue_bounds_are_exactly_one_fifty_and_two_hundred() -> None:
    """Pinned exactly: an unbounded list
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
    seeded_mcp_db: SeededMcp,
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
        "solve_ticket",
        "create_access_request",
        # Both record a decision and one of them executes a grant,
        # so both ride the idempotency guard. `list_pending_approvals` is a
        # read and must stay off this list.
        "approve_request",
        "reject_request",
        # It creates a PERSON and their template grants, so a
        # retried call must replay rather than be refused POLICY_BLOCKED by
        # its own duplicate-address check.
        "onboard_user",
        # It revokes and grants in one pass, so a retried call must
        # replay rather than move the person a second time (by then they are
        # already in the new department, and a re-run answers POLICY_BLOCKED
        # for a call the caller saw succeed).
        "transfer_user",
        # It moves a waiting request to cancelled, so a retried call must
        # replay rather than be refused as no longer waiting.
        "cancel_request",
        # It ends live grants, so a retried call must replay rather
        # than re-read the live set and revoke whatever is there now.
        "revoke_access",
        # It revokes every live grant and closes every open request
        # in one pass, so a retried call must replay rather than be refused
        # POLICY_BLOCKED by its own already-leaver check for a call the caller
        # saw succeed.
        "offboard_user",
        # It writes a review and one item per active grant in scope,
        # so a retried call must replay rather than be refused POLICY_BLOCKED
        # by its own duplicate check for a call the caller saw succeed.
        # `list_review_items` is a read and must stay off this list.
        "open_access_review",
        # It writes a decision and can end a grant, so a retried
        # call must replay rather than be refused POLICY_BLOCKED by decision
        # C's already-decided check for a call the caller saw succeed.
        "decide_review_item",
        # It revokes every live grant a request produced and closes
        # the request, so a retried call must replay rather than be refused
        # POLICY_BLOCKED by its own status check for a call the caller saw
        # succeed (by then the request reads `rolled_back`).
        "rollback_request",
        # `mark_notification_read` stamps `read_at` on one row, so
        # it rides the idempotency guard and a retried click replays the
        # stored response instead of re-reading the row. `list_notifications`
        # is a READ and must stay off this list: a key demanded for a bell
        # nobody can clear without minting one would make the notifications
        # page unable to list anything.
        "mark_notification_read",
        # `set_kill_switch`
        # DOES belong here. It is a normal write now, keyed and run through
        # `run_once`, so a retried toggle replays the stored response and
        # writes `tool.replayed` instead of running twice.
        "set_kill_switch",
        # `set_tool_steps_visible` writes the tenant's settings, so it is keyed
        # and runs through `run_once` like `set_kill_switch`: a retried switch
        # replays the stored answer instead of writing twice.
        "set_tool_steps_visible",
        #
        # `list_kill_switches` does NOT belong here, and for
        # the plainer reason — the board is a READ. It issues one `MGET` and
        # writes nothing at all, so a `True` here would demand an idempotency
        # key for a call the guard never protects and the admin page could not
        # render without minting one.
        #
        # `list_sod_rules` must stay off this list too, and for the
        # same plain reason - the rules board is a READ. It issues three
        # SELECTs and writes nothing at all.
        #
        # `list_policy_documents` stays off it for the same reason -
        # two SELECTs over `policy_documents` and `policy_chunks`, and nothing
        # written. A `True` here would demand an idempotency key for a call
        # the guard never protects, so `/admin/policies` could not render
        # without minting one.
        #
        # `reingest_policy_document` DOES belong here. It reads a
        # corpus file, registers a version and writes points and chunk rows,
        # so a retried call must replay the stored response rather than run
        # the ingest a second time - which, on a source the first call already
        # registered, answers `created=false, chunk_count=0` for a call the
        # caller saw land four chunks.
        "reingest_policy_document",
    }
)


def test_the_registry_holds_thirty_eight_tools_with_the_person_lookup_among_them() -> None:
    """Catches a new tool that is never registered (the handler exists and no
    caller can reach it) and a registry grown by a tool nobody decided on.
    The new lookup is also pinned as its own spec: a read (not mutating, so no
    key is demanded), the generic success event, and the matrix member of the
    same name, so a spec pointed at `revoke_access` (whose scope row it shares)
    would not pass."""
    assert len(REGISTRY) == 38
    spec = REGISTRY["list_person_access"]
    assert spec.name == "list_person_access" == rbac.Tool.LIST_PERSON_ACCESS.value
    assert spec.tool is rbac.Tool.LIST_PERSON_ACCESS
    assert spec.event_type == registry.SUCCESS_EVENT_TYPE
    assert spec.mutating is False


def test_the_wire_name_aliases_are_exactly_the_declared_pair() -> None:
    """The alias table is the ONLY licence a wire name
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
        # The matrix gives `create_access_request` two matrix rows for one wire
        # name, so identity is impossible there; the declared pairing is what
        # replaces it.
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
    mcp_env: SeededMcp,
    mcp_redis_url: str,
) -> None:
    """The wire name is validated before it can become a Redis
    key. Redis has no length or shape limit, so an unchecked name would write
    a key nothing reads."""
    assert _redis_keys(mcp_redis_url, f"{KEY_PREFIX}:*") == []

    result = call_tool(bearer(token_for(mcp_env.tenant_a)), UNREGISTERED_NAME, {})

    assert error_code(result) == "INTERNAL"
    assert _redis_keys(mcp_redis_url, f"{KEY_PREFIX}:*") == []
    assert _redis_keys(mcp_redis_url, f"*{UNREGISTERED_NAME}*") == []


# --- the review tools ---------------------------------------


REVIEW_TOOL_EXPECTATIONS: tuple[tuple[str, rbac.Tool, str, bool], ...] = (
    # Both declare the GENERIC success event and the opener appends its own
    # `review.opened`: the pipeline's event carries a fixed
    # payload and the TOOL as its subject, which would record that somebody
    # called a tool and not what was reviewed.
    ("open_access_review", rbac.Tool.OPEN_ACCESS_REVIEW, registry.SUCCESS_EVENT_TYPE, True),
    ("list_review_items", rbac.Tool.LIST_REVIEW_ITEMS, registry.SUCCESS_EVENT_TYPE, False),
    # The queue. A READ — it writes nothing at all — so it declares
    # the generic success event and stays OFF `MUTATING_TOOL_NAMES` above; a
    # `True` here would demand an idempotency key for a call the guard never
    # protects, and the web's `/reviews` page would be unable to list anything
    # without minting one.
    ("list_reviews", rbac.Tool.LIST_REVIEWS, registry.SUCCESS_EVENT_TYPE, False),
)


@pytest.mark.parametrize(
    ("name", "tool", "event_type", "mutating"),
    REVIEW_TOOL_EXPECTATIONS,
    ids=[name for name, _, _, _ in REVIEW_TOOL_EXPECTATIONS],
)
def test_each_review_tool_is_registered_once_under_its_matrix_name(
    name: str, tool: rbac.Tool, event_type: str, mutating: bool
) -> None:
    """Catches the opener pointed at `DECIDE_REVIEW_ITEM` (whose the matrix row
    is identical, so no permission test anywhere would notice), the read
    declared as mutating — which would demand an idempotency key for a call
    the guard never protects — and the opener declared as a read, which skips
    the guard entirely so a retried open writes a second review.

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


def test_the_review_opened_event_type_is_exactly_the_spec_string() -> None:
    """Pins the value exactly. The audit log names
    `review.opened` and the handler appends it under that name; a typo would
    produce an event type nothing in the audit UI or the golden set knows,
    with every other test still green because they read the same constant."""
    assert reviews_domain.REVIEW_OPENED_EVENT_TYPE == "review.opened"


# --- the notification tools -----------------------------------------


NOTIFICATION_TOOL_EXPECTATIONS: tuple[tuple[str, rbac.Tool, str, bool], ...] = (
    # A READ: it writes nothing at all, so it declares the generic success
    # event and stays OFF `MUTATING_TOOL_NAMES` above.
    ("list_notifications", rbac.Tool.LIST_NOTIFICATIONS, registry.SUCCESS_EVENT_TYPE, False),
    # A MUTATION that declares the GENERIC success event on purpose:
    # "a person opened their own bell" answers no auditor's question, and
    # a dedicated event type would put one row in the hash chain per UI
    # interaction.
    (
        "mark_notification_read",
        rbac.Tool.MARK_NOTIFICATION_READ,
        registry.SUCCESS_EVENT_TYPE,
        True,
    ),
)


@pytest.mark.parametrize(
    ("name", "tool", "event_type", "mutating"),
    NOTIFICATION_TOOL_EXPECTATIONS,
    ids=[name for name, _, _, _ in NOTIFICATION_TOOL_EXPECTATIONS],
)
def test_each_notification_tool_is_registered_once_under_its_matrix_name(
    name: str, tool: rbac.Tool, event_type: str, mutating: bool
) -> None:
    """Catches the list declared as mutating — which would demand an
    idempotency key for a call the guard never protects, so the bell could not
    be read without minting one — and the stamp declared as a read, which
    skips the guard entirely so a double-clicked bell runs the effect twice.
    It also catches either spec pointed at the other's matrix member, whose
    the matrix rows are identical (`self_only` for all seven principals), so no
    permission test anywhere would notice.

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


# --- the admin board's read half ------------------------------------


ADMIN_READ_TOOL_EXPECTATIONS: tuple[tuple[str, rbac.Tool, str, bool], ...] = (
    # A READ over Redis: one `MGET` and nothing written, so it declares the
    # GENERIC success event and stays OFF `MUTATING_TOOL_NAMES` above. There is
    # no domain event to append — an admin opening the switch board changed
    # nothing, and a `killswitch.changed` per render would file a governance
    # record for a page view.
    ("list_kill_switches", rbac.Tool.LIST_KILL_SWITCHES, registry.SUCCESS_EVENT_TYPE, False),
    # The SoD rules board, a READ over Postgres for exactly the
    # same reasons - three SELECTs and nothing written, so the GENERIC success
    # event and OFF `MUTATING_TOOL_NAMES` above. The table described
    # twenty-six tools and now describes twenty-seven.
    ("list_sod_rules", rbac.Tool.LIST_SOD_RULES, registry.SUCCESS_EVENT_TYPE, False),
    # The policy documents board, a READ over Postgres for exactly
    # the same reasons - two SELECTs and nothing written, so the GENERIC
    # success event and OFF `MUTATING_TOOL_NAMES` above. The table described
    # twenty-seven tools and now describes twenty-eight.
    (
        "list_policy_documents",
        rbac.Tool.LIST_POLICY_DOCUMENTS,
        registry.SUCCESS_EVENT_TYPE,
        False,
    ),
)


@pytest.mark.parametrize(
    ("name", "tool", "event_type", "mutating"),
    ADMIN_READ_TOOL_EXPECTATIONS,
    ids=[name for name, _, _, _ in ADMIN_READ_TOOL_EXPECTATIONS],
)
def test_the_kill_switch_board_is_registered_once_under_its_matrix_name(
    name: str, tool: rbac.Tool, event_type: str, mutating: bool
) -> None:
    """Catches the board's spec pointed at `rbac.Tool.SET_KILL_SWITCH`, whose
    the matrix row is identical (`platform_admin` alone, at `Scope.ANY`), so no
    permission test anywhere would notice — and every kill switch the pipeline
    checked for this tool would be keyed on the writer's name instead. It also
    catches the board declared as MUTATING, which demands an idempotency key
    for a call the guard never protects, and a declared
    `killswitch.changed`, which would record a change on every render.

    The name is written out literally rather than derived from the registry:
    reading the value under test back out of the code under test would assert
    nothing.

    `list_sod_rules` is a second row of this table, where the
    equivalent slip is a spec pointed at `rbac.Tool.LIST_KILL_SWITCHES` - the
    other admin READ, and the neighbour whose shape it copies.

    `list_policy_documents` is a third row. Its equivalent slip
    is a spec pointed at `rbac.Tool.LIST_SOD_RULES`, whose matrix row
    is identical (`it_admin` and `platform_admin` at `Scope.ANY`), so no
    permission test anywhere would notice and every kill switch the pipeline
    checked for the corpus board would be keyed on the rules board's name.
    """
    spec = REGISTRY[name]

    assert spec.name == name == tool.value
    assert spec.tool is tool
    assert spec.event_type == event_type
    assert spec.mutating is mutating
    assert name not in registry.WIRE_NAME_ALIASES


# --- the admin board's write half ------------------------------------


ADMIN_MUTATION_TOOL_EXPECTATIONS: tuple[tuple[str, rbac.Tool, str, bool], ...] = (
    # The re-ingest. A MUTATION over the filesystem, Qdrant and
    # Postgres, so it rides the idempotency guard and IS on
    # `MUTATING_TOOL_NAMES` above - and it declares its OWN success event
    # rather than the generic one, which is what separates it
    # from every row of the admin READ table above.
    (
        "reingest_policy_document",
        rbac.Tool.REINGEST_POLICY_DOCUMENT,
        "policy.reingested",
        True,
    ),
)


@pytest.mark.parametrize(
    ("name", "tool", "event_type", "mutating"),
    ADMIN_MUTATION_TOOL_EXPECTATIONS,
    ids=[name for name, _, _, _ in ADMIN_MUTATION_TOOL_EXPECTATIONS],
)
def test_the_reingest_tool_is_registered_once_under_its_matrix_name(
    name: str, tool: rbac.Tool, event_type: str, mutating: bool
) -> None:
    """Catches the spec pointed at `rbac.Tool.LIST_POLICY_DOCUMENTS`, whose
    the matrix row is identical (`it_admin` and `platform_admin` at
    `Scope.ANY`) and whose module and page it shares, so no permission test
    anywhere would notice - and every kill switch the pipeline checked for the
    re-ingest would be keyed on the board's name, leaving the mutation running
    while an operator believed they had stopped it.

    It also catches the tool declared as a READ, which skips the idempotency
    guard entirely so a retried re-ingest runs the whole ingest again, and a
    declared `tool.succeeded`, which would leave `search_audit` with no way to
    answer "when was this corpus last rewritten" - the one question the event
    exists for.

    The name and the event type are written out literally rather than derived
    from the registry: reading the value under test back out of the code under
    test would assert nothing.
    """
    spec = REGISTRY[name]

    assert spec.name == name == tool.value
    assert spec.tool is tool
    assert spec.event_type == event_type
    assert spec.mutating is mutating
    assert name not in registry.WIRE_NAME_ALIASES


def test_the_review_item_bounds_are_exactly_one_fifty_and_two_hundred() -> None:
    """Pinned exactly: an unbounded item
    list is an unbounded response over the wire, and a default that drifts
    changes what every caller who omits `limit` receives. `PERIOD_LENGTH` is
    the column's own size and the input model checks the client's string
    against it, so widening one without the other fails here."""
    assert reviews.LIMIT_MIN == 1
    assert reviews.LIMIT_DEFAULT == 50
    assert reviews.LIMIT_MAX == 200
    assert PERIOD_LENGTH == 16
