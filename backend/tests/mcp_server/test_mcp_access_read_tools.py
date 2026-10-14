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
from datetime import UTC, datetime
from typing import Any

import pytest
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
from scripts.seed import CUAN, NORDLICHT, TenantSpec, UserRow, generate_tenant
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from helpdesk.mcp_server.tools import access
from helpdesk.shared.core.db import create_engine, create_session_factory, tenant_session
from helpdesk.shared.core.repository import Repository
from helpdesk.shared.models import AccessRequest, Grant, RequestStatus

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


# --- an earlier change: the widened row, its grant join and its limit -------------------
#
# Surface under test (written before it exists; every case below is red on one
# of these names until step 1 lands):
#
# ``mcp_server.tools.access``
#     ``REQUESTS_LIMIT_MIN = 1``, ``REQUESTS_LIMIT_DEFAULT = 50``,
#     ``REQUESTS_LIMIT_MAX = 200`` — 23a §1's three constants, the trio
#     ``tools/read.py`` already carries for ``search_audit``.
#     ``ListMyRequestsInput.limit`` — ``ge=REQUESTS_LIMIT_MIN``,
#     ``le=REQUESTS_LIMIT_MAX``, defaulting to ``REQUESTS_LIMIT_DEFAULT``.
#     ``AccessRequestSummary`` — the nine fields it carries today plus
#     ``role_slug``, ``role_name``, ``system_slug``, ``system_name``,
#     ``target_display_name``, ``decision_reasons``, ``citations``,
#     ``approvals``, ``grant_id`` and ``expires_at``: NINETEEN, asserted as an
#     exact set.
#
# Shapes this file fixes that the plan left open (a project rule and 55;
# these belong in 23a's Deviations):
# - ``approvals`` renders each stored ``SlotState`` as an OBJECT carrying at
#   least ``slot`` and ``user_id``; decision D is slot-only, so no display name
#   is resolved. The case below reads those two keys and nothing else, because
#   whether ``SlotState``'s four decision fields are dumped as explicit nulls
#   is the implementer's call and pinning invented keys would be a test only
#   this file could satisfy.
# - ``decision_reasons`` and ``citations`` come back as the JSONB columns hold
#   them, unchanged: a list of strings and a list of objects.
# - ``grant_id`` is a string uuid and ``expires_at`` an ISO instant, the way
#   every other uuid and datetime on this wire already is.
#
# Isolation is the existing namespace: the session-scoped
# ``mcp_env`` schema, planted rows carrying a per-case ``uuid4`` prefix in
# their justification, and no new fixture of any kind.

# The nineteen fields of §1, written out rather than derived: a set built by
# comprehension over the model would agree with any implementation.
EXPECTED_SUMMARY_FIELDS = frozenset(
    {
        "request_id",
        "target_user_id",
        "role_id",
        "justification",
        "requested_expiry_days",
        "risk_class",
        "decision",
        "status",
        "created_at",
        "role_slug",
        "role_name",
        "system_slug",
        "system_name",
        "target_display_name",
        "decision_reasons",
        "citations",
        "approvals",
        "grant_id",
        "expires_at",
    }
)

EXPECTED_REQUESTS_LIMIT_MIN = 1
EXPECTED_REQUESTS_LIMIT_DEFAULT = 50
EXPECTED_REQUESTS_LIMIT_MAX = 200

# Fixed instants, never offsets from "now": a grant whose expiry depends on
# when the suite runs is a flaky row, not a fixture (mcp_fixtures' own rule).
PLANTED_GRANTED_AT = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
PLANTED_EXPIRES_AT = datetime(2099, 3, 1, 12, 0, tzinfo=UTC)

PLANTED_REASONS = ["IN_TEMPLATE", "EXPIRY_CAPPED"]
PLANTED_CITATION = {
    "document_slug": "fake-access-policy",
    "title": "Fake Access Policy",
    "version": "v1",
    "heading_path": "Fake Section",
    "chunk_id": "fake-chunk-0001",
}

PLANTED_SLOT = "department_manager"


def _insert_wide_request(
    database_url: str,
    caller: SeededUser,
    *,
    justification: str,
    status: RequestStatus,
    role_id: uuid.UUID,
    target_user_id: uuid.UUID,
    approvals: list[dict[str, Any]],
    requested_expiry_days: int | None = None,
) -> uuid.UUID:
    """One `access_requests` row with every column the widened row renders.

    A second helper rather than more keyword arguments on `_insert_request`:
    the cases above own that one, and a default changed there would change
    what they plant.
    """

    async def scenario() -> uuid.UUID:
        engine = create_engine(database_url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, caller.tenant_id) as session:
                row = await Repository(session, AccessRequest).create(
                    requester_user_id=caller.user_id,
                    target_user_id=target_user_id,
                    role_id=role_id,
                    justification=justification,
                    requested_expiry_days=requested_expiry_days,
                    risk_class="elevated",
                    decision="approval",
                    decision_reasons=list(PLANTED_REASONS),
                    citations=[dict(PLANTED_CITATION)],
                    status=status.value,
                    approver_user_ids=[],
                    approvals=approvals,
                    session_id=None,
                    trace_id=None,
                    idempotency_key=None,
                )
                return row.id
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _insert_grant_for_request(
    database_url: str,
    caller: SeededUser,
    *,
    user_id: uuid.UUID,
    role_id: uuid.UUID,
    request_id: uuid.UUID,
    expires_at: datetime | None,
) -> uuid.UUID:
    """One live `grants` row pointing back at the request it came from."""

    async def scenario() -> uuid.UUID:
        engine = create_engine(database_url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, caller.tenant_id) as session:
                row = await Repository(session, Grant).create(
                    user_id=user_id,
                    role_id=role_id,
                    request_id=request_id,
                    granted_by_user_id=caller.user_id,
                    granted_at=PLANTED_GRANTED_AT,
                    expires_at=expires_at,
                    revoked_at=None,
                    revoke_reason=None,
                    status="active",
                )
                return row.id
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _directory_names(database_url: str, role_id: uuid.UUID) -> tuple[str, str, str, str]:
    """`(role_slug, role_name, system_slug, system_name)` off the directory rows."""
    rows = _rows(
        database_url,
        "SELECT r.slug, r.name, s.slug, s.name FROM roles r"
        " JOIN systems s ON s.id = r.system_id WHERE r.id = :role",
        {"role": role_id},
    )
    assert len(rows) == 1, "fixture premise: the planted request names a real role"
    return (str(rows[0][0]), str(rows[0][1]), str(rows[0][2]), str(rows[0][3]))


def _display_name(database_url: str, user_id: uuid.UUID) -> str:
    rows = _rows(database_url, "SELECT display_name FROM users WHERE id = :id", {"id": user_id})
    assert len(rows) == 1, "fixture premise: the planted request names a real user"
    return str(rows[0][0])


def _row_for(payload: dict[str, Any], justification: str) -> dict[str, Any]:
    matches = [item for item in payload["requests"] if item["justification"] == justification]
    assert len(matches) == 1, f"expected one row for {justification!r}, got {len(matches)}"
    row: dict[str, Any] = matches[0]
    return row


def test_the_widened_row_carries_exactly_the_nineteen_planned_fields(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches a widening that drops one of §1's ten new fields AND one that
    carries a spare (a `requester_user_id`, the raw `approver_user_ids`, a
    `system_id`): the key set is asserted as an exact set, so both directions
    fail. Per-field assertions — which every case above uses — cannot see the
    spare field, and the spare field is the one publishing a column nobody
    decided to publish."""
    caller = mcp_env.tenant_a
    justification = f"fake wide row {uuid.uuid4().hex}"
    _insert_wide_request(
        mcp_env.database_url,
        caller,
        justification=justification,
        status=RequestStatus.PENDING,
        role_id=_role_in_callers_tenant(caller),
        target_user_id=caller.user_id,
        approvals=[],
    )

    payload = _structured(
        call_tool(
            bearer(token_for(caller)), LIST_MY_REQUESTS, {"limit": EXPECTED_REQUESTS_LIMIT_MAX}
        )
    )

    assert len(EXPECTED_SUMMARY_FIELDS) == 19
    assert set(_row_for(payload, justification)) == EXPECTED_SUMMARY_FIELDS


def test_the_names_come_from_the_directory_and_not_from_the_request_row(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches the four name fields and `target_display_name` being filled
    from anything but the rows their ids point at.

    Three kills in one shape, none of them decidable by another guard:
    `role_name`/`system_name` echoed from the matching SLUG fails, because the
    premise reads both off the directory and asserts they differ;
    `target_display_name` taken from the TOKEN's user fails, because the
    planted request targets a colleague in the SAME tenant
    whose display name differs from the caller's; a row that resolves nothing
    fails on all five.

    The stored chain, reasons and citations are read back here too: they are
    columns rather than lookups, so dropping them is the other half of
    "rendered by slot" (decision D).
    """
    caller = mcp_env.tenant_a
    target = mcp_env.probe
    assert caller.tenant_id == target.tenant_id, "the point of this test (standing rule 9)"
    assert caller.display_name != target.display_name
    role_id = _role_in_callers_tenant(caller)
    role_slug, role_name, system_slug, system_name = _directory_names(mcp_env.database_url, role_id)
    assert role_name != role_slug, "fixture premise: the seeded role's name is not its slug"
    assert system_name != system_slug, "fixture premise: the seeded system's name is not its slug"
    approver_id = caller.user_id
    justification = f"fake named row {uuid.uuid4().hex}"
    _insert_wide_request(
        mcp_env.database_url,
        caller,
        justification=justification,
        status=RequestStatus.PENDING,
        role_id=role_id,
        target_user_id=target.user_id,
        approvals=[{"slot": PLANTED_SLOT, "user_id": str(approver_id)}],
    )

    payload = _structured(
        call_tool(
            bearer(token_for(caller)), LIST_MY_REQUESTS, {"limit": EXPECTED_REQUESTS_LIMIT_MAX}
        )
    )

    row = _row_for(payload, justification)
    assert row["role_slug"] == role_slug
    assert row["role_name"] == role_name
    assert row["system_slug"] == system_slug
    assert row["system_name"] == system_name
    assert row["target_display_name"] == _display_name(mcp_env.database_url, target.user_id)
    assert row["target_display_name"] != caller.display_name

    assert row["decision_reasons"] == PLANTED_REASONS
    assert row["citations"] == [PLANTED_CITATION]
    assert [slot["slot"] for slot in row["approvals"]] == [PLANTED_SLOT]
    assert row["approvals"][0]["user_id"] == str(approver_id)


def test_the_grant_join_is_null_when_pending_and_carries_the_grant_when_executed(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches `grant_id`/`expires_at` that are always null (no join at all),
    and catches `expires_at` computed from `requested_expiry_days` instead of
    read off the grant: the planted request asks for 7 days and the planted
    grant expires in 2099, which decision E is precisely about — the requested
    days are not the expiry once the engine capped them.

    The pending half is the other kill: an INNER join drops the ungranted
    request entirely, so a row missing there fails nothing else.
    """
    caller = mcp_env.tenant_a
    role_id = _role_in_callers_tenant(caller)
    prefix = f"fake grant join {uuid.uuid4().hex}"
    _insert_wide_request(
        mcp_env.database_url,
        caller,
        justification=f"{prefix} pending",
        status=RequestStatus.PENDING,
        role_id=role_id,
        target_user_id=caller.user_id,
        approvals=[],
        requested_expiry_days=7,
    )
    executed_id = _insert_wide_request(
        mcp_env.database_url,
        caller,
        justification=f"{prefix} executed",
        status=RequestStatus.EXECUTED,
        role_id=role_id,
        target_user_id=caller.user_id,
        approvals=[],
        requested_expiry_days=7,
    )
    grant_id = _insert_grant_for_request(
        mcp_env.database_url,
        caller,
        user_id=caller.user_id,
        role_id=role_id,
        request_id=executed_id,
        expires_at=PLANTED_EXPIRES_AT,
    )

    payload = _structured(
        call_tool(
            bearer(token_for(caller)), LIST_MY_REQUESTS, {"limit": EXPECTED_REQUESTS_LIMIT_MAX}
        )
    )

    pending = _row_for(payload, f"{prefix} pending")
    assert pending["grant_id"] is None
    assert pending["expires_at"] is None

    executed = _row_for(payload, f"{prefix} executed")
    assert executed["grant_id"] == str(grant_id)
    assert executed["expires_at"] is not None
    assert datetime.fromisoformat(str(executed["expires_at"])) == PLANTED_EXPIRES_AT


def test_several_requests_come_back_once_each_despite_the_grant_join(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches a join that multiplies rows (§5's second risk): three requests
    are planted for one caller, one of them granted, and the answer must carry
    three rows with exactly one `grant_id` filled.

    Only this case can fail for that reason — every other case reads one row
    by its unique justification through `_row_for`, which is indifferent to how
    many other rows the join produced.
    """
    caller = mcp_env.tenant_a
    role_id = _role_in_callers_tenant(caller)
    prefix = f"fake join count {uuid.uuid4().hex}"
    planted = [
        _insert_wide_request(
            mcp_env.database_url,
            caller,
            justification=f"{prefix} {index}",
            status=RequestStatus.PENDING,
            role_id=role_id,
            target_user_id=caller.user_id,
            approvals=[],
        )
        for index in range(3)
    ]
    _insert_grant_for_request(
        mcp_env.database_url,
        caller,
        user_id=caller.user_id,
        role_id=role_id,
        request_id=planted[1],
        expires_at=PLANTED_EXPIRES_AT,
    )

    payload = _structured(
        call_tool(
            bearer(token_for(caller)), LIST_MY_REQUESTS, {"limit": EXPECTED_REQUESTS_LIMIT_MAX}
        )
    )

    mine = [
        item for item in payload["requests"] if str(item["justification"]).startswith(f"{prefix} ")
    ]
    assert len(mine) == 3
    assert {str(item["request_id"]) for item in mine} == {str(row_id) for row_id in planted}
    assert [item["grant_id"] for item in mine].count(None) == 2


def test_the_request_limit_constants_are_exactly_their_planned_values() -> None:
    """Catches a widened `limit` (an unbounded response over a table that only
    grows), a drifted default (every caller who omits `limit` silently gets a
    different page), and a lower bound of 0 (an empty page a client would read
    as "you have filed nothing"). a project rule wants the values, not a
    band."""
    assert access.REQUESTS_LIMIT_MIN == EXPECTED_REQUESTS_LIMIT_MIN == 1
    assert access.REQUESTS_LIMIT_DEFAULT == EXPECTED_REQUESTS_LIMIT_DEFAULT == 50
    assert access.REQUESTS_LIMIT_MAX == EXPECTED_REQUESTS_LIMIT_MAX == 200


def test_the_default_request_limit_is_read_off_the_input_model() -> None:
    """Catches the constant and the model disagreeing: a `Field(default=100)`
    beside a `REQUESTS_LIMIT_DEFAULT` of 50 leaves the case above green while
    every caller who omits `limit` receives a different page size than §1
    promises. Both fields stay optional, so the model constructs with no
    arguments at all — which is what pins that an unfiltered, unpaged call is
    still legal."""
    assert access.ListMyRequestsInput().limit == access.REQUESTS_LIMIT_DEFAULT


REQUEST_LIMIT_CASES: tuple[tuple[str, int, bool], ...] = (
    ("limit-0-refused", 0, False),
    ("limit-1-ok", EXPECTED_REQUESTS_LIMIT_MIN, True),
    ("limit-200-ok", EXPECTED_REQUESTS_LIMIT_MAX, True),
    ("limit-201-refused", EXPECTED_REQUESTS_LIMIT_MAX + 1, False),
)


@pytest.mark.parametrize(
    ("limit", "accepted"),
    [case[1:] for case in REQUEST_LIMIT_CASES],
    ids=[case[0] for case in REQUEST_LIMIT_CASES],
)
def test_the_request_limit_boundary_is_inclusive_at_1_and_200(
    mcp_env: SeededMcp,  # noqa: F811
    limit: int,
    accepted: bool,
) -> None:
    """§1's `limit`, both sides of both ends.

    A one-sided assertion cannot tell an inclusive bound from an exclusive
    one: `gt=1` accepts 200 and refuses 1, `lt=200` accepts 1 and refuses 200,
    so both accepted rows are needed to pin `ge`/`le`. Two rows are planted
    first, so the `limit=1` row returning exactly one of them also catches a
    bound that validates and is then never passed to the query — the failure a
    boundary table alone cannot see.

    The refusals are VALIDATION from the INPUT MODEL, before any DB access:
    nothing is planted for them to read, so a handler validating after the
    query would answer 200 here.
    """
    caller = mcp_env.tenant_a
    role_id = _role_in_callers_tenant(caller)
    prefix = f"fake limit probe {uuid.uuid4().hex}"
    for index in range(2):
        _insert_wide_request(
            mcp_env.database_url,
            caller,
            justification=f"{prefix} {index}",
            status=RequestStatus.PENDING,
            role_id=role_id,
            target_user_id=caller.user_id,
            approvals=[],
        )

    result = call_tool(bearer(token_for(caller)), LIST_MY_REQUESTS, {"limit": limit})

    if not accepted:
        assert error_code(result) == "VALIDATION"
        return
    payload = _structured(result)
    assert 1 <= len(payload["requests"]) <= limit
