"""An earlier change, sections 1 and 6: the three approval tools, through the transport.

Surface under test (written before it exists):

``mcp_server.tools.approvals``
    ``list_pending_approvals(limit=50)`` — a registered READ tool (registry
    event ``tool.succeeded``) answering "what is waiting for ME": pending
    rows in the caller's scope that carry an open slot this caller may fill
    (decision G), oldest first, at most ``limit`` (decision H,
    ``LIMIT_MIN = 1``, ``LIMIT_DEFAULT = 50``, ``LIMIT_MAX = 200``).
    ``approve_request(request_id, comment?, idempotency_key)`` and
    ``reject_request(request_id, reason, idempotency_key)`` — MUTATING tools
    that APPEND ``request.approved`` / ``request.rejected`` themselves
    (decision I), recording the caller against exactly ONE open slot,
    executing the grant through an earlier change's ``execute_grant`` when the last
    slot is approved, and refusing a non-pending row ``POLICY_BLOCKED``
    (decision J), a caller matching no open slot ``FORBIDDEN``, and an
    unknown or other-tenant ``request_id`` ``NOT_FOUND``.
    ``COMMENT_MAX_LENGTH = 2000``, ``REASON_MAX_LENGTH = 2000``.

Audit deltas are asserted as exact event-type lists against a before
snapshot, never as counts. The handlers append their domain
event BEFORE execution and the pipeline's generic ``tool.succeeded`` lands
last, so a completing approval reads ``request.approved``, ``grant.executed``,
``tool.succeeded`` in ``seq`` order — and an approval that completed a chain
but FAILED execution (decision E) differs from it by the missing
``grant.executed`` and nothing else.

Fixture cost: everything rides an earlier change's session-scoped
``mcp_fixtures`` seam — one seeded throwaway database, one dedicated Redis
index flushed per test. Per-test isolation inside the shared database is a
namespace: each case owns seeded users no other case decides for, plants its
own request rows, and sends a unique idempotency key per call. Seeded
identities are recomputed from ``scripts.seed.generate_tenant`` —
deterministic uuid5 ids, never hardcoded UUIDs.

No pytest-asyncio: each case is a sync function driving one ``asyncio.run``
(the concurrency case drives two, one per thread).
"""

import asyncio
import uuid
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any

import pytest
from helpdesk.access.approvals import SlotDecision
from helpdesk.core.db import create_engine, create_session_factory, tenant_session
from helpdesk.core.repository import Repository
from helpdesk.models import AccessRequest, RequestStatus
from helpdesk.policy.types import ApproverSlot
from mcp_fixtures import (  # noqa: F401 - fixtures are resolved by name
    SeededMcp,
    SeededUser,
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
from mcp_server.tools.approvals import (
    COMMENT_MAX_LENGTH,
    LIMIT_DEFAULT,
    LIMIT_MAX,
    LIMIT_MIN,
    REASON_MAX_LENGTH,
    ListPendingApprovalsInput,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from scripts.seed import CUAN, NORDLICHT, UserRow, generate_tenant

LIST = "list_pending_approvals"
APPROVE = "approve_request"
REJECT = "reject_request"

# the design's two decision events (the registry event_type of each deciding
# tool, decision I) and the grant event an earlier change declares.
REQUEST_APPROVED_EVENT = "request.approved"
REQUEST_REJECTED_EVENT = "request.rejected"
GRANT_EXECUTED_EVENT = "grant.executed"
# The pipeline's own event for both deciding tools: they append their SPEC
# §13.1 domain event themselves (decision I), because the pipeline's event
# carries a fixed payload and the TOOL as its subject, which would record
# that somebody called a tool and not which request they decided.
SUCCEEDED_EVENT = "tool.succeeded"
DENIED_EVENT = "tool.denied"
REPLAYED_EVENT = "tool.replayed"

ROWS = generate_tenant(NORDLICHT)
OTHER_TENANT_ROWS = generate_tenant(CUAN)

# The seeded manager of every department is index 0 (`scripts.seed`
# `_assign_principals`), so this token really carries the `manager` principal.
PRODUCTION_MANAGER = ROWS.users_by_department["Production"][0]
# A Production user who is NOT the manager: the seat that names them is one
# the manager may not fill.
NOT_THE_MANAGER = ROWS.users_by_department["Production"][60]

# The seeded HR officer (HR index 1) and auditor (index 1 of the auditor
# department): the design gives neither a cell on the approval row.
HR_OFFICER = ROWS.users_by_department["HR"][1]
# The `prod-k8s` owner is an IT user carrying `system_owner`; the two seeded
# `it_admin`s and the `platform_admin` follow the owners in the IT department.
PROD_K8S_OWNER = ROWS.system_owners["prod-k8s"]
IT_ADMIN_ONE = ROWS.users_by_department["IT"][16]
IT_ADMIN_TWO = ROWS.users_by_department["IT"][17]
PLATFORM_ADMIN = ROWS.users_by_department["IT"][18]

# Seeded users this module owns. Every index avoids the seed's planted
# anomalies (a Production user's tenant_index is 46 + index; `% 20 == 7` is
# the stale grant and `% 15 == 4` the out-of-template one), so nothing here
# classifies differently than the plain template says.
REQUESTER = ROWS.users_by_department["Production"][55]
MANAGER_QUEUE_TARGET = ROWS.users_by_department["Production"][40]
SLOT_ONLY_TARGET = ROWS.users_by_department["Production"][42]
DIRECTORY_TARGET = ROWS.users_by_department["Production"][43]
UNION_SYSTEM_TARGET = ROWS.users_by_department["Production"][44]
TWO_SLOT_TARGET = ROWS.users_by_department["Production"][45]
COMPLETE_TARGET = ROWS.users_by_department["Production"][46]
SOD_TARGET = ROWS.users_by_department["Production"][47]
REJECT_TARGET = ROWS.users_by_department["Production"][49]
NO_SEAT_TARGET = ROWS.users_by_department["Production"][50]
CONCURRENCY_TARGET = ROWS.users_by_department["Production"][51]
COMMENT_TARGET = ROWS.users_by_department["Production"][52]
REASON_TARGET = ROWS.users_by_department["Production"][53]
TWO_SEAT_TARGET = ROWS.users_by_department["Production"][64]
TRANSFER_TARGET = ROWS.users_by_department["Production"][65]
EVENT_TARGET = ROWS.users_by_department["Production"][66]
# Named on no other case's seat, which is what lets the ordering case below
# assert its queue as an exact list: `NOT_THE_MANAGER` is already named on the
# seat the eligibility case plants, so a caller with the `any` scope would see
# that row too.
LIMIT_APPROVER = ROWS.users_by_department["Production"][62]
LIMIT_TARGET_ONE = ROWS.users_by_department["Production"][54]
LIMIT_TARGET_TWO = ROWS.users_by_department["Production"][56]
EXTRA_ARG_TARGET = ROWS.users_by_department["Production"][57]
NOT_PENDING_TARGET = ROWS.users_by_department["Production"][58]
HR_SEAT_TARGET = ROWS.users_by_department["Production"][59]
# Same tenant, another department: a cross-tenant target
# would be refused by the tenant guard whether the scope rule holds or not.
SALES_TARGET = ROWS.users_by_department["Sales"][12]
UNION_DEPARTMENT_TARGET = ROWS.users_by_department["IT"][20]

# Outside Production's template and named by no SoD rule: an approval that
# completes can execute, and its only possible refusal is one this test
# plants itself.
GRANTABLE_ROLE = ROWS.role_ids[("grafana", "viewer")]
# `hris.viewer` is a read role with `default_expiry_days = 365` on a system
# seeded `critical`, so a FRESH classification caps the grant at 30 days.
# That cap, not the role default, is what pins "the expiry is the one the
# fresh classification computed".
CAPPED_ROLE = ROWS.role_ids[("hris", "viewer")]
CRITICAL_CLASS_CAP_DAYS = 30
ROLE_DEFAULT_EXPIRY_DAYS = 365
# The seeded SoD pair (SoD-STD §2.1); Production's template holds neither.
SOD_REQUEST_ROLE = ROWS.role_ids[("payments", "approver")]
SOD_CONFLICTING_ROLE = ROWS.role_ids[("payments", "initiator")]
# `prod-k8s` is owned by PROD_K8S_OWNER; `crm` is owned by somebody else, so
# an `own_systems` filter cannot reach a request for it.
OWNED_SYSTEM_ROLE = ROWS.role_ids[("prod-k8s", "deployer")]
UNOWNED_ROLE = ROWS.role_ids[("crm", "editor")]

# The second seeded tenant has its own department names ("Operations", not
# "Sales"): all these two have to be is a tenant this caller's token cannot see.
OTHER_TENANT_REQUESTER = OTHER_TENANT_ROWS.users_by_department["Operations"][2]
OTHER_TENANT_TARGET = OTHER_TENANT_ROWS.users_by_department["Operations"][3]
OTHER_TENANT_ROLE = next(iter(OTHER_TENANT_ROWS.role_ids.values()))


def _seeded(user: UserRow) -> SeededUser:
    """A token-ready handle for a deterministic seeded user."""
    return SeededUser(
        tenant_id=ROWS.tenant_id,
        tenant_slug=NORDLICHT.slug,
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        department_id=ROWS.department_ids[user.department],
        principals=tuple(sorted(user.principals)),
        status="active",
    )


def _key() -> str:
    """A unique idempotency key: half the per-test namespace."""
    return f"fake-key-{uuid.uuid4().hex}"


def _seat(slot: ApproverSlot, user_id: uuid.UUID | None = None) -> dict[str, Any]:
    """One stored open seat, exactly the JSONB shape an earlier change writes."""
    return {"slot": slot.value, "user_id": None if user_id is None else str(user_id)}


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


# --- planting rows around the tools --------------------------------------------


def _plant_request(
    database_url: str,
    *,
    requester_user_id: uuid.UUID,
    target_user_id: uuid.UUID,
    role_id: uuid.UUID,
    approvals: Sequence[dict[str, Any]],
    status: RequestStatus = RequestStatus.PENDING,
    tenant_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """One committed `access_requests` row carrying the chain a case needs."""

    async def scenario() -> uuid.UUID:
        engine = create_engine(database_url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, tenant_id or ROWS.tenant_id) as session:
                row = await Repository(session, AccessRequest).create(
                    requester_user_id=requester_user_id,
                    target_user_id=target_user_id,
                    role_id=role_id,
                    justification=f"fake pending request {uuid.uuid4().hex}",
                    requested_expiry_days=ROLE_DEFAULT_EXPIRY_DAYS,
                    risk_class="critical",
                    decision="approval",
                    decision_reasons=[],
                    citations=[],
                    status=status.value,
                    approver_user_ids=[],
                    approvals=list(approvals),
                    session_id=None,
                    trace_id=None,
                    idempotency_key=None,
                )
                return row.id
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


# --- raw-SQL probes (reading around the tools, never through them) -------------


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


def _request_row(database_url: str, request_id: uuid.UUID) -> Any:
    rows = _rows(
        database_url,
        "SELECT id, status, approvals, approver_user_ids FROM access_requests WHERE id = :id",
        {"id": request_id},
    )
    assert len(rows) == 1, f"expected exactly one request row, got {len(rows)}"
    return rows[0]


def _grant_count(database_url: str, tenant_id: uuid.UUID) -> int:
    rows = _rows(
        database_url,
        "SELECT count(*) FROM grants WHERE tenant_id = :tenant",
        {"tenant": tenant_id},
    )
    return int(rows[0][0])


def _grants_for_request(database_url: str, request_id: uuid.UUID) -> list[Any]:
    return _rows(
        database_url,
        "SELECT id, user_id, role_id, granted_at, expires_at, status"
        " FROM grants WHERE request_id = :request",
        {"request": request_id},
    )


def _execute(database_url: str, sql: str, params: dict[str, Any]) -> None:
    """One committed statement written around the tools."""

    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as conn:
                await conn.execute(text(sql), params)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def _insert_grant(database_url: str, *, user_id: uuid.UUID, role_id: uuid.UUID) -> None:
    """A live grant written around the tools, while the request waits."""
    _execute(
        database_url,
        "INSERT INTO grants (id, tenant_id, user_id, role_id, request_id,"
        " granted_by_user_id, granted_at, expires_at, revoked_at, revoke_reason, status,"
        " created_at, updated_at)"
        " VALUES (:id, :tenant, :user, :role, NULL, NULL, now(), NULL, NULL, NULL,"
        " 'active', now(), now())",
        {"id": uuid.uuid4(), "tenant": ROWS.tenant_id, "user": user_id, "role": role_id},
    )


def _set_user_department(database_url: str, user_id: uuid.UUID, department_id: uuid.UUID) -> None:
    """Move one user between departments, around the tools."""
    _execute(
        database_url,
        "UPDATE users SET department_id = :department WHERE id = :id",
        {"department": department_id, "id": user_id},
    )


def _decision_args(tool_name: str, request_id: uuid.UUID | str, **extra: Any) -> dict[str, Any]:
    """Minimally valid arguments for whichever deciding tool is under test.

    Built per tool rather than shared: `reason` is an EXTRA field on
    `approve_request`, and a mutating input forbids extras, so one shared
    argument dict would answer VALIDATION where the case expects the refusal
    it is actually about.
    """
    arguments: dict[str, Any] = {"request_id": str(request_id), "idempotency_key": _key(), **extra}
    if tool_name == REJECT:
        arguments.setdefault("reason", f"fake rejection reason {uuid.uuid4().hex}")
    return arguments


def _slots(approvals: Any) -> list[tuple[str, str | None]]:
    """The stored chain as (slot, decision), order preserved."""
    return [(str(entry["slot"]), entry.get("decision")) for entry in approvals]


def _queue_ids(payload: dict[str, Any]) -> list[str]:
    return [str(entry["request_id"]) for entry in payload["approvals"]]


# --- the queue: who sees what (criteria 1, 2 and 3) -----------------------------


def test_a_manager_sees_a_pending_row_in_their_department_that_names_them(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """The allow direction, which every refusal case below depends on: a
    queue returning nothing at all would pass them and fail here.

    Catches a queue that answers with bare ids as well — the caller is told
    WHICH seat is theirs (`my_slot`) and what they are deciding, and without
    that an approver cannot act on the row they were shown.
    """
    manager = _seeded(PRODUCTION_MANAGER)
    target = _seeded(MANAGER_QUEUE_TARGET)
    assert "manager" in manager.principals, "premise: the seeded index 0 is the manager"
    assert manager.department_id == target.department_id
    request_id = _plant_request(
        mcp_env.database_url,
        requester_user_id=REQUESTER.id,
        target_user_id=target.user_id,
        role_id=GRANTABLE_ROLE,
        approvals=[_seat(ApproverSlot.DEPARTMENT_MANAGER, manager.user_id)],
    )

    payload = _structured(call_tool(bearer(token_for(manager)), LIST, {}))

    entries = [entry for entry in payload["approvals"] if entry["request_id"] == str(request_id)]
    assert len(entries) == 1, payload["approvals"]
    entry = entries[0]
    assert entry["my_slot"] == ApproverSlot.DEPARTMENT_MANAGER.value
    assert entry["target_user_id"] == str(target.user_id)
    assert entry["requester_user_id"] == str(REQUESTER.id)
    assert entry["role_id"] == str(GRANTABLE_ROLE)
    assert entry["status"] == RequestStatus.PENDING.value
    assert _slots(entry["approvals"]) == [(ApproverSlot.DEPARTMENT_MANAGER.value, None)]


def test_the_scope_filter_alone_hides_a_row_whose_seat_names_the_caller(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """a project rule. The open seat NAMES the caller, so the eligibility
    filter admits this row and only the `own_department` scope refuses it:
    the target sits in Sales while the caller manages Production. Deleting
    the scope filter is the one change that flips this case, and both users
    are in the same tenant so the tenant guard cannot
    produce the refusal on its behalf."""
    manager = _seeded(PRODUCTION_MANAGER)
    outsider = _seeded(SALES_TARGET)
    assert manager.tenant_id == outsider.tenant_id, "the point of this test"
    assert manager.department_id != outsider.department_id
    request_id = _plant_request(
        mcp_env.database_url,
        requester_user_id=REQUESTER.id,
        target_user_id=outsider.user_id,
        role_id=GRANTABLE_ROLE,
        approvals=[_seat(ApproverSlot.DEPARTMENT_MANAGER, manager.user_id)],
    )

    payload = _structured(call_tool(bearer(token_for(manager)), LIST, {}))

    assert str(request_id) not in _queue_ids(payload)


def test_the_seat_filter_alone_hides_a_row_in_the_callers_own_department(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """a project rule, the other half: the target IS in the caller's
    department, so the scope filter admits the row and only the eligibility
    filter refuses it — the open seat names another Production user.

    Without this case a queue with no eligibility filter (decision G's B
    option, "everything pending in scope") passes every other queue test and
    shows a manager rows only somebody else can move.
    """
    manager = _seeded(PRODUCTION_MANAGER)
    target = _seeded(SLOT_ONLY_TARGET)
    other = _seeded(NOT_THE_MANAGER)
    assert manager.department_id == target.department_id, "the point of this test"
    assert other.user_id != manager.user_id
    request_id = _plant_request(
        mcp_env.database_url,
        requester_user_id=REQUESTER.id,
        target_user_id=target.user_id,
        role_id=GRANTABLE_ROLE,
        approvals=[_seat(ApproverSlot.DEPARTMENT_MANAGER, other.user_id)],
    )

    payload = _structured(call_tool(bearer(token_for(manager)), LIST, {}))

    assert str(request_id) not in _queue_ids(payload)


def test_the_managers_department_comes_from_the_directory_not_the_token(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches the `own_department` filter reading the token's
    `department_id` claim instead of the caller's current directory row
    (criterion 2).

    The token is minted while the manager is in Production and is NOT
    reissued; the manager's row then moves to Sales. Two rows are planted,
    both naming the manager on their open seat, one targeting Production and
    one targeting Sales — so an implementation trusting the claim returns the
    Production row and an implementation reading the directory returns the
    Sales one. Asserting both directions also catches a filter that returns
    everything: in the seed the claim and the row always agree, so without
    this move both implementations pass.
    """
    manager = _seeded(PRODUCTION_MANAGER)
    token = token_for(manager)
    sales_department_id = ROWS.department_ids["Sales"]
    assert sales_department_id != manager.department_id
    production_request = _plant_request(
        mcp_env.database_url,
        requester_user_id=REQUESTER.id,
        target_user_id=DIRECTORY_TARGET.id,
        role_id=GRANTABLE_ROLE,
        approvals=[_seat(ApproverSlot.DEPARTMENT_MANAGER, manager.user_id)],
    )
    sales_request = _plant_request(
        mcp_env.database_url,
        requester_user_id=REQUESTER.id,
        target_user_id=SALES_TARGET.id,
        role_id=GRANTABLE_ROLE,
        approvals=[_seat(ApproverSlot.DEPARTMENT_MANAGER, manager.user_id)],
    )

    _set_user_department(mcp_env.database_url, manager.user_id, sales_department_id)
    try:
        payload = _structured(call_tool(bearer(token), LIST, {}))
    finally:
        _set_user_department(mcp_env.database_url, manager.user_id, manager.department_id)

    queued = _queue_ids(payload)
    assert str(sales_request) in queued
    assert str(production_request) not in queued


def test_a_manager_and_system_owner_caller_sees_the_union_of_both_queues(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches the handler reading `Authorization.scope` instead of
    `.scopes` — criterion 3, the narrowing an earlier change recorded and 11a re-owed.

    `own_department` is WIDER than `own_systems` in the total order, so a
    caller holding both principals collapses to `own_department` alone and
    loses every request on the systems they own. One row is reachable only
    through the department scope (an IT target on a role the caller does not
    own) and the other only through the systems scope (a Production target on
    the caller's own system), so an implementation using the widest single
    scope returns exactly one of them and this case fails.
    """
    owner = _seeded(PROD_K8S_OWNER)
    assert "system_owner" in owner.principals, "premise: the seeded owner carries the principal"
    token = token_for(owner, principals=("manager", "system_owner"))
    department_request = _plant_request(
        mcp_env.database_url,
        requester_user_id=REQUESTER.id,
        target_user_id=UNION_DEPARTMENT_TARGET.id,
        role_id=UNOWNED_ROLE,
        approvals=[_seat(ApproverSlot.DEPARTMENT_MANAGER, owner.user_id)],
    )
    systems_request = _plant_request(
        mcp_env.database_url,
        requester_user_id=REQUESTER.id,
        target_user_id=UNION_SYSTEM_TARGET.id,
        role_id=OWNED_SYSTEM_ROLE,
        approvals=[_seat(ApproverSlot.SYSTEM_OWNER, owner.user_id)],
    )

    payload = _structured(call_tool(bearer(token), LIST, {}))

    queued = _queue_ids(payload)
    assert str(department_request) in queued, "the own_department half of the union is missing"
    assert str(systems_request) in queued, "the own_systems half of the union is missing"


def test_the_queue_is_oldest_first_and_stops_at_the_limit(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches a `limit` accepted and ignored — decision H's unbounded
    response over the wire — and an unordered queue, which starves the oldest
    request. The approver here is named on no other case's seat, so their
    queue is exactly these two rows and the order is an exact list."""
    approver = _seeded(LIMIT_APPROVER)
    token = token_for(approver, principals=("it_admin",))
    first = _plant_request(
        mcp_env.database_url,
        requester_user_id=REQUESTER.id,
        target_user_id=LIMIT_TARGET_ONE.id,
        role_id=GRANTABLE_ROLE,
        approvals=[_seat(ApproverSlot.DEPARTMENT_MANAGER, approver.user_id)],
    )
    second = _plant_request(
        mcp_env.database_url,
        requester_user_id=REQUESTER.id,
        target_user_id=LIMIT_TARGET_TWO.id,
        role_id=GRANTABLE_ROLE,
        approvals=[_seat(ApproverSlot.DEPARTMENT_MANAGER, approver.user_id)],
    )

    everything = _structured(call_tool(bearer(token), LIST, {}))
    capped = _structured(call_tool(bearer(token), LIST, {"limit": 1}))

    assert _queue_ids(everything) == [str(first), str(second)]
    assert _queue_ids(capped) == [str(first)]


@pytest.mark.parametrize(
    ("limit", "accepted"),
    [
        pytest.param(LIMIT_MIN, True, id="limit-1-ok"),
        pytest.param(LIMIT_MAX, True, id="limit-200-ok"),
        pytest.param(LIMIT_MIN - 1, False, id="limit-0-refused"),
        pytest.param(LIMIT_MAX + 1, False, id="limit-201-refused"),
    ],
)
def test_the_limit_boundary_is_inclusive_at_1_and_200(
    mcp_env: SeededMcp,  # noqa: F811
    limit: int,
    accepted: bool,
) -> None:
    """Catches decision H's bounds being absent or exclusive. Both ends are
    asserted in both directions: a one-sided check cannot tell an inclusive
    bound from an exclusive one, and an unbounded `limit` is an unbounded
    response over the wire."""
    manager = _seeded(PRODUCTION_MANAGER)

    result = call_tool(bearer(token_for(manager)), LIST, {"limit": limit})

    if accepted:
        assert result.is_error is False, result.content
    else:
        assert error_code(result) == "VALIDATION"


def test_the_default_limit_is_exactly_fifty() -> None:
    """Pins the default as a value, not a band: it
    decides what every caller who omits `limit` receives, and a docstring is
    not a record. Read off the input model, so a constant that stops being
    wired into the field fails here."""
    assert LIMIT_DEFAULT == 50
    assert ListPendingApprovalsInput().limit == 50


def test_the_queue_ignores_an_unknown_argument(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """The read tools' contract (`extra="ignore"`), asserted so a later edit
    cannot quietly give a read the mutating models' `extra="forbid"` and
    break agents that send a spare field."""
    manager = _seeded(PRODUCTION_MANAGER)

    result = call_tool(bearer(token_for(manager)), LIST, {"limit": 5, "cursor": "fake"})

    assert result.is_error is False, result.content


# --- the design's denials on the approval row --------------------------------------


@pytest.mark.parametrize("tool_name", [LIST, APPROVE, REJECT])
def test_an_hr_officer_cannot_reach_the_approval_tools_at_all(
    mcp_env: SeededMcp,  # noqa: F811
    tool_name: str,
) -> None:
    """Pins an earlier change §5's recorded the design / §9.3 conflict, so it reads as
    the behaviour that was decided rather than as an oversight.

    §9.3 puts an `hr` seat on a `critical` request against an HRIS system;
    §7's approval row gives `hr` no cell at all. This plan implements §7 as
    written (the matrix is the single source of truth and changing the table
    is the owner's call), so a critical HRIS chain contains a seat NOBODY may
    fill: the `hr` caller is refused here at the RBAC stage, and an
    `it_admin` or `platform_admin` does not hold the `hr` principal the seat
    names — which the case below asserts. The row parks in `pending` for
    ever. Catches someone "fixing" the conflict by adding `hr` to the matrix
    without an ADR.
    """
    officer = _seeded(HR_OFFICER)
    assert "hr" in officer.principals, "premise: the seeded HR index 1 is the officer"
    events_before = _audit_event_types(mcp_env.database_url, officer.tenant_id)

    arguments = {} if tool_name == LIST else _decision_args(tool_name, uuid.uuid4())
    result = call_tool(bearer(token_for(officer)), tool_name, arguments)

    assert error_code(result) == "FORBIDDEN"
    events_after = _audit_event_types(mcp_env.database_url, officer.tenant_id)
    assert events_after == [*events_before, DENIED_EVENT]


def test_an_it_admin_may_not_fill_the_hr_seat_a_critical_hris_chain_names(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """The second half of the conflict above: catches a group seat matched
    against "any group principal", which would let an `it_admin` fill an `hr`
    seat and silently shorten the §9.3 chain the seat exists for.

    The caller's matrix cell ALLOWS `approve_request` (`it_admin`, scope
    `any`), and the row is pending, so neither the RBAC stage nor the status
    check can produce this refusal — only the seat rule can.
    """
    admin = _seeded(IT_ADMIN_ONE)
    assert "it_admin" in admin.principals, "premise: the seeded IT index 16 is an it_admin"
    assert "hr" not in admin.principals
    request_id = _plant_request(
        mcp_env.database_url,
        requester_user_id=REQUESTER.id,
        target_user_id=HR_SEAT_TARGET.id,
        role_id=CAPPED_ROLE,
        approvals=[_seat(ApproverSlot.HR)],
    )
    events_before = _audit_event_types(mcp_env.database_url, admin.tenant_id)
    grants_before = _grant_count(mcp_env.database_url, admin.tenant_id)

    result = call_tool(
        bearer(token_for(admin)),
        APPROVE,
        {"request_id": str(request_id), "idempotency_key": _key()},
    )

    assert error_code(result) == "FORBIDDEN"
    assert _request_row(mcp_env.database_url, request_id).status == RequestStatus.PENDING.value
    assert _grant_count(mcp_env.database_url, admin.tenant_id) == grants_before
    events_after = _audit_event_types(mcp_env.database_url, admin.tenant_id)
    assert events_after == [*events_before, DENIED_EVENT]


# --- approving (criteria 4, 5, 6 and 7) -----------------------------------------


def test_approving_one_of_two_open_slots_leaves_the_row_pending_and_grants_nothing(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Criterion 7. Catches a handler that executes on the FIRST approval —
    a two-seat chain reduced to one approver is the control halved — and one
    that marks the row `approved` while a seat is still open.

    The audit delta is the exact list, so a missing `request.approved` (the
    only record that this approver decided) is visible where a count would
    hide it, and a stray `grant.executed` fails immediately.
    """
    manager = _seeded(PRODUCTION_MANAGER)
    request_id = _plant_request(
        mcp_env.database_url,
        requester_user_id=REQUESTER.id,
        target_user_id=TWO_SLOT_TARGET.id,
        role_id=GRANTABLE_ROLE,
        approvals=[
            _seat(ApproverSlot.DEPARTMENT_MANAGER, manager.user_id),
            _seat(ApproverSlot.IT_ADMIN),
        ],
    )
    events_before = _audit_event_types(mcp_env.database_url, manager.tenant_id)
    grants_before = _grant_count(mcp_env.database_url, manager.tenant_id)

    payload = _structured(
        call_tool(
            bearer(token_for(manager)),
            APPROVE,
            {"request_id": str(request_id), "idempotency_key": _key()},
        )
    )

    assert payload["chain_complete"] is False
    assert payload["status"] == RequestStatus.PENDING.value
    assert payload["slot"] == ApproverSlot.DEPARTMENT_MANAGER.value
    assert payload["grant_id"] is None
    assert payload["expires_at"] is None
    assert payload["failure_code"] is None

    row = _request_row(mcp_env.database_url, request_id)
    assert row.status == RequestStatus.PENDING.value
    assert _slots(row.approvals) == [
        (ApproverSlot.DEPARTMENT_MANAGER.value, SlotDecision.APPROVE.value),
        (ApproverSlot.IT_ADMIN.value, None),
    ]
    assert _grant_count(mcp_env.database_url, manager.tenant_id) == grants_before
    events_after = _audit_event_types(mcp_env.database_url, manager.tenant_id)
    assert events_after == [*events_before, REQUEST_APPROVED_EVENT, SUCCEEDED_EVENT]


def test_approving_the_last_open_slot_grants_once_with_the_fresh_expiry(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Criterion 6. Catches an approval that completes the chain and never
    executes (the request would sit `approved` with no access), one that
    executes twice, and an expiry taken from the request rather than from a
    FRESH classification: the role's own default is 365 days and the request
    asked for 365, but the request classifies `critical`, whose cap is 30.

    The audit delta is the exact ordered list, which is decision I: the
    pipeline appends the tool's declared event AFTER the body, so
    `grant.executed` lands before `request.approved`. A handler appending
    `request.approved` itself would produce it twice, and a count would not
    notice.
    """
    manager = _seeded(PRODUCTION_MANAGER)
    assert ROLE_DEFAULT_EXPIRY_DAYS != CRITICAL_CLASS_CAP_DAYS
    request_id = _plant_request(
        mcp_env.database_url,
        requester_user_id=REQUESTER.id,
        target_user_id=COMPLETE_TARGET.id,
        role_id=CAPPED_ROLE,
        approvals=[_seat(ApproverSlot.DEPARTMENT_MANAGER, manager.user_id)],
    )
    events_before = _audit_event_types(mcp_env.database_url, manager.tenant_id)
    grants_before = _grant_count(mcp_env.database_url, manager.tenant_id)

    payload = _structured(
        call_tool(
            bearer(token_for(manager)),
            APPROVE,
            {"request_id": str(request_id), "idempotency_key": _key()},
        )
    )

    assert payload["chain_complete"] is True
    assert payload["status"] == RequestStatus.APPROVED.value
    assert payload["failure_code"] is None
    assert payload["grant_id"] is not None
    assert payload["expires_at"] is not None

    assert _grant_count(mcp_env.database_url, manager.tenant_id) == grants_before + 1
    grants = _grants_for_request(mcp_env.database_url, request_id)
    assert len(grants) == 1, "exactly one grant must point at this request"
    grant = grants[0]
    assert uuid.UUID(str(grant.id)) == uuid.UUID(str(payload["grant_id"]))
    assert uuid.UUID(str(grant.user_id)) == COMPLETE_TARGET.id
    assert uuid.UUID(str(grant.role_id)) == CAPPED_ROLE
    assert grant.status == "active"
    assert grant.expires_at - grant.granted_at == timedelta(days=CRITICAL_CLASS_CAP_DAYS)

    row = _request_row(mcp_env.database_url, request_id)
    assert row.status == RequestStatus.APPROVED.value
    assert _slots(row.approvals) == [
        (ApproverSlot.DEPARTMENT_MANAGER.value, SlotDecision.APPROVE.value)
    ]
    events_after = _audit_event_types(mcp_env.database_url, manager.tenant_id)
    assert events_after == [
        *events_before,
        REQUEST_APPROVED_EVENT,
        GRANT_EXECUTED_EVENT,
        SUCCEEDED_EVENT,
    ]


def test_a_conflict_planted_while_the_request_waits_fails_the_row_without_a_grant(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Criterion 9 and decision E, the design's "SoD at execution" row.

    Catches an approve path that trusts the classification stored on the
    request instead of re-classifying at execution (the conflicting grant is
    written AFTER the row is planted, so it exists only in the state a
    re-check would see), and catches decision E being implemented as B: the
    call must SUCCEED carrying `SOD_CONFLICT` with the row marked `failed`,
    because the approval really happened and the audit trail must keep it.
    Raising instead would roll the row back to `pending` and lose it.
    """
    manager = _seeded(PRODUCTION_MANAGER)
    request_id = _plant_request(
        mcp_env.database_url,
        requester_user_id=REQUESTER.id,
        target_user_id=SOD_TARGET.id,
        role_id=SOD_REQUEST_ROLE,
        approvals=[_seat(ApproverSlot.DEPARTMENT_MANAGER, manager.user_id)],
    )
    _insert_grant(mcp_env.database_url, user_id=SOD_TARGET.id, role_id=SOD_CONFLICTING_ROLE)
    grants_before = _grant_count(mcp_env.database_url, manager.tenant_id)
    events_before = _audit_event_types(mcp_env.database_url, manager.tenant_id)

    payload = _structured(
        call_tool(
            bearer(token_for(manager)),
            APPROVE,
            {"request_id": str(request_id), "idempotency_key": _key()},
        )
    )

    assert payload["failure_code"] == "SOD_CONFLICT"
    assert payload["grant_id"] is None
    assert payload["expires_at"] is None
    assert payload["status"] == RequestStatus.FAILED.value

    assert _grant_count(mcp_env.database_url, manager.tenant_id) == grants_before
    assert _grants_for_request(mcp_env.database_url, request_id) == []
    row = _request_row(mcp_env.database_url, request_id)
    assert row.status == RequestStatus.FAILED.value
    assert _slots(row.approvals) == [
        (ApproverSlot.DEPARTMENT_MANAGER.value, SlotDecision.APPROVE.value)
    ], "the approval that happened must stay in the record (decision E)"
    events_after = _audit_event_types(mcp_env.database_url, manager.tenant_id)
    assert events_after == [*events_before, REQUEST_APPROVED_EVENT, SUCCEEDED_EVENT], (
        "a completing approval that FAILED execution differs from one that granted "
        "by the absence of grant.executed after it, and by nothing else"
    )


def test_a_replay_of_a_failed_approval_returns_the_stored_response(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """An earlier change §5's last risk, pinned so it cannot later read as a bug.

    Decision E stores a `failed` outcome against the idempotency key, so
    every retry of that key replays `failed` rather than re-attempting the
    decision. Catches a replay rebuilt rather than replayed (the bytes
    differ), a retry that re-runs the body — which would re-classify, and a
    conflict cleared in the meantime would grant access nobody approved
    twice — and a replay that re-emits the domain events.
    """
    manager = _seeded(PRODUCTION_MANAGER)
    request_id = _plant_request(
        mcp_env.database_url,
        requester_user_id=REQUESTER.id,
        target_user_id=SOD_TARGET.id,
        role_id=SOD_REQUEST_ROLE,
        approvals=[_seat(ApproverSlot.DEPARTMENT_MANAGER, manager.user_id)],
    )
    _insert_grant(mcp_env.database_url, user_id=SOD_TARGET.id, role_id=SOD_CONFLICTING_ROLE)
    arguments = {"request_id": str(request_id), "idempotency_key": _key()}
    grants_before = _grant_count(mcp_env.database_url, manager.tenant_id)

    first = call_tool(bearer(token_for(manager)), APPROVE, arguments)
    events_between = _audit_event_types(mcp_env.database_url, manager.tenant_id)
    second = call_tool(bearer(token_for(manager)), APPROVE, arguments)

    assert _structured(first)["failure_code"] == "SOD_CONFLICT"
    assert _response_text(second) == _response_text(first)
    assert second.structured_content == first.structured_content
    assert _grant_count(mcp_env.database_url, manager.tenant_id) == grants_before
    events_after = _audit_event_types(mcp_env.database_url, manager.tenant_id)
    assert events_after == [*events_between, REPLAYED_EVENT]


def test_a_caller_matching_no_open_slot_is_refused_and_writes_nothing(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Criterion 4's refusal. Catches a handler that records whoever calls
    against the first open seat: the caller's matrix cell allows the tool
    (`platform_admin`, scope `any`) and the row is pending in their tenant,
    so only the seat rule can refuse — the open seat names a different
    person and the caller holds no group principal it stands for.

    `tool.denied` rather than `tool.failed`: a permission decision filed as
    an outage is a false record.
    """
    admin = _seeded(PLATFORM_ADMIN)
    assert "platform_admin" in admin.principals, "premise: the seeded IT index 18 is the admin"
    request_id = _plant_request(
        mcp_env.database_url,
        requester_user_id=REQUESTER.id,
        target_user_id=NO_SEAT_TARGET.id,
        role_id=GRANTABLE_ROLE,
        approvals=[_seat(ApproverSlot.DEPARTMENT_MANAGER, PRODUCTION_MANAGER.id)],
    )
    events_before = _audit_event_types(mcp_env.database_url, admin.tenant_id)
    grants_before = _grant_count(mcp_env.database_url, admin.tenant_id)

    result = call_tool(
        bearer(token_for(admin)),
        APPROVE,
        {"request_id": str(request_id), "idempotency_key": _key()},
    )

    assert error_code(result) == "FORBIDDEN"
    row = _request_row(mcp_env.database_url, request_id)
    assert row.status == RequestStatus.PENDING.value
    assert _slots(row.approvals) == [(ApproverSlot.DEPARTMENT_MANAGER.value, None)]
    assert _grant_count(mcp_env.database_url, admin.tenant_id) == grants_before
    events_after = _audit_event_types(mcp_env.database_url, admin.tenant_id)
    assert events_after == [*events_before, DENIED_EVENT]


@pytest.mark.parametrize("party", ["requester", "target"])
def test_a_party_to_the_request_is_refused_even_holding_the_seats_principal(
    mcp_env: SeededMcp,  # noqa: F811
    party: str,
) -> None:
    """Criterion 5, and an earlier change's owed marker discharged end to end: the
    engine cannot see whether the requester is an `it_admin`, so if this
    handler does not exclude the parties, an it_admin files a request for
    themselves and signs their own `critical` chain.

    The caller really holds `it_admin`, the open seat is the `it_admin` group
    seat, the row is pending and the caller's matrix cell allows the tool —
    every other guard admits this call, so only the party rule can refuse it.
    Both parties get their own case because an exclusion that checks the
    requester and forgets the target passes either one alone.
    """
    admin = _seeded(IT_ADMIN_ONE)
    other = _seeded(IT_ADMIN_TWO)
    assert "it_admin" in admin.principals, "premise: the caller holds the seat's principal"
    requester_id = admin.user_id if party == "requester" else other.user_id
    target_id = admin.user_id if party == "target" else NO_SEAT_TARGET.id
    request_id = _plant_request(
        mcp_env.database_url,
        requester_user_id=requester_id,
        target_user_id=target_id,
        role_id=GRANTABLE_ROLE,
        approvals=[_seat(ApproverSlot.IT_ADMIN)],
    )
    events_before = _audit_event_types(mcp_env.database_url, admin.tenant_id)
    grants_before = _grant_count(mcp_env.database_url, admin.tenant_id)

    result = call_tool(
        bearer(token_for(admin)),
        APPROVE,
        {"request_id": str(request_id), "idempotency_key": _key()},
    )

    assert error_code(result) == "FORBIDDEN"
    row = _request_row(mcp_env.database_url, request_id)
    assert row.status == RequestStatus.PENDING.value
    assert _slots(row.approvals) == [(ApproverSlot.IT_ADMIN.value, None)]
    assert _grant_count(mcp_env.database_url, admin.tenant_id) == grants_before
    events_after = _audit_event_types(mcp_env.database_url, admin.tenant_id)
    assert events_after == [*events_before, DENIED_EVENT]


def test_a_non_party_it_admin_does_fill_the_group_seat(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """The allow direction the two party cases need: catches a party rule so
    broad that nobody can ever fill a group seat, which would make every
    `critical` prod-infra chain unapprovable while both refusal cases above
    still pass."""
    admin = _seeded(IT_ADMIN_ONE)
    request_id = _plant_request(
        mcp_env.database_url,
        requester_user_id=REQUESTER.id,
        target_user_id=NO_SEAT_TARGET.id,
        role_id=GRANTABLE_ROLE,
        approvals=[_seat(ApproverSlot.IT_ADMIN), _seat(ApproverSlot.SYSTEM_OWNER, REQUESTER.id)],
    )

    payload = _structured(
        call_tool(
            bearer(token_for(admin)),
            APPROVE,
            {"request_id": str(request_id), "idempotency_key": _key()},
        )
    )

    assert payload["slot"] == ApproverSlot.IT_ADMIN.value
    assert payload["chain_complete"] is False
    row = _request_row(mcp_env.database_url, request_id)
    assert _slots(row.approvals) == [
        (ApproverSlot.IT_ADMIN.value, SlotDecision.APPROVE.value),
        (ApproverSlot.SYSTEM_OWNER.value, None),
    ]


@pytest.mark.parametrize("tool_name", [APPROVE, REJECT])
def test_another_tenants_request_and_a_missing_one_answer_the_same_way(
    mcp_env: SeededMcp,  # noqa: F811
    tool_name: str,
) -> None:
    """Criterion 11. Catches a lookup that escapes the tenant guard, and one
    that distinguishes the two absences: if another tenant's id answered
    `FORBIDDEN` (or `POLICY_BLOCKED`) while an unknown id answered
    `NOT_FOUND`, the seam would be an id oracle — an approver could probe
    which request ids are live in a tenant they cannot see."""
    manager = _seeded(PRODUCTION_MANAGER)
    theirs = _plant_request(
        mcp_env.database_url,
        requester_user_id=OTHER_TENANT_REQUESTER.id,
        target_user_id=OTHER_TENANT_TARGET.id,
        role_id=OTHER_TENANT_ROLE,
        approvals=[_seat(ApproverSlot.DEPARTMENT_MANAGER, manager.user_id)],
        tenant_id=OTHER_TENANT_ROWS.tenant_id,
    )
    grants_before = _grant_count(mcp_env.database_url, manager.tenant_id)

    cross_tenant = call_tool(
        bearer(token_for(manager)), tool_name, _decision_args(tool_name, theirs)
    )
    missing = call_tool(
        bearer(token_for(manager)), tool_name, _decision_args(tool_name, uuid.uuid4())
    )

    assert error_code(cross_tenant) == "NOT_FOUND"
    assert error_code(missing) == "NOT_FOUND"
    assert _request_row(mcp_env.database_url, theirs).status == RequestStatus.PENDING.value
    assert _grant_count(mcp_env.database_url, manager.tenant_id) == grants_before


@pytest.mark.parametrize("tool_name", [APPROVE, REJECT])
def test_a_row_that_is_not_pending_is_refused_policy_blocked(
    mcp_env: SeededMcp,  # noqa: F811
    tool_name: str,
) -> None:
    """Decision J, pinned by code. Catches `FORBIDDEN` (which would tell an
    approver who simply arrived second that they lack permission, filing a
    race as a denial in the audit log) and `NOT_FOUND` (a lie about a row the
    caller can see in their own queue).

    The seat NAMES the caller and the caller's matrix cell allows the tool,
    so only the status check can refuse this call.
    """
    manager = _seeded(PRODUCTION_MANAGER)
    request_id = _plant_request(
        mcp_env.database_url,
        requester_user_id=REQUESTER.id,
        target_user_id=NOT_PENDING_TARGET.id,
        role_id=GRANTABLE_ROLE,
        approvals=[_seat(ApproverSlot.DEPARTMENT_MANAGER, manager.user_id)],
        status=RequestStatus.APPROVED,
    )
    grants_before = _grant_count(mcp_env.database_url, manager.tenant_id)

    result = call_tool(bearer(token_for(manager)), tool_name, _decision_args(tool_name, request_id))

    assert error_code(result) == "POLICY_BLOCKED"
    assert _request_row(mcp_env.database_url, request_id).status == RequestStatus.APPROVED.value
    assert _grant_count(mcp_env.database_url, manager.tenant_id) == grants_before


def test_two_approvers_racing_the_last_slot_produce_exactly_one_grant(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Criterion 10 and decision F, the failure the design names explicitly.

    Two different approvers send two different idempotency keys, so the
    idempotency guard cannot serialise them at all; only the `SELECT … FOR
    UPDATE` on the request row can. Catches its absence: without the lock
    both calls read a `pending` row with one open group seat, both complete
    the chain, and the target is granted the same access twice.

    Both callers are seeded `it_admin`s and neither is a party, so both are
    genuinely eligible for the one open seat and the race is real.
    """
    first_admin = _seeded(IT_ADMIN_ONE)
    second_admin = _seeded(IT_ADMIN_TWO)
    assert first_admin.user_id != second_admin.user_id
    assert "it_admin" in first_admin.principals and "it_admin" in second_admin.principals
    request_id = _plant_request(
        mcp_env.database_url,
        requester_user_id=REQUESTER.id,
        target_user_id=CONCURRENCY_TARGET.id,
        role_id=GRANTABLE_ROLE,
        approvals=[_seat(ApproverSlot.IT_ADMIN)],
    )
    grants_before = _grant_count(mcp_env.database_url, first_admin.tenant_id)

    def approve(caller: SeededUser) -> Any:
        return call_tool(
            bearer(token_for(caller)),
            APPROVE,
            {"request_id": str(request_id), "idempotency_key": _key()},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(approve, first_admin), pool.submit(approve, second_admin)]
        results = [future.result() for future in futures]

    successes = [result for result in results if result.is_error is False]
    refusals = [result for result in results if result.is_error is True]
    assert len(successes) == 1, [result.content for result in results]
    assert len(refusals) == 1
    assert error_code(refusals[0]) == "POLICY_BLOCKED"
    assert _grant_count(mcp_env.database_url, first_admin.tenant_id) == grants_before + 1
    assert len(_grants_for_request(mcp_env.database_url, request_id)) == 1


# --- rejecting (criterion 8) ----------------------------------------------------


def test_rejecting_stores_the_reason_on_the_slot_and_grants_nothing(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Criterion 8. Catches a rejection that executes anyway (any grant here
    is the LLM-free mutation guarantee broken), one that loses the reason —
    the only record of WHY access was refused, which the design keeps for the
    audit — and one that writes `request.approved` for a rejection, which the
    exact audit delta catches where a count would not."""
    manager = _seeded(PRODUCTION_MANAGER)
    reason = f"fake rejection reason {uuid.uuid4().hex}"
    request_id = _plant_request(
        mcp_env.database_url,
        requester_user_id=REQUESTER.id,
        target_user_id=REJECT_TARGET.id,
        role_id=GRANTABLE_ROLE,
        approvals=[
            _seat(ApproverSlot.DEPARTMENT_MANAGER, manager.user_id),
            _seat(ApproverSlot.IT_ADMIN),
        ],
    )
    events_before = _audit_event_types(mcp_env.database_url, manager.tenant_id)
    grants_before = _grant_count(mcp_env.database_url, manager.tenant_id)

    payload = _structured(
        call_tool(
            bearer(token_for(manager)),
            REJECT,
            {"request_id": str(request_id), "reason": reason, "idempotency_key": _key()},
        )
    )

    assert payload["status"] == RequestStatus.REJECTED.value
    assert payload["slot"] == ApproverSlot.DEPARTMENT_MANAGER.value

    row = _request_row(mcp_env.database_url, request_id)
    assert row.status == RequestStatus.REJECTED.value
    assert _slots(row.approvals) == [
        (ApproverSlot.DEPARTMENT_MANAGER.value, SlotDecision.REJECT.value),
        (ApproverSlot.IT_ADMIN.value, None),
    ]
    assert row.approvals[0]["comment"] == reason
    assert row.approvals[0]["decided_by_user_id"] == str(manager.user_id)
    assert _grant_count(mcp_env.database_url, manager.tenant_id) == grants_before
    events_after = _audit_event_types(mcp_env.database_url, manager.tenant_id)
    assert events_after == [*events_before, REQUEST_REJECTED_EVENT, SUCCEEDED_EVENT]


def test_one_rejection_ends_the_chain_for_a_later_approver(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Criterion 8's second half. Catches a chain-complete predicate reading
    "no open seats" without looking at the decisions: the `it_admin` seat is
    still open after the rejection, so an approver arriving second must be
    refused typed rather than allowed to finish (and execute) a request that
    was already refused.

    The caller genuinely holds the open seat's principal and is no party, so
    the seat rule admits them; only the rejected-row check refuses.
    """
    manager = _seeded(PRODUCTION_MANAGER)
    admin = _seeded(IT_ADMIN_ONE)
    request_id = _plant_request(
        mcp_env.database_url,
        requester_user_id=REQUESTER.id,
        target_user_id=REJECT_TARGET.id,
        role_id=GRANTABLE_ROLE,
        approvals=[
            _seat(ApproverSlot.DEPARTMENT_MANAGER, manager.user_id),
            _seat(ApproverSlot.IT_ADMIN),
        ],
    )
    rejection = call_tool(
        bearer(token_for(manager)),
        REJECT,
        {"request_id": str(request_id), "reason": "fake rejection", "idempotency_key": _key()},
    )
    assert rejection.is_error is False, rejection.content
    grants_before = _grant_count(mcp_env.database_url, manager.tenant_id)

    result = call_tool(
        bearer(token_for(admin)),
        APPROVE,
        {"request_id": str(request_id), "idempotency_key": _key()},
    )

    assert error_code(result) == "POLICY_BLOCKED"
    row = _request_row(mcp_env.database_url, request_id)
    assert row.status == RequestStatus.REJECTED.value
    assert _slots(row.approvals)[1] == (ApproverSlot.IT_ADMIN.value, None)
    assert _grant_count(mcp_env.database_url, manager.tenant_id) == grants_before


# --- exact, inclusive boundaries -----------------------


@pytest.mark.parametrize(
    ("length", "accepted"),
    [
        pytest.param(COMMENT_MAX_LENGTH, True, id="comment-2000-ok"),
        pytest.param(COMMENT_MAX_LENGTH + 1, False, id="comment-2001-refused"),
    ],
)
def test_the_comment_boundary_is_inclusive_at_2000(
    mcp_env: SeededMcp,  # noqa: F811
    length: int,
    accepted: bool,
) -> None:
    """Catches the bound being absent or off by one. The comment lands in the
    unbounded `approvals` JSONB, so nothing downstream refuses an oversized
    one: without this check 2001 characters are stored, not rejected, and the
    cap exists only in the plan. The refused side asserts
    the row is untouched, so the check runs BEFORE any effect."""
    manager = _seeded(PRODUCTION_MANAGER)
    comment = "c" * length
    request_id = _plant_request(
        mcp_env.database_url,
        requester_user_id=REQUESTER.id,
        target_user_id=COMMENT_TARGET.id,
        role_id=GRANTABLE_ROLE,
        approvals=[
            _seat(ApproverSlot.DEPARTMENT_MANAGER, manager.user_id),
            _seat(ApproverSlot.IT_ADMIN),
        ],
    )
    grants_before = _grant_count(mcp_env.database_url, manager.tenant_id)

    result = call_tool(
        bearer(token_for(manager)),
        APPROVE,
        {"request_id": str(request_id), "comment": comment, "idempotency_key": _key()},
    )

    row = _request_row(mcp_env.database_url, request_id)
    if accepted:
        assert result.is_error is False, result.content
        assert row.approvals[0]["comment"] == comment
    else:
        assert error_code(result) == "VALIDATION"
        assert _slots(row.approvals)[0] == (ApproverSlot.DEPARTMENT_MANAGER.value, None)
        assert _grant_count(mcp_env.database_url, manager.tenant_id) == grants_before


@pytest.mark.parametrize(
    ("length", "accepted"),
    [
        pytest.param(1, True, id="reason-1-ok"),
        pytest.param(REASON_MAX_LENGTH, True, id="reason-2000-ok"),
        pytest.param(0, False, id="reason-empty-refused"),
        pytest.param(REASON_MAX_LENGTH + 1, False, id="reason-2001-refused"),
    ],
)
def test_the_reason_boundary_is_inclusive_at_1_and_2000(
    mcp_env: SeededMcp,  # noqa: F811
    length: int,
    accepted: bool,
) -> None:
    """Both ends, both directions. A rejection with an EMPTY reason is a
    refusal nobody can audit, which is why the lower bound is 1 and not 0;
    the upper bound is the same unbounded JSONB store as the comment. A
    one-sided assertion cannot tell an inclusive bound from an exclusive
    one."""
    manager = _seeded(PRODUCTION_MANAGER)
    reason = "r" * length
    request_id = _plant_request(
        mcp_env.database_url,
        requester_user_id=REQUESTER.id,
        target_user_id=REASON_TARGET.id,
        role_id=GRANTABLE_ROLE,
        approvals=[_seat(ApproverSlot.DEPARTMENT_MANAGER, manager.user_id)],
    )

    result = call_tool(
        bearer(token_for(manager)),
        REJECT,
        {"request_id": str(request_id), "reason": reason, "idempotency_key": _key()},
    )

    row = _request_row(mcp_env.database_url, request_id)
    if accepted:
        assert result.is_error is False, result.content
        assert row.status == RequestStatus.REJECTED.value
        assert row.approvals[0]["comment"] == reason
    else:
        assert error_code(result) == "VALIDATION"
        assert row.status == RequestStatus.PENDING.value
        assert _slots(row.approvals)[0] == (ApproverSlot.DEPARTMENT_MANAGER.value, None)


@pytest.mark.parametrize("tool_name", [APPROVE, REJECT])
def test_an_extra_argument_on_a_deciding_tool_is_refused_with_nothing_written(
    mcp_env: SeededMcp,  # noqa: F811
    tool_name: str,
) -> None:
    """Catches an input model that ignores extras instead of forbidding them
    (the read tools' contract, wrong for a mutation): a `user_id` in the
    arguments is a caller who believes they are deciding on somebody's
    behalf, and dropping it silently would record the token's user as the
    approver while the caller reads success as having named the colleague.

    Both users are in the SAME tenant, so the tenant guard
    cannot produce this refusal on the model's behalf.
    """
    manager = _seeded(PRODUCTION_MANAGER)
    colleague = _seeded(NOT_THE_MANAGER)
    assert manager.tenant_id == colleague.tenant_id, "the point of this test"
    request_id = _plant_request(
        mcp_env.database_url,
        requester_user_id=REQUESTER.id,
        target_user_id=EXTRA_ARG_TARGET.id,
        role_id=GRANTABLE_ROLE,
        approvals=[_seat(ApproverSlot.DEPARTMENT_MANAGER, manager.user_id)],
    )
    grants_before = _grant_count(mcp_env.database_url, manager.tenant_id)

    result = call_tool(
        bearer(token_for(manager)),
        tool_name,
        _decision_args(tool_name, request_id, user_id=str(colleague.user_id)),
    )

    assert error_code(result) == "VALIDATION"
    row = _request_row(mcp_env.database_url, request_id)
    assert row.status == RequestStatus.PENDING.value
    assert _slots(row.approvals) == [(ApproverSlot.DEPARTMENT_MANAGER.value, None)]
    assert _grant_count(mcp_env.database_url, manager.tenant_id) == grants_before


# --- the rules the first review round found missing ------------------------------


def _audit_records(database_url: str, tenant_id: uuid.UUID, event_type: str) -> list[Any]:
    """Every (subject_type, subject_id, payload) written under one event type."""
    return _rows(
        database_url,
        "SELECT subject_type, subject_id, payload FROM audit_events WHERE tenant_id = :tenant"
        " AND event_type = :event_type ORDER BY seq",
        {"tenant": tenant_id, "event_type": event_type},
    )


def test_one_person_cannot_fill_two_seats_of_the_same_chain(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches the four-eyes control collapsing onto one person.

    The caller is the department manager AND an `it_admin`, which is an
    ordinary combination, and the chain has one seat for each. Without the
    chain-level rule they fill both over two calls with two idempotency keys
    and approve the request on their own — the property the ENGINE refuses to
    build into a chain (`policy/decision.py::_is_usable_fallback`: "that would
    collapse two approvers into one person and quietly halve the control"),
    inherited here because seats are filled by whoever acts.

    Every other guard admits the second call: the matrix allows it, the row is
    still pending, the caller is no party, and they really do hold the group
    seat's principal — so only the chain rule can refuse it.
    """
    manager = _seeded(PRODUCTION_MANAGER)
    token = token_for(manager, principals=("manager", "it_admin"))
    request_id = _plant_request(
        mcp_env.database_url,
        requester_user_id=REQUESTER.id,
        target_user_id=TWO_SEAT_TARGET.id,
        role_id=GRANTABLE_ROLE,
        approvals=[
            _seat(ApproverSlot.DEPARTMENT_MANAGER, manager.user_id),
            _seat(ApproverSlot.IT_ADMIN),
        ],
    )

    first = call_tool(
        bearer(token), APPROVE, {"request_id": str(request_id), "idempotency_key": _key()}
    )
    assert _structured(first)["slot"] == ApproverSlot.DEPARTMENT_MANAGER.value
    grants_before = _grant_count(mcp_env.database_url, manager.tenant_id)

    second = call_tool(
        bearer(token), APPROVE, {"request_id": str(request_id), "idempotency_key": _key()}
    )

    assert error_code(second) == "FORBIDDEN"
    row = _request_row(mcp_env.database_url, request_id)
    assert row.status == RequestStatus.PENDING.value
    assert _slots(row.approvals) == [
        (ApproverSlot.DEPARTMENT_MANAGER.value, SlotDecision.APPROVE.value),
        (ApproverSlot.IT_ADMIN.value, None),
    ]
    assert _grant_count(mcp_env.database_url, manager.tenant_id) == grants_before


def test_a_transferred_manager_may_not_decide_a_row_outside_their_new_department(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """The seat and the design's scope are two obligations, not one.

    The seat NAMES this caller, the row is pending, they are no party and the
    matrix allows the tool, so every other guard admits the call: only the
    scope check can refuse it. Without it the queue's own scope filter is
    cosmetic — a manager who transferred yesterday still decides yesterday's
    department's requests, and needs nothing but the request id, which their
    own queue showed them before the move.

    The department is read from the DIRECTORY on both sides; the token is
    minted before the move and never reissued, so an implementation trusting
    the `department_id` claim allows the call and fails here.
    """
    manager = _seeded(PRODUCTION_MANAGER)
    token = token_for(manager)
    sales_department_id = ROWS.department_ids["Sales"]
    request_id = _plant_request(
        mcp_env.database_url,
        requester_user_id=REQUESTER.id,
        target_user_id=TRANSFER_TARGET.id,
        role_id=GRANTABLE_ROLE,
        approvals=[_seat(ApproverSlot.DEPARTMENT_MANAGER, manager.user_id)],
    )
    grants_before = _grant_count(mcp_env.database_url, manager.tenant_id)

    _set_user_department(mcp_env.database_url, manager.user_id, sales_department_id)
    try:
        result = call_tool(
            bearer(token), APPROVE, {"request_id": str(request_id), "idempotency_key": _key()}
        )
    finally:
        _set_user_department(mcp_env.database_url, manager.user_id, manager.department_id)

    assert error_code(result) == "FORBIDDEN"
    row = _request_row(mcp_env.database_url, request_id)
    assert row.status == RequestStatus.PENDING.value
    assert _slots(row.approvals) == [(ApproverSlot.DEPARTMENT_MANAGER.value, None)]
    assert _grant_count(mcp_env.database_url, manager.tenant_id) == grants_before

    allowed = call_tool(
        bearer(token), APPROVE, {"request_id": str(request_id), "idempotency_key": _key()}
    )
    assert allowed.is_error is False, (
        "back in their own department the same caller must be able to decide it, "
        "or this case would pass against a handler that refuses everyone"
    )


def test_the_decision_event_names_the_request_and_the_seat(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Decision I's whole reason. Catches the domain event being dropped in
    favour of the pipeline's declared one, whose payload is `{"tool",
    "scope"}` and whose subject is the TOOL name: the audit log would record
    that somebody called `approve_request` and never which request they
    approved or which seat they filled, so two approvals of two different
    requests would be indistinguishable rows.

    The SUBJECT is asserted as well as the payload: an auditor asking
    "everything about request X" queries `subject_id`, and an event copying
    the pipeline's shape (`subject_id = the tool name`) answers nothing for
    every approval in the tenant while the payload assertions still pass.

    `chain_complete` is asserted false here because this chain has a second
    open seat — that flag is what separates the first approval of a chain
    from the one that granted access.
    """
    manager = _seeded(PRODUCTION_MANAGER)
    request_id = _plant_request(
        mcp_env.database_url,
        requester_user_id=REQUESTER.id,
        target_user_id=EVENT_TARGET.id,
        role_id=GRANTABLE_ROLE,
        approvals=[
            _seat(ApproverSlot.DEPARTMENT_MANAGER, manager.user_id),
            _seat(ApproverSlot.IT_ADMIN),
        ],
    )
    before = len(_audit_records(mcp_env.database_url, manager.tenant_id, REQUEST_APPROVED_EVENT))

    call_tool(
        bearer(token_for(manager)),
        APPROVE,
        {"request_id": str(request_id), "idempotency_key": _key()},
    )

    records = _audit_records(mcp_env.database_url, manager.tenant_id, REQUEST_APPROVED_EVENT)
    assert len(records) == before + 1
    subject_type, subject_id, payload = records[-1]
    assert subject_type == "access_request"
    assert subject_id == str(request_id)
    assert payload["request_id"] == str(request_id)
    assert payload["target_user_id"] == str(EVENT_TARGET.id)
    assert payload["role_id"] == str(GRANTABLE_ROLE)
    assert payload["slot"] == ApproverSlot.DEPARTMENT_MANAGER.value
    assert payload["chain_complete"] is False
