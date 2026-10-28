"""`create_access_request`, through the transport.

Surface under test (written before it exists):

``mcp_server.tools.access``
    ``create_access_request(target_user_id, role_id, justification,
    requested_expiry_days?, citations[], idempotency_key)`` — a registered
    MUTATING tool (wire name ``create_access_request``, mapped to
    ``rbac.Tool.CREATE_ACCESS_REQUEST_SELF`` by ``registry.WIRE_NAME_ALIASES``).
    It classifies through the snapshot builder and
    ``policy.engine.classify``, persists exactly one ``access_requests`` row
    carrying the engine's ``risk_class``, ``decision``, ``decision_reasons``,
    ``citations`` and the caller's ``idempotency_key``, then branches:
    ``auto`` → the grant is executed in the same transaction and the row is
    ``executed``; ``approval`` → the row is ``pending`` with the resolved
    approver ids in ``approver_user_ids`` and the engine's FULL slot list
    (slot plus ``user_id | null``) in ``approvals``; ``blocked`` → the row is
    ``rejected`` with the reasons and no grant.
    Bounds: ``JUSTIFICATION_MAX_LENGTH = 2000`` (the column
    is unbounded ``Text``), ``CITATIONS_MAX = 20`` typed ``Citation`` entries,
    ``requested_expiry_days`` gt 0 le 3650 (the ``EXPIRY_DAYS_MAX``),
    ``extra="forbid"`` like every ``MutatingToolInput``.
    On-behalf: a ``target_user_id`` that is not the token's user is
    authorized against ``CREATE_ACCESS_REQUEST_ON_BEHALF`` inside the handler
    before any write; a ``manager`` (scope ``own_department``)
    may only name a target in their own department. A refusal is
    ``FORBIDDEN`` with exactly one ``tool.denied`` event and nothing written.

Audit sequences, in ``seq`` order (the pipeline appends its
own event AFTER the tool body, and that event is this tool's
``request.created``, so the domain's events precede it):

    auto     → request.auto_executed, grant.executed, request.created
    approval → request.queued, request.created
    blocked  → request.created

Each branch asserts that whole list as a delta against a before-snapshot, not
a count.

Fixture cost: everything rides the session-scoped
``mcp_fixtures`` seam — one seeded throwaway database, one dedicated Redis
index flushed per test. Per-test isolation inside the shared database is a
namespace: a unique justification string and a unique idempotency key per
call. Seeded identities (users, roles, the planted SoD conflict, the seeded
rule row) are recomputed from ``scripts.seed.generate_tenant`` — deterministic
uuid5 ids, never hardcoded UUIDs.

No pytest-asyncio: each case is a sync function driving one ``asyncio.run``.
"""

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from scripts.seed import NORDLICHT, UserRow, generate_tenant
from tests.support.mcp import (
    SeededMcp,
    SeededUser,
    bearer,
    call_tool,
    error_code,
    token_for,
)

CREATE = "create_access_request"

# The pipeline's own event for this tool (registry `event_type`) and the three
# domain events beside it.
REQUEST_CREATED_EVENT = "request.created"
REQUEST_QUEUED_EVENT = "request.queued"
REQUEST_AUTO_EXECUTED_EVENT = "request.auto_executed"
GRANT_EXECUTED_EVENT = "grant.executed"
DENIED_EVENT = "tool.denied"
REPLAYED_EVENT = "tool.replayed"

# The caps under test, at their exact inclusive boundaries.
# EXPIRY_DAYS_MAX is the constant, read from one place.
JUSTIFICATION_MAX_LENGTH = 2000
CITATIONS_MAX = 20
EXPIRY_DAYS_MAX = 3650
# The per-field cap inside the same unbounded JSONB column: `CITATIONS_MAX`
# bounds how MANY citations, this bounds how big each one may be.
CITATION_FIELD_MAX_LENGTH = 512

ROWS = generate_tenant(NORDLICHT)

# Production index 2 has no planted anomaly (its grants are exactly its
# department template), so its classifications come from the request alone.
CLEAN_USER = ROWS.users_by_department["Production"][2]
# The seeded manager of every department is index 0 (`scripts.seed`
# `_assign_principals`), so this token really carries the `manager` principal.
PRODUCTION_MANAGER = ROWS.users_by_department["Production"][0]
PRODUCTION_REPORT = ROWS.users_by_department["Production"][6]
# Same TENANT, another department: a cross-tenant target
# would be refused by the tenant guard whether the department rule holds or
# not, which would make the on-behalf test vacuous.
OTHER_DEPARTMENT_TARGET = ROWS.users_by_department["Sales"][7]
# The seeded HR officer is HR index 1 (`scripts.seed` `_assign_principals`),
# so this token really carries the `hr` principal and its `any` scope.
HR_OFFICER = ROWS.users_by_department["HR"][1]
# Finance index 3 is the seeded planted conflict: it holds `payments.approver`
# on top of the Finance template, so `vendor-master.editor` completes the
# seeded SoD pair.
CONFLICT_USER = ROWS.users_by_department["Finance"][3]

# In every department template that lists it, low sensitivity, read: the auto
# branch. `wiki` is seeded `low` and `viewer` is `read` with 365 default days.
LOW_ROLE = ROWS.role_ids[("wiki", "viewer")]
LOW_ROLE_EXPIRY_DAYS = 365
# Outside Production's template, on a `critical` `prod_infra` system: risk
# `critical`, so the chain is department_manager + system_owner + it_admin —
# and `it_admin` is a group seat the engine cannot resolve, which is what
# makes "resolved ids only in approver_user_ids" observable.
APPROVAL_ROLE = ROWS.role_ids[("prod-k8s", "deployer")]
PROD_K8S_OWNER = ROWS.system_owners["prod-k8s"]
# The other half of the seeded prod-k8s SoD pair, asserted absent as a premise.
PROD_K8S_AUDITOR_ROLE = ROWS.role_ids[("prod-k8s", "auditor")]
# Not in Finance's template, `high` sensitivity, `write`: `privileged`, and
# blocked only by the SoD conflict.
CONFLICT_REQUEST_ROLE = ROWS.role_ids[("vendor-master", "editor")]
CONFLICT_HELD_ROLE = ROWS.role_ids[("payments", "approver")]


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


def _justification() -> str:
    """A unique justification: how each case finds only its own row."""
    return f"fake access request {uuid.uuid4().hex}"


def _citation(index: int) -> dict[str, str]:
    """One obviously fake citation of the typed shape."""
    return {
        "document_slug": "fake-access-policy",
        "title": "Fake Access Policy",
        "version": "v1",
        "heading_path": f"Section {index}",
        "chunk_id": f"fake-chunk-{index}",
    }


def _args(
    target: uuid.UUID,
    role: uuid.UUID,
    *,
    justification: str,
    key: str,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "target_user_id": str(target),
        "role_id": str(role),
        "justification": justification,
        "idempotency_key": key,
        **extra,
    }


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


def _request_rows(database_url: str, justification: str) -> list[Any]:
    return _rows(
        database_url,
        "SELECT id, tenant_id, requester_user_id, target_user_id, role_id,"
        " requested_expiry_days, risk_class, decision, decision_reasons, citations,"
        " status, approver_user_ids, approvals, idempotency_key"
        " FROM access_requests WHERE justification = :justification",
        {"justification": justification},
    )


def _one_request_row(database_url: str, justification: str) -> Any:
    rows = _request_rows(database_url, justification)
    assert len(rows) == 1, f"expected exactly one request row, got {len(rows)}"
    return rows[0]


def _request_row_count(database_url: str, tenant_id: uuid.UUID) -> int:
    rows = _rows(
        database_url,
        "SELECT count(*) FROM access_requests WHERE tenant_id = :tenant",
        {"tenant": tenant_id},
    )
    return int(rows[0][0])


def _grant_count(database_url: str, tenant_id: uuid.UUID) -> int:
    rows = _rows(
        database_url,
        "SELECT count(*) FROM grants WHERE tenant_id = :tenant",
        {"tenant": tenant_id},
    )
    return int(rows[0][0])


def _grants_for(database_url: str, user_id: uuid.UUID, role_id: uuid.UUID) -> list[Any]:
    return _rows(
        database_url,
        "SELECT id, request_id, granted_at, expires_at, status"
        " FROM grants WHERE user_id = :user AND role_id = :role",
        {"user": user_id, "role": role_id},
    )


def _held_role_ids(database_url: str, user_id: uuid.UUID) -> set[uuid.UUID]:
    rows = _rows(
        database_url,
        "SELECT role_id FROM grants WHERE user_id = :user AND status = 'active'"
        " AND revoked_at IS NULL",
        {"user": user_id},
    )
    return {uuid.UUID(str(row[0])) for row in rows}


def _set_user_department(database_url: str, user_id: uuid.UUID, department_id: uuid.UUID) -> None:
    """Move one user between departments, around the tool."""

    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text("UPDATE users SET department_id = :department WHERE id = :id"),
                    {"department": department_id, "id": user_id},
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


def _slots(approvals: Any) -> list[tuple[str, uuid.UUID | None]]:
    """The stored slot list as (slot, resolved id or None), order preserved."""
    return [
        (
            str(entry["slot"]),
            None if entry["user_id"] is None else uuid.UUID(str(entry["user_id"])),
        )
        for entry in approvals
    ]


# --- the three branches --------------------------------------------------------


def test_an_in_template_low_role_is_executed_with_three_events_and_one_grant(
    mcp_env: SeededMcp,
) -> None:
    """Catches a handler that files the request but never executes the grant
    (zero grants and a row left `pending`), executes it but leaves the row's
    status on the classification rather than `executed`, drops the caller's
    `idempotency_key` from its column, or emits
    the wrong audit sequence — a missing `request.auto_executed` or
    `grant.executed` is invisible to a count and visible to this list."""
    caller = _seeded(CLEAN_USER)
    justification = _justification()
    events_before = _audit_event_types(mcp_env.database_url, caller.tenant_id)
    grants_before = _grant_count(mcp_env.database_url, caller.tenant_id)
    key = _key()

    result = call_tool(
        bearer(token_for(caller)),
        CREATE,
        _args(caller.user_id, LOW_ROLE, justification=justification, key=key),
    )

    payload = _structured(result)
    assert payload["risk_class"] == "low"
    assert payload["decision"] == "auto"
    assert payload["status"] == "executed"
    assert payload["sod_conflicts"] == []
    assert payload["approver_user_ids"] == []
    assert payload["effective_expiry_days"] == LOW_ROLE_EXPIRY_DAYS
    assert payload["grant_id"] is not None
    assert payload["expires_at"] is not None

    row = _one_request_row(mcp_env.database_url, justification)
    assert uuid.UUID(str(row.id)) == uuid.UUID(str(payload["request_id"]))
    assert uuid.UUID(str(row.requester_user_id)) == caller.user_id
    assert uuid.UUID(str(row.target_user_id)) == caller.user_id
    assert uuid.UUID(str(row.role_id)) == LOW_ROLE
    assert row.risk_class == "low"
    assert row.decision == "auto"
    assert row.status == "executed"
    assert row.idempotency_key == key

    assert _grant_count(mcp_env.database_url, caller.tenant_id) == grants_before + 1
    written = [
        grant
        for grant in _grants_for(mcp_env.database_url, caller.user_id, LOW_ROLE)
        if grant.request_id is not None and uuid.UUID(str(grant.request_id)) == row.id
    ]
    assert len(written) == 1, "the executed grant must point at its request"
    assert written[0].status == "active"

    events_after = _audit_event_types(mcp_env.database_url, caller.tenant_id)
    assert events_after == [
        *events_before,
        REQUEST_AUTO_EXECUTED_EVENT,
        GRANT_EXECUTED_EVENT,
        REQUEST_CREATED_EVENT,
    ]


def test_a_critical_role_is_queued_with_the_engines_resolved_approvers(
    mcp_env: SeededMcp,
) -> None:
    """Catches a handler that executes a grant on the `approval` branch (any
    grant at all here is the LLM-free mutation guarantee broken), that fills
    `approver_user_ids` from the full slot list — the unresolved `it_admin`
    group seat would appear as a null id — or that drops that unresolved slot
    from `approvals` instead of recording the whole chain.
    Also catches a missing `request.queued` event.

    The role is on a `critical` `prod_infra` system, so the chain is exactly
    department_manager + system_owner + it_admin, and neither resolved seat is
    the caller, so no self-approval substitution muddies the expectation."""
    caller = _seeded(CLEAN_USER)
    assert PROD_K8S_AUDITOR_ROLE not in _held_role_ids(mcp_env.database_url, caller.user_id), (
        "premise: the caller must not already hold the SoD pair's other half"
    )
    assert PRODUCTION_MANAGER.id != caller.user_id
    assert PROD_K8S_OWNER.id != caller.user_id
    justification = _justification()
    events_before = _audit_event_types(mcp_env.database_url, caller.tenant_id)
    grants_before = _grant_count(mcp_env.database_url, caller.tenant_id)

    result = call_tool(
        bearer(token_for(caller)),
        CREATE,
        _args(caller.user_id, APPROVAL_ROLE, justification=justification, key=_key()),
    )

    payload = _structured(result)
    assert payload["risk_class"] == "critical"
    assert payload["decision"] == "approval"
    assert payload["status"] == "pending"
    assert payload["sod_conflicts"] == []
    assert payload["grant_id"] is None
    assert payload["expires_at"] is None
    assert payload["approver_user_ids"] == [
        str(PRODUCTION_MANAGER.id),
        str(PROD_K8S_OWNER.id),
    ]

    row = _one_request_row(mcp_env.database_url, justification)
    assert row.risk_class == "critical"
    assert row.decision == "approval"
    assert row.status == "pending"
    assert [uuid.UUID(str(value)) for value in row.approver_user_ids] == [
        PRODUCTION_MANAGER.id,
        PROD_K8S_OWNER.id,
    ]
    assert _slots(row.approvals) == [
        ("department_manager", PRODUCTION_MANAGER.id),
        ("system_owner", PROD_K8S_OWNER.id),
        ("it_admin", None),
    ]

    assert _grant_count(mcp_env.database_url, caller.tenant_id) == grants_before
    events_after = _audit_event_types(mcp_env.database_url, caller.tenant_id)
    assert events_after == [*events_before, REQUEST_QUEUED_EVENT, REQUEST_CREATED_EVENT]


def test_an_sod_conflict_is_rejected_citing_the_seeded_rule_and_grants_nothing(
    mcp_env: SeededMcp,
) -> None:
    """Catches a blocked branch that still executes the grant (requirement #8),
    that stores the row as anything but `rejected`, or that loses the SoD
    evidence: a block without the seeded rule's id in `decision_reasons` and
    its `policy_ref` in the response is a refusal nobody can audit.
    Also catches a blocked call that emits a domain event it must not — the
    pipeline's `request.created` is the only event this branch earns."""
    caller = _seeded(CONFLICT_USER)
    assert CONFLICT_HELD_ROLE in _held_role_ids(mcp_env.database_url, caller.user_id), (
        "premise: the planted conflict grant exists"
    )
    rule_id, policy_ref = _seeded_sod_rule(
        mcp_env.database_url, CONFLICT_REQUEST_ROLE, CONFLICT_HELD_ROLE
    )
    justification = _justification()
    events_before = _audit_event_types(mcp_env.database_url, caller.tenant_id)
    grants_before = _grant_count(mcp_env.database_url, caller.tenant_id)

    result = call_tool(
        bearer(token_for(caller)),
        CREATE,
        _args(caller.user_id, CONFLICT_REQUEST_ROLE, justification=justification, key=_key()),
    )

    payload = _structured(result)
    assert payload["decision"] == "blocked"
    assert payload["status"] == "rejected"
    assert payload["risk_class"] == "privileged"
    assert payload["grant_id"] is None
    assert payload["expires_at"] is None
    assert len(payload["sod_conflicts"]) == 1, payload["sod_conflicts"]
    conflict = payload["sod_conflicts"][0]
    assert conflict["rule_id"] == str(rule_id)
    assert conflict["policy_ref"] == policy_ref
    assert conflict["conflicting_role_id"] == str(CONFLICT_HELD_ROLE)
    assert f"SOD_CONFLICT:{rule_id}" in payload["reasons"]
    assert policy_ref in payload["policy_refs"]

    row = _one_request_row(mcp_env.database_url, justification)
    assert row.decision == "blocked"
    assert row.status == "rejected"
    assert row.risk_class == "privileged"
    assert f"SOD_CONFLICT:{rule_id}" in list(row.decision_reasons)

    assert _grant_count(mcp_env.database_url, caller.tenant_id) == grants_before
    events_after = _audit_event_types(mcp_env.database_url, caller.tenant_id)
    assert events_after == [*events_before, REQUEST_CREATED_EVENT]


# --- on-behalf (two matrix rows for one wire name) ---------------


def test_a_manager_may_name_a_report_in_their_own_department(
    mcp_env: SeededMcp,
) -> None:
    """Catches an on-behalf seam that refuses every target but the token's own
    user (the `own_department` row would then be dead), or one that
    silently rewrites the target to the caller — the row's `target_user_id`
    would be the manager and the grant would land on the wrong person."""
    manager = _seeded(PRODUCTION_MANAGER)
    report = _seeded(PRODUCTION_REPORT)
    assert "manager" in manager.principals, "premise: the seeded index 0 is the manager"
    assert manager.department_id == report.department_id
    assert manager.user_id != report.user_id
    justification = _justification()
    grants_before = _grant_count(mcp_env.database_url, manager.tenant_id)

    result = call_tool(
        bearer(token_for(manager)),
        CREATE,
        _args(report.user_id, LOW_ROLE, justification=justification, key=_key()),
    )

    payload = _structured(result)
    assert payload["decision"] == "auto"
    assert payload["status"] == "executed"

    row = _one_request_row(mcp_env.database_url, justification)
    assert uuid.UUID(str(row.requester_user_id)) == manager.user_id
    assert uuid.UUID(str(row.target_user_id)) == report.user_id
    assert _grant_count(mcp_env.database_url, manager.tenant_id) == grants_before + 1
    written = [
        grant
        for grant in _grants_for(mcp_env.database_url, report.user_id, LOW_ROLE)
        if grant.request_id is not None and uuid.UUID(str(grant.request_id)) == row.id
    ]
    assert len(written) == 1, "the grant must be written for the target, not the caller"


def test_a_manager_naming_a_target_in_another_department_is_denied(
    mcp_env: SeededMcp,
) -> None:
    """Catches an on-behalf seam that stops at `rbac.authorize(...ON_BEHALF)`
    without enforcing the `own_department` scope it returns.

    The caller is a real `manager`, so the matrix ALLOWS the on-behalf tool
    for them; the target is a real user in the SAME tenant,
    so the tenant guard cannot refuse on the rule's behalf. Removing the
    department comparison is therefore the only change that flips this
    result. It also pins the refusal's shape: `FORBIDDEN`, exactly one
    `tool.denied` (a `tool.failed` here would file a permission decision as
    an outage), and nothing written."""
    manager = _seeded(PRODUCTION_MANAGER)
    outsider = _seeded(OTHER_DEPARTMENT_TARGET)
    assert manager.tenant_id == outsider.tenant_id, "the point of this test"
    assert manager.department_id != outsider.department_id
    justification = _justification()
    events_before = _audit_event_types(mcp_env.database_url, manager.tenant_id)
    requests_before = _request_row_count(mcp_env.database_url, manager.tenant_id)
    grants_before = _grant_count(mcp_env.database_url, manager.tenant_id)

    result = call_tool(
        bearer(token_for(manager)),
        CREATE,
        _args(outsider.user_id, LOW_ROLE, justification=justification, key=_key()),
    )

    assert error_code(result) == "FORBIDDEN"
    assert _request_rows(mcp_env.database_url, justification) == []
    assert _request_row_count(mcp_env.database_url, manager.tenant_id) == requests_before
    assert _grant_count(mcp_env.database_url, manager.tenant_id) == grants_before
    events_after = _audit_event_types(mcp_env.database_url, manager.tenant_id)
    assert events_after == [*events_before, DENIED_EVENT]


def test_an_employee_may_not_name_a_colleague_even_in_their_own_department(
    mcp_env: SeededMcp,
) -> None:
    """Catches the `rbac.authorize(...CREATE_ACCESS_REQUEST_ON_BEHALF)` call
    being deleted while the department comparison stays.

    The two on-behalf cases above are both DECIDED by that comparison, so
    they pass with the matrix check gone. This one cannot be: the caller is a
    plain `employee`, whom the on-behalf row denies outright, and the
    target is in the caller's OWN department, so the comparison would let it
    through. Only the matrix check refuses here."""
    caller = _seeded(CLEAN_USER)
    colleague = _seeded(PRODUCTION_REPORT)
    assert caller.principals == ("employee",), (
        f"premise: the caller must hold no on-behalf principal, has {caller.principals}"
    )
    assert caller.department_id == colleague.department_id, "the point of this test"
    assert caller.user_id != colleague.user_id
    justification = _justification()
    events_before = _audit_event_types(mcp_env.database_url, caller.tenant_id)
    grants_before = _grant_count(mcp_env.database_url, caller.tenant_id)

    result = call_tool(
        bearer(token_for(caller)),
        CREATE,
        _args(colleague.user_id, LOW_ROLE, justification=justification, key=_key()),
    )

    assert error_code(result) == "FORBIDDEN"
    assert _request_rows(mcp_env.database_url, justification) == []
    assert _grant_count(mcp_env.database_url, caller.tenant_id) == grants_before
    events_after = _audit_event_types(mcp_env.database_url, caller.tenant_id)
    assert events_after == [*events_before, DENIED_EVENT]


def test_an_hr_officer_may_name_a_target_in_any_department(
    mcp_env: SeededMcp,
) -> None:
    """Catches the `scope is Scope.ANY` branch being deleted, which would drop
    `hr`, `it_admin` and `platform_admin` into the `own_department`
    comparison and refuse them a cross-department target — the opposite of
    the matrix, which gives those three `any`.

    The target is deliberately in ANOTHER department, so the `own_department`
    path cannot produce this success."""
    officer = _seeded(HR_OFFICER)
    target = _seeded(PRODUCTION_REPORT)
    assert "hr" in officer.principals, "premise: the seeded HR index 1 is the officer"
    assert officer.department_id != target.department_id, "the point of this test"
    justification = _justification()

    result = call_tool(
        bearer(token_for(officer)),
        CREATE,
        _args(target.user_id, LOW_ROLE, justification=justification, key=_key()),
    )

    assert result.is_error is False, result.content
    row = _one_request_row(mcp_env.database_url, justification)
    assert uuid.UUID(str(row.requester_user_id)) == officer.user_id
    assert uuid.UUID(str(row.target_user_id)) == target.user_id


def test_the_callers_department_is_read_from_the_directory_not_the_token(
    mcp_env: SeededMcp,
) -> None:
    """Catches the `own_department` comparison reading the token's
    `department_id` claim instead of the caller's current directory row.

    The token is minted while the manager is in Production and is NOT
    reissued; the manager's row then moves to Sales. The claim still says
    Production, the directory says Sales, and the target is in Production —
    so an implementation trusting the claim allows the call, and one reading
    the directory refuses it. Without this case both implementations pass,
    because in the seed the claim and the row always agree.

    A manager transferred mid-day keeping authority over the department they
    left for the life of their token is the real failure this prevents."""
    manager = _seeded(PRODUCTION_MANAGER)
    report = _seeded(PRODUCTION_REPORT)
    assert manager.department_id == report.department_id, "premise: the token's claim allows this"
    token = token_for(manager)
    sales_department_id = ROWS.department_ids["Sales"]
    assert sales_department_id != manager.department_id
    justification = _justification()
    grants_before = _grant_count(mcp_env.database_url, manager.tenant_id)

    _set_user_department(mcp_env.database_url, manager.user_id, sales_department_id)
    try:
        result = call_tool(
            bearer(token),
            CREATE,
            _args(report.user_id, LOW_ROLE, justification=justification, key=_key()),
        )
    finally:
        _set_user_department(mcp_env.database_url, manager.user_id, manager.department_id)

    assert error_code(result) == "FORBIDDEN"
    assert _request_rows(mcp_env.database_url, justification) == []
    assert _grant_count(mcp_env.database_url, manager.tenant_id) == grants_before


# --- replay (requirement #19) ---------------------------------------------------


def test_a_replayed_call_is_byte_identical_and_files_one_row_and_one_grant(
    mcp_env: SeededMcp,
) -> None:
    """Catches the spec registered non-mutating (the body would run twice: two
    request rows and two grants for one key), a replay response rebuilt rather
    than replayed (the bytes differ), or a replay re-emitting the domain
    events — a second `grant.executed` for one grant is a false record."""
    caller = _seeded(CLEAN_USER)
    key = _key()
    justification = _justification()
    arguments = _args(caller.user_id, LOW_ROLE, justification=justification, key=key)
    events_before = _audit_event_types(mcp_env.database_url, caller.tenant_id)
    grants_before = _grant_count(mcp_env.database_url, caller.tenant_id)

    first = call_tool(bearer(token_for(caller)), CREATE, arguments)
    second = call_tool(bearer(token_for(caller)), CREATE, arguments)

    assert first.is_error is False, first.content
    assert second.is_error is False, second.content
    assert _response_text(second) == _response_text(first)
    assert second.structured_content == first.structured_content

    assert len(_request_rows(mcp_env.database_url, justification)) == 1
    assert _grant_count(mcp_env.database_url, caller.tenant_id) == grants_before + 1
    events_after = _audit_event_types(mcp_env.database_url, caller.tenant_id)
    assert events_after == [
        *events_before,
        REQUEST_AUTO_EXECUTED_EVENT,
        GRANT_EXECUTED_EVENT,
        REQUEST_CREATED_EVENT,
        REPLAYED_EVENT,
    ]


def test_the_same_key_with_a_different_request_is_a_conflict(
    mcp_env: SeededMcp,
) -> None:
    """Catches a fingerprint built from the key alone, which would answer a
    genuinely different request with the first one's response — here, access
    the caller never asked for. The second request must leave no row and no
    grant."""
    caller = _seeded(CLEAN_USER)
    key = _key()
    first_justification = _justification()
    second_justification = _justification()
    grants_before = _grant_count(mcp_env.database_url, caller.tenant_id)

    first = call_tool(
        bearer(token_for(caller)),
        CREATE,
        _args(caller.user_id, LOW_ROLE, justification=first_justification, key=key),
    )
    second = call_tool(
        bearer(token_for(caller)),
        CREATE,
        _args(caller.user_id, LOW_ROLE, justification=second_justification, key=key),
    )

    assert first.is_error is False, first.content
    assert error_code(second) == "IDEMPOTENCY_CONFLICT"
    assert len(_request_rows(mcp_env.database_url, first_justification)) == 1
    assert _request_rows(mcp_env.database_url, second_justification) == []
    assert _grant_count(mcp_env.database_url, caller.tenant_id) == grants_before + 1


# --- exact, inclusive boundaries -----------------------


@pytest.mark.parametrize(
    ("length", "accepted"),
    [
        pytest.param(JUSTIFICATION_MAX_LENGTH, True, id="justification-2000-ok"),
        pytest.param(JUSTIFICATION_MAX_LENGTH + 1, False, id="justification-2001-refused"),
        pytest.param(0, False, id="justification-empty-refused"),
    ],
)
def test_the_justification_boundary_is_inclusive_at_2000(
    mcp_env: SeededMcp,
    length: int,
    accepted: bool,
) -> None:
    """Catches the bound being absent or off by one. The column is unbounded
    `Text`, so nothing downstream refuses an oversized justification: without
    this check 2001 characters are stored, not rejected, and the cap exists
    only on paper. The empty row pins the lower bound at 1."""
    caller = _seeded(CLEAN_USER)
    marker = _justification()
    justification = "" if length == 0 else f"{marker} ".ljust(length, "j")[:length]
    assert len(justification) == length
    grants_before = _grant_count(mcp_env.database_url, caller.tenant_id)

    result = call_tool(
        bearer(token_for(caller)),
        CREATE,
        _args(caller.user_id, LOW_ROLE, justification=justification, key=_key()),
    )

    if accepted:
        assert _structured(result)["status"] == "executed"
        row = _one_request_row(mcp_env.database_url, justification)
        assert row.status == "executed"
        assert _grant_count(mcp_env.database_url, caller.tenant_id) == grants_before + 1
    else:
        assert error_code(result) == "VALIDATION"
        assert _request_rows(mcp_env.database_url, justification) == []
        assert _grant_count(mcp_env.database_url, caller.tenant_id) == grants_before


@pytest.mark.parametrize(
    ("count", "accepted"),
    [
        pytest.param(CITATIONS_MAX, True, id="citations-20-ok"),
        pytest.param(CITATIONS_MAX + 1, False, id="citations-21-refused"),
    ],
)
def test_the_citations_boundary_is_inclusive_at_20(
    mcp_env: SeededMcp,
    count: int,
    accepted: bool,
) -> None:
    """Catches the citations cap being absent or exclusive, and — on the
    accepted side — a handler that takes the citations and drops them instead
    of persisting them onto the row's column."""
    caller = _seeded(CLEAN_USER)
    justification = _justification()
    citations = [_citation(index) for index in range(count)]
    grants_before = _grant_count(mcp_env.database_url, caller.tenant_id)

    result = call_tool(
        bearer(token_for(caller)),
        CREATE,
        _args(
            caller.user_id,
            LOW_ROLE,
            justification=justification,
            key=_key(),
            citations=citations,
        ),
    )

    if accepted:
        assert result.is_error is False, result.content
        row = _one_request_row(mcp_env.database_url, justification)
        assert len(row.citations) == CITATIONS_MAX
        assert _grant_count(mcp_env.database_url, caller.tenant_id) == grants_before + 1
    else:
        assert error_code(result) == "VALIDATION"
        assert _request_rows(mcp_env.database_url, justification) == []
        assert _grant_count(mcp_env.database_url, caller.tenant_id) == grants_before


@pytest.mark.parametrize(
    ("days", "accepted"),
    [
        pytest.param(EXPIRY_DAYS_MAX, True, id="expiry-3650-ok"),
        pytest.param(EXPIRY_DAYS_MAX + 1, False, id="expiry-3651-refused"),
        pytest.param(0, False, id="expiry-0-refused"),
    ],
)
def test_the_requested_expiry_boundary_is_inclusive_at_3650(
    mcp_env: SeededMcp,
    days: int,
    accepted: bool,
) -> None:
    """Catches the mutating input retyping (or dropping) the
    `EXPIRY_DAYS_MAX`: 3650 lands in the int4 column fine and must pass, 3651
    and 0 must be VALIDATION before any row or grant exists."""
    caller = _seeded(CLEAN_USER)
    justification = _justification()
    grants_before = _grant_count(mcp_env.database_url, caller.tenant_id)

    result = call_tool(
        bearer(token_for(caller)),
        CREATE,
        _args(
            caller.user_id,
            LOW_ROLE,
            justification=justification,
            key=_key(),
            requested_expiry_days=days,
        ),
    )

    if accepted:
        payload = _structured(result)
        # The ask is capped by the role's own default, not honoured as given.
        assert payload["effective_expiry_days"] == LOW_ROLE_EXPIRY_DAYS
        assert len(_request_rows(mcp_env.database_url, justification)) == 1
        assert _grant_count(mcp_env.database_url, caller.tenant_id) == grants_before + 1
    else:
        assert error_code(result) == "VALIDATION"
        assert _request_rows(mcp_env.database_url, justification) == []
        assert _grant_count(mcp_env.database_url, caller.tenant_id) == grants_before


CITATION_FIELDS = ("document_slug", "title", "version", "heading_path", "chunk_id")


@pytest.mark.parametrize("field", CITATION_FIELDS)
@pytest.mark.parametrize(
    ("length", "accepted"),
    [
        pytest.param(CITATION_FIELD_MAX_LENGTH, True, id="512-ok"),
        pytest.param(CITATION_FIELD_MAX_LENGTH + 1, False, id="513-refused"),
    ],
)
def test_the_citation_field_boundary_is_inclusive_at_512(
    mcp_env: SeededMcp,
    field: str,
    length: int,
    accepted: bool,
) -> None:
    """Catches the per-field bound being absent, exclusive, or applied to only
    some of the five fields — which is why this is parametrized over every
    field rather than asserted once on `title`.

    `access_requests.citations` is unbounded JSONB, so `CITATIONS_MAX` alone
    caps the number of entries and not their size: without this bound twenty
    citations can carry five arbitrarily long strings each.
    """
    caller = _seeded(CLEAN_USER)
    justification = _justification()
    citation = _citation(0)
    citation[field] = "c" * length
    assert len(citation[field]) == length
    grants_before = _grant_count(mcp_env.database_url, caller.tenant_id)

    result = call_tool(
        bearer(token_for(caller)),
        CREATE,
        _args(
            caller.user_id,
            LOW_ROLE,
            justification=justification,
            key=_key(),
            citations=[citation],
        ),
    )

    if accepted:
        assert result.is_error is False, result.content
        row = _one_request_row(mcp_env.database_url, justification)
        assert row.citations[0][field] == citation[field]
    else:
        assert error_code(result) == "VALIDATION"
        assert _request_rows(mcp_env.database_url, justification) == []
        assert _grant_count(mcp_env.database_url, caller.tenant_id) == grants_before


def test_an_extra_argument_is_refused_with_no_row_and_no_grant(
    mcp_env: SeededMcp,
) -> None:
    """Catches an input model that ignores extras instead of forbidding them
    (the read tools' contract, which is wrong for a mutation): a `user_id` in
    the arguments is a caller who believes they are acting on someone else,
    and dropping it silently would file the request for the token's user
    while the caller reads success as having named the colleague.

    Both users are in the SAME tenant, so the tenant guard
    cannot produce this refusal on the model's behalf."""
    caller = _seeded(CLEAN_USER)
    colleague = _seeded(PRODUCTION_REPORT)
    assert caller.tenant_id == colleague.tenant_id, "the point of this test"
    assert caller.user_id != colleague.user_id
    justification = _justification()
    grants_before = _grant_count(mcp_env.database_url, caller.tenant_id)

    result = call_tool(
        bearer(token_for(caller)),
        CREATE,
        _args(
            caller.user_id,
            LOW_ROLE,
            justification=justification,
            key=_key(),
            user_id=str(colleague.user_id),
        ),
    )

    assert error_code(result) == "VALIDATION"
    assert _request_rows(mcp_env.database_url, justification) == []
    assert _grant_count(mcp_env.database_url, caller.tenant_id) == grants_before


# --- a request already open is returned, not filed again -------------

# Engineering has 46 seeded users and no other test names index 30 or 31, so
# no earlier test has left an open request for them in the shared database.
REPEAT_USER = ROWS.users_by_department["Engineering"][30]
CLASSIFY_REPEAT_USER = ROWS.users_by_department["Engineering"][31]
CLASSIFY = "classify_access_request"


def test_create_returns_open_request_instead_of_filing(mcp_env: SeededMcp) -> None:
    """Catches a create that files a second row for the same target and role
    (the check only in the agent), writes a new audit event, or reports the
    open request with a new number. The first call is the premise: it must
    file a pending row and not claim a repeat itself, so the second call's
    flag cannot come from a default."""
    caller = _seeded(REPEAT_USER)
    first = _structured(
        call_tool(
            bearer(token_for(caller)),
            CREATE,
            _args(
                caller.user_id,
                APPROVAL_ROLE,
                justification=_justification(),
                key=_key(),
            ),
        )
    )
    assert first["status"] == "pending"
    assert first["already_requested"] is False
    rows_before = _request_row_count(mcp_env.database_url, caller.tenant_id)
    grants_before = _grant_count(mcp_env.database_url, caller.tenant_id)
    events_before = _audit_event_types(mcp_env.database_url, caller.tenant_id)
    second_justification = _justification()

    second = _structured(
        call_tool(
            bearer(token_for(caller)),
            CREATE,
            _args(
                caller.user_id,
                APPROVAL_ROLE,
                justification=second_justification,
                key=_key(),
            ),
        )
    )

    assert second["already_requested"] is True
    assert second["request_number"] == first["request_number"]
    assert second["request_id"] == first["request_id"]
    assert second["status"] == "pending"
    assert _request_row_count(mcp_env.database_url, caller.tenant_id) == rows_before
    assert _request_rows(mcp_env.database_url, second_justification) == []
    assert _grant_count(mcp_env.database_url, caller.tenant_id) == grants_before
    assert _audit_event_types(mcp_env.database_url, caller.tenant_id) == events_before


def test_create_after_rejected_files_new(mcp_env: SeededMcp) -> None:
    """Catches an open-request check that counts any earlier request, rejected
    ones included: a blocked request is closed, so asking again must file a new
    row. The same SoD-conflict request is made twice, so the first call is
    rejected and the second must still be filed (and be rejected again)."""
    caller = _seeded(CONFLICT_USER)
    first = _structured(
        call_tool(
            bearer(token_for(caller)),
            CREATE,
            _args(
                caller.user_id,
                CONFLICT_REQUEST_ROLE,
                justification=_justification(),
                key=_key(),
            ),
        )
    )
    assert first["status"] == "rejected"
    rows_before = _request_row_count(mcp_env.database_url, caller.tenant_id)
    justification = _justification()

    second = _structured(
        call_tool(
            bearer(token_for(caller)),
            CREATE,
            _args(
                caller.user_id,
                CONFLICT_REQUEST_ROLE,
                justification=justification,
                key=_key(),
            ),
        )
    )

    assert second["already_requested"] is False
    assert second["request_number"] != first["request_number"]
    assert _request_row_count(mcp_env.database_url, caller.tenant_id) == rows_before + 1
    assert len(_request_rows(mcp_env.database_url, justification)) == 1


def test_classify_reports_open_request(mcp_env: SeededMcp) -> None:
    """Catches a classify that ignores the open request, so a planner that
    classifies first never learns the person already asked, or one that names
    another request's number. A role with no open request is the contrast: the
    flag must be false and the number absent, so the flag is not a constant."""
    caller = _seeded(CLASSIFY_REPEAT_USER)
    filed = _structured(
        call_tool(
            bearer(token_for(caller)),
            CREATE,
            _args(
                caller.user_id,
                APPROVAL_ROLE,
                justification=_justification(),
                key=_key(),
            ),
        )
    )
    assert filed["status"] == "pending"

    open_one = _structured(
        call_tool(
            bearer(token_for(caller)),
            CLASSIFY,
            {"target_user_id": str(caller.user_id), "role_id": str(APPROVAL_ROLE)},
        )
    )
    other = _structured(
        call_tool(
            bearer(token_for(caller)),
            CLASSIFY,
            {"target_user_id": str(caller.user_id), "role_id": str(LOW_ROLE)},
        )
    )

    assert open_one["already_requested"] is True
    assert open_one["request_number"] == filed["request_number"]
    assert open_one["status"] == "pending"
    assert other["already_requested"] is False
    assert other["request_number"] is None
    assert other["status"] is None
