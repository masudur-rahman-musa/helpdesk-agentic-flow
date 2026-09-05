"""An earlier change, sections 1 and 6: the two access read tools, through the transport.

Surface under test (written before it exists):

``mcp_server.tools.access``
    ``classify_access_request(target_user_id, role_id, requested_expiry_days?)``
    — builds an earlier change's snapshot, runs `policy.engine.classify`, returns the
    full `Classification` (risk_class, decision, reasons, required_approvers,
    effective_expiry_days, sod_conflicts with rule ids and `policy_ref`,
    policy_refs). Pure over DB state: no `access_requests` row is written and
    exactly one audit event, ``request.classified`` (decision D), lands per
    call. `requested_expiry_days` is typed gt=0 le=3650 (decision C). The
    requester is the TOKEN's user; extra arguments are ignored like the other
    read tools.
    ``list_my_requests(status?)`` — the token user's own requests only (hard
    rule 6), event type ``tool.succeeded``. The status filter is exact
    `RequestStatus` membership; anything else is VALIDATION.

Both wire names are sent as strings on purpose: an unregistered name fails at
the registry, which is the right failure for a not-yet-implemented tool.

Fixture cost: everything rides an earlier change's session-scoped
``mcp_fixtures`` seam — one seeded throwaway database, one dedicated Redis
index flushed per test. Per-test isolation inside the shared database is a
namespace: unique justification strings, distinct seeded users, and
before/after deltas for audit and row counts. Seeded identities (users, roles,
the planted SoD conflict) are recomputed from ``scripts.seed.generate_tenant``
— deterministic uuid5 ids, never hardcoded UUIDs.

No pytest-asyncio: each case is a sync function driving one ``asyncio.run``.
"""

import asyncio
import uuid
from typing import Any

import pytest
from helpdesk.core.db import create_engine, create_session_factory, tenant_session
from helpdesk.core.repository import Repository
from helpdesk.models import AccessRequest, RequestStatus
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
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from scripts.seed import CUAN, NORDLICHT, TenantSpec, UserRow, generate_tenant

CLASSIFY = "classify_access_request"
LIST_MY_REQUESTS = "list_my_requests"

# Decision D: classify's own audit event, never the generic success one.
CLASSIFIED_EVENT = "request.classified"

# Decision C's inclusive cap on `requested_expiry_days`.
EXPIRY_DAYS_MAX = 3650

# The deterministic seed rows for the tenant the classify cases run in.
ROWS = generate_tenant(NORDLICHT)
OTHER_TENANT_ROWS = generate_tenant(CUAN)

# An active user whose grants are exactly their department template (no
# planted anomaly at Production index 2) — the clean caller/target.
CLEAN_USER = ROWS.users_by_department["Production"][2]
# The seeded planted-conflict user: Finance index 3 holds `payments.approver`
# on top of the Finance template, so requesting `vendor-master.editor`
# completes the seeded SoD pair (SoD-STD §2.2).
CONFLICT_USER = ROWS.users_by_department["Finance"][3]
CONFLICT_REQUEST_ROLE = ROWS.role_ids[("vendor-master", "editor")]
CONFLICT_HELD_ROLE = ROWS.role_ids[("payments", "approver")]
# Flipped to leaver and back inside one test; nothing else uses this user.
LEAVER_USER = ROWS.users_by_department["Sales"][4]

# Low sensitivity, read privilege, in every department template that lists it:
# the auto case.
LOW_ROLE = ROWS.role_ids[("wiki", "viewer")]


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


def _structured(result: Any) -> dict[str, Any]:
    assert result.is_error is False, result.content
    assert result.structured_content is not None, "a successful call carries structured content"
    content: dict[str, Any] = result.structured_content
    return content


def _classify_args(target: uuid.UUID, role: uuid.UUID, **extra: Any) -> dict[str, Any]:
    return {"target_user_id": str(target), "role_id": str(role), **extra}


# --- raw-SQL probes (reading around the tools, never through them) --------------


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


def _request_row_count(database_url: str, tenant_id: uuid.UUID) -> int:
    rows = _rows(
        database_url,
        "SELECT count(*) FROM access_requests WHERE tenant_id = :tenant",
        {"tenant": tenant_id},
    )
    return int(rows[0][0])


def _set_user_status(database_url: str, user_id: uuid.UUID, status: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text("UPDATE users SET status = :status WHERE id = :id"),
                    {"status": status, "id": user_id},
                )
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def _seeded_sod_rule(
    database_url: str, role_a: uuid.UUID, role_b: uuid.UUID
) -> tuple[uuid.UUID, str]:
    """The seeded rule row pairing these two roles (either order): id + ref."""
    rows = _rows(
        database_url,
        "SELECT id, policy_ref FROM sod_rules WHERE tenant_id = :tenant"
        " AND ((role_a_id = :a AND role_b_id = :b) OR (role_a_id = :b AND role_b_id = :a))",
        {"tenant": ROWS.tenant_id, "a": role_a, "b": role_b},
    )
    assert len(rows) == 1, "fixture premise: exactly one seeded rule pairs these roles"
    return uuid.UUID(str(rows[0][0])), str(rows[0][1])


def _insert_request(
    database_url: str,
    caller: SeededUser,
    *,
    justification: str,
    status: RequestStatus,
    role_id: uuid.UUID,
    requested_expiry_days: int | None = None,
) -> uuid.UUID:
    """One `access_requests` row through the tenant-scoped repository."""

    async def scenario() -> uuid.UUID:
        engine = create_engine(database_url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, caller.tenant_id) as session:
                row = await Repository(session, AccessRequest).create(
                    requester_user_id=caller.user_id,
                    target_user_id=caller.user_id,
                    role_id=role_id,
                    justification=justification,
                    requested_expiry_days=requested_expiry_days,
                    risk_class="low",
                    decision="auto",
                    decision_reasons=[],
                    citations=[],
                    status=status.value,
                    approver_user_ids=[],
                    approvals=[],
                    session_id=None,
                    trace_id=None,
                    idempotency_key=None,
                )
                return row.id
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _role_in_callers_tenant(caller: SeededUser) -> uuid.UUID:
    """A real role id in the caller's own tenant (both tenants seed wiki.viewer)."""
    spec_by_slug: dict[str, TenantSpec] = {NORDLICHT.slug: NORDLICHT, CUAN.slug: CUAN}
    rows = generate_tenant(spec_by_slug[caller.tenant_slug])
    assert rows.tenant_id == caller.tenant_id
    return rows.role_ids[("wiki", "viewer")]


# --- classify: decisions -------------------------------------------------------


def test_classify_in_template_low_role_is_auto_with_one_event_and_no_row(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches a handler that persists an `access_requests` row (classify is
    read-only), writes any event other than exactly one `request.classified`
    (decision D), or feeds the engine a snapshot with the wrong template
    membership — the reasons would read NOT_IN_TEMPLATE and the decision
    would not be `auto`."""
    caller = _seeded(CLEAN_USER)
    events_before = _audit_event_types(mcp_env.database_url, caller.tenant_id)
    requests_before = _request_row_count(mcp_env.database_url, caller.tenant_id)

    result = call_tool(
        bearer(token_for(caller)), CLASSIFY, _classify_args(caller.user_id, LOW_ROLE)
    )

    payload = _structured(result)
    assert payload["risk_class"] == "low"
    assert payload["decision"] == "auto"
    assert "IN_TEMPLATE" in payload["reasons"]
    assert payload["required_approvers"] == []
    assert payload["sod_conflicts"] == []
    # wiki.viewer is a seeded read role: default_expiry_days 365, uncapped.
    assert payload["effective_expiry_days"] == 365

    events_after = _audit_event_types(mcp_env.database_url, caller.tenant_id)
    assert events_after == [*events_before, CLASSIFIED_EVENT]
    assert _request_row_count(mcp_env.database_url, caller.tenant_id) == requests_before


def test_classify_sod_conflict_is_blocked_citing_the_seeded_rule(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches a classify path that loses the SoD evidence on the way out:
    a conflict without the seeded rule's id, without its `policy_ref`, or
    without the `SOD_CONFLICT:{rule_id}` reason is a block nobody can audit
    (the design). Also catches a conflict path that files a row or logs a
    second event."""
    held = {ROWS.role_ids[grant.role] for grant in ROWS.grants if grant.user_id == CONFLICT_USER.id}
    assert CONFLICT_HELD_ROLE in held, "fixture premise: the planted conflict grant exists"
    rule_id, policy_ref = _seeded_sod_rule(
        mcp_env.database_url, CONFLICT_REQUEST_ROLE, CONFLICT_HELD_ROLE
    )
    caller = _seeded(CONFLICT_USER)
    events_before = _audit_event_types(mcp_env.database_url, caller.tenant_id)
    requests_before = _request_row_count(mcp_env.database_url, caller.tenant_id)

    result = call_tool(
        bearer(token_for(caller)),
        CLASSIFY,
        _classify_args(caller.user_id, CONFLICT_REQUEST_ROLE),
    )

    payload = _structured(result)
    assert payload["decision"] == "blocked"
    assert len(payload["sod_conflicts"]) == 1, payload["sod_conflicts"]
    conflict = payload["sod_conflicts"][0]
    assert conflict["rule_id"] == str(rule_id)
    assert conflict["policy_ref"] == policy_ref
    assert conflict["conflicting_role_id"] == str(CONFLICT_HELD_ROLE)
    assert conflict["source"] == "active_grant"
    assert f"SOD_CONFLICT:{rule_id}" in payload["reasons"]

    events_after = _audit_event_types(mcp_env.database_url, caller.tenant_id)
    assert events_after == [*events_before, CLASSIFIED_EVENT]
    assert _request_row_count(mcp_env.database_url, caller.tenant_id) == requests_before


def test_classify_a_leaver_target_is_blocked(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches a snapshot that reads the target's status as a constant
    `active` (or off the caller's row instead of the target's): the target
    is a leaver and the role is in their template with no SoD conflict, so
    only the leaver control can produce this block."""
    caller = _seeded(CLEAN_USER)
    target = LEAVER_USER
    events_before = _audit_event_types(mcp_env.database_url, caller.tenant_id)
    requests_before = _request_row_count(mcp_env.database_url, caller.tenant_id)

    _set_user_status(mcp_env.database_url, target.id, "leaver")
    try:
        result = call_tool(bearer(token_for(caller)), CLASSIFY, _classify_args(target.id, LOW_ROLE))
    finally:
        _set_user_status(mcp_env.database_url, target.id, "active")

    payload = _structured(result)
    assert payload["decision"] == "blocked"
    assert "TARGET_LEAVER" in payload["reasons"]
    assert payload["sod_conflicts"] == [], "the block must come from the leaver control"

    events_after = _audit_event_types(mcp_env.database_url, caller.tenant_id)
    assert events_after == [*events_before, CLASSIFIED_EVENT]
    assert _request_row_count(mcp_env.database_url, caller.tenant_id) == requests_before


# --- classify: exact boundaries and typed refusals -----------------------------


@pytest.mark.parametrize(
    ("days", "accepted"),
    [
        pytest.param(EXPIRY_DAYS_MAX, True, id="3650-ok"),
        pytest.param(EXPIRY_DAYS_MAX + 1, False, id="3651-refused"),
        pytest.param(0, False, id="0-refused"),
    ],
)
def test_the_requested_expiry_boundary_is_inclusive_at_3650(
    mcp_env: SeededMcp,  # noqa: F811
    days: int,
    accepted: bool,
) -> None:
    """Catches decision C's cap missing, off by one, or exclusive where it
    must be inclusive: 3650 lands in the int4 column fine and must pass,
    3651 and 0 must be refused VALIDATION before any effect (standing
    rules 1 and 2)."""
    caller = _seeded(CLEAN_USER)
    requests_before = _request_row_count(mcp_env.database_url, caller.tenant_id)

    result = call_tool(
        bearer(token_for(caller)),
        CLASSIFY,
        _classify_args(caller.user_id, LOW_ROLE, requested_expiry_days=days),
    )

    if accepted:
        payload = _structured(result)
        assert payload["decision"] in {"auto", "approval", "blocked"}
    else:
        assert error_code(result) == "VALIDATION"
    assert _request_row_count(mcp_env.database_url, caller.tenant_id) == requests_before


def test_a_non_uuid_role_id_is_refused_validation(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches a role_id typed as a bare string: a non-UUID must be VALIDATION
    at the input model, never a database cast error."""
    caller = _seeded(CLEAN_USER)

    result = call_tool(
        bearer(token_for(caller)),
        CLASSIFY,
        {"target_user_id": str(caller.user_id), "role_id": "not-a-uuid"},
    )

    assert error_code(result) == "VALIDATION"


def test_an_unknown_role_is_not_found(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches a snapshot builder that surfaces a missing role as INTERNAL
    (or a half-built snapshot) instead of the typed NOT_FOUND."""
    caller = _seeded(CLEAN_USER)

    result = call_tool(
        bearer(token_for(caller)), CLASSIFY, _classify_args(caller.user_id, uuid.uuid4())
    )

    assert error_code(result) == "NOT_FOUND"


def test_a_real_target_in_another_tenant_is_not_found(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches a target lookup that escapes the tenant guard: the target id
    names a REAL seeded user of the other tenant, so an unguarded read would
    find the row and classify instead of answering NOT_FOUND."""
    caller = _seeded(CLEAN_USER)
    foreign_user = OTHER_TENANT_ROWS.users[0]
    assert OTHER_TENANT_ROWS.tenant_id != caller.tenant_id

    result = call_tool(
        bearer(token_for(caller)), CLASSIFY, _classify_args(foreign_user.id, LOW_ROLE)
    )

    assert error_code(result) == "NOT_FOUND"


# --- list_my_requests ----------------------------------------------------------


def test_list_my_requests_returns_only_the_token_users_rows(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches an implementation that filters by an argument instead of the
    token, or not at all (every tenant row would come back).

    The caller and the other requester are in the SAME tenant (standing
    rule 9), so an implementation that honoured the `user_id` argument in
    the second call — or leaked tenant-wide rows in the first — returns the
    colleague's row rather than being saved by the tenant guard.
    """
    caller = mcp_env.tenant_a
    colleague = mcp_env.probe
    assert caller.tenant_id == colleague.tenant_id, "the point of this test"
    assert caller.user_id != colleague.user_id
    role_id = _role_in_callers_tenant(caller)
    own_justification = f"fake own request {uuid.uuid4().hex}"
    other_justification = f"fake colleague request {uuid.uuid4().hex}"
    own_id = _insert_request(
        mcp_env.database_url,
        caller,
        justification=own_justification,
        status=RequestStatus.PENDING,
        role_id=role_id,
        requested_expiry_days=14,
    )
    _insert_request(
        mcp_env.database_url,
        colleague,
        justification=other_justification,
        status=RequestStatus.PENDING,
        role_id=role_id,
    )

    payload = _structured(call_tool(bearer(token_for(caller)), LIST_MY_REQUESTS, {}))

    justifications = {item["justification"] for item in payload["requests"]}
    assert own_justification in justifications
    assert other_justification not in justifications

    own_rows = [item for item in payload["requests"] if item["justification"] == own_justification]
    assert len(own_rows) == 1
    row = own_rows[0]
    assert row["request_id"] == str(own_id)
    assert row["target_user_id"] == str(caller.user_id)
    assert row["role_id"] == str(role_id)
    assert row["requested_expiry_days"] == 14
    assert row["risk_class"] == "low"
    assert row["decision"] == "auto"
    assert row["status"] == "pending"
    assert row["created_at"] is not None

    # Naming the colleague in the arguments changes nothing: identity comes
    # from the token, extra arguments are dropped like the other read tools.
    named = _structured(
        call_tool(
            bearer(token_for(caller)),
            LIST_MY_REQUESTS,
            {"user_id": str(colleague.user_id)},
        )
    )
    named_justifications = {item["justification"] for item in named["requests"]}
    assert own_justification in named_justifications
    assert other_justification not in named_justifications


@pytest.mark.parametrize("status", list(RequestStatus), ids=[s.value for s in RequestStatus])
def test_the_status_filter_returns_exactly_that_status(
    mcp_env: SeededMcp,  # noqa: F811
    status: RequestStatus,
) -> None:
    """Catches a status filter that is ignored (the decoy row with another
    status would come back) or inverted, for every the design status value."""
    caller = mcp_env.probe
    decoy_status = (
        RequestStatus.APPROVED if status is RequestStatus.PENDING else RequestStatus.PENDING
    )
    prefix = f"fake status probe {uuid.uuid4().hex}"
    role_id = _role_in_callers_tenant(caller)
    _insert_request(
        mcp_env.database_url,
        caller,
        justification=f"{prefix} match",
        status=status,
        role_id=role_id,
    )
    _insert_request(
        mcp_env.database_url,
        caller,
        justification=f"{prefix} decoy",
        status=decoy_status,
        role_id=role_id,
    )

    payload = _structured(
        call_tool(bearer(token_for(caller)), LIST_MY_REQUESTS, {"status": status.value})
    )

    mine = {
        item["justification"]
        for item in payload["requests"]
        if str(item["justification"]).startswith(prefix)
    }
    assert mine == {f"{prefix} match"}
    for item in payload["requests"]:
        assert item["status"] == status.value


@pytest.mark.parametrize(
    "status",
    ["pending ", "pending\n", "nonsense"],
    ids=["trailing-space", "trailing-newline", "unknown"],
)
def test_a_status_outside_the_enum_is_refused_validation(
    mcp_env: SeededMcp,  # noqa: F811
    status: str,
) -> None:
    """Catches a status check weaker than exact enum membership: a stripped
    compare or a prefix match admits the whitespace rows (a project rule's
    shape), and free text must never reach the query."""
    result = call_tool(bearer(token_for(mcp_env.probe)), LIST_MY_REQUESTS, {"status": status})

    assert error_code(result) == "VALIDATION"


def test_a_user_with_no_requests_gets_an_empty_list(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches an empty result reported as an error or a missing key: no rows
    is a valid answer and must serialise as an empty list."""
    payload = _structured(call_tool(bearer(token_for(mcp_env.tenant_b)), LIST_MY_REQUESTS, {}))

    assert payload["requests"] == []
