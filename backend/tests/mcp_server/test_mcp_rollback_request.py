"""An earlier change, sections 1 and 6: `rollback_request`, through the transport.

Surface under test (written before it exists):

``mcp_server.tools.rollback`` (new module, the design's Admin group)
    ``RollbackRequestInput(MutatingToolInput)`` — ``request_id: uuid.UUID``
    and ``reason: str`` bounded ``1..REASON_MAX_LENGTH``, the constant
    IMPORTED from ``mcp_server.tools.access`` rather than retyped, plus the
    idempotency key and ``extra="forbid"`` of ``MutatingToolInput``. No tenant
    and no user argument: both come from the token.
    ``RollbackRequestOutput`` — ``request_id``, ``status``,
    ``revoked_grant_ids`` and ``revoked_count``.
    ``rollback_request`` — a registered MUTATING tool whose registry
    ``event_type`` is ``tool.succeeded``; it writes one ``grant.revoked`` and
    one ``grant.rolled_back`` per revoked row itself (decision E), so the
    pipeline's summary event lands last.

Refusals: ``NOT_FOUND`` for a request that does not exist and for another
tenant's (identical answers, so the id is not an existence oracle),
``FORBIDDEN`` (+ ``tool.denied``) for the five principals the design gives no
cell on this row, ``POLICY_BLOCKED`` for a request that is not ``executed``,
for one past the 24h window and for one with no live grant left (decision C),
and ``VALIDATION`` for a reason outside 1..2000.

There is NO scope helper on this tool: the design gives it `it_admin` and
`platform_admin` only, both at `Scope.ANY`, so the pipeline's matrix stage is
the whole permission check and the denial case below IS its kill (unlike
`revoke_access`, whose handler re-runs `rbac.authorize` — a project rule).

The window's exact inclusivity is NOT asserted here: over the wire the handler
reads its own clock, so an "exactly 24h" case would race it. It is pinned
purely by
`test_access_rollback.py::test_the_rollback_window_is_inclusive_at_exactly_24_hours`;
what this file pins is that the window is applied at all, and to WHICH
`granted_at` when a request produced more than one grant (decision A).

Audit deltas are asserted as exact ordered event-type lists against a before
snapshot, never as counts: a missing domain event, a duplicated one, or a
denial filed as an outage is invisible to a count.

Fixture cost: everything rides an earlier change's session-scoped
``mcp_fixtures`` seam — one seeded throwaway database, one dedicated Redis
index flushed per test. Per-test isolation inside the shared database is a
namespace: every case plants its OWN department, system, role, people,
request and grants (uuid4-suffixed names) and sends a fresh idempotency key
per call, so no two cases can see each other's rows.

Every raw-SQL fixture binds `datetime` OBJECTS, never strings: asyncpg binds
by type and does not coerce (the project notes, an earlier change).

No pytest-asyncio: each case is a sync function driving one ``asyncio.run``.
"""

import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from helpdesk.shared.models import RequestStatus
from scripts.seed import CUAN, NORDLICHT, TenantRows, UserRow, generate_tenant
from tests.support.mcp import (  # noqa: F401 - fixtures are resolved by name
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

ROLLBACK = "rollback_request"

# the design's two events, appended per revoked row by the handler (decision
# E), and the pipeline's own events. Literals rather than imports: these
# strings are the audit contract, and reading them from the module under test
# would let a rename pass unnoticed.
GRANT_REVOKED_EVENT = "grant.revoked"
GRANT_ROLLED_BACK_EVENT = "grant.rolled_back"
SUCCEEDED_EVENT = "tool.succeeded"
DENIED_EVENT = "tool.denied"
REPLAYED_EVENT = "tool.replayed"
FAILED_EVENT = "tool.failed"

# The bound the wire model must carry. Written out
# here and compared against `mcp_server.tools.access`'s own constant inside
# the boundary case, so the number is pinned in the test rather than borrowed.
REASON_MAX_LENGTH = 2000

# Decision B's literal, as a literal: `grants.revoke_reason` on a rollback is
# `rollback` and not the caller's text, which is what row 21's sweeps match on.
ROLLBACK_REVOKE_REASON = "rollback"

# Which of the two seeded tenants `mcp_env.tenant_a` is depends on the order
# the fixture reads them back, so the rows a caller is built from are resolved
# from that handle's own slug per test rather than pinned to one tenant here.
# Everything below plants into `tenant_a`, so caller and rows must agree.
_TENANTS = {CUAN.slug: CUAN, NORDLICHT.slug: NORDLICHT}

# the design's `rollback_request` row: two principals hold a cell, five hold
# none. `hr` and `manager` are denied here (unlike the review rows) and
# `auditor` too (unlike the audit row), so a row copied from either by habit
# fails.
ALLOWED_PRINCIPALS = ("it_admin", "platform_admin")
DENIED_PRINCIPALS = ("employee", "manager", "system_owner", "hr", "auditor")

# Every `RequestStatus` except `executed`: criterion 5 says the tool answers
# `POLICY_BLOCKED` for all of them, and `rolled_back` is also the §5 race
# loser's answer (the winner took the lock and moved the status first).
NON_EXECUTED_STATUSES = tuple(
    status for status in RequestStatus if status is not RequestStatus.EXECUTED
)

# The principal this file calls with. The callers are FOUND by it rather than
# read off a fixed index: the seed's IT department carries
# system owners, platform admins and admins in an order this file must not
# depend on, and an index would silently resolve to another principal.
CALLING_PRINCIPAL = "it_admin"

ACTIVE_STATUS = "active"
REVOKED_STATUS = "revoked"
PLANTED_START = date(2019, 3, 7)
PLANTED_PASSWORD_HASH = "not-a-real-hash-this-user-never-logs-in"
FAR_EXPIRY = datetime(2099, 6, 1, 12, 0, tzinfo=UTC)
HISTORIC_REVOKED_AT = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
HISTORIC_REASON = "fake historical revocation, must not be rewritten"

# Ages relative to the handler's own clock, which this file cannot control.
# One hour is comfortably inside the 24h window and 25 hours comfortably
# outside it, so neither depends on how long the suite takes to run.
INSIDE_WINDOW = timedelta(hours=1)
OUTSIDE_WINDOW = timedelta(hours=25)


def _tenant_rows(slug: str) -> TenantRows:
    return generate_tenant(_TENANTS[slug])


def _handle(env: SeededMcp, user: UserRow) -> SeededUser:
    """A token-ready handle for one deterministic seeded user of `tenant_a`."""
    rows = _tenant_rows(env.tenant_a.tenant_slug)
    return SeededUser(
        tenant_id=rows.tenant_id,
        tenant_slug=env.tenant_a.tenant_slug,
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        department_id=rows.department_ids[user.department],
        principals=tuple(sorted(user.principals)),
        status=ACTIVE_STATUS,
    )


def _it_admins(env: SeededMcp) -> list[UserRow]:
    """Every seeded `it_admin` of the tenant these cases plant into.

    Read off the seed rather than indexed: which of the two
    tenants `mcp_env.tenant_a` is decides the rows, and the IT department's
    order is not this file's to depend on. Two are needed, for the race case.
    """
    rows = _tenant_rows(env.tenant_a.tenant_slug)
    admins = [
        user for user in rows.users_by_department["IT"] if CALLING_PRINCIPAL in user.principals
    ]
    assert len(admins) >= 2, f"the race case needs two seeded it_admins, found {len(admins)}"
    return admins


def _seeded(env: SeededMcp) -> SeededUser:
    """The seeded `it_admin` every single-caller case calls as."""
    return _handle(env, _it_admins(env)[0])


def _second_seeded(env: SeededMcp) -> SeededUser:
    """A DIFFERENT seeded `it_admin` of the same tenant, for the race case."""
    return _handle(env, _it_admins(env)[1])


def _key() -> str:
    """A unique idempotency key: half the per-test namespace."""
    return f"fake-key-{uuid.uuid4().hex}"


def _args(request_id: uuid.UUID | str, **extra: Any) -> dict[str, Any]:
    """Minimally valid arguments naming one request."""
    return {
        "request_id": str(request_id),
        "reason": f"fake rollback reason {uuid.uuid4().hex}",
        "idempotency_key": _key(),
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


# --- raw-SQL probes (reading and writing around the tool, never through it) -----


def _rows(database_url: str, sql: str, params: dict[str, Any]) -> list[Any]:
    async def scenario() -> list[Any]:
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as conn:
                return list(await conn.execute(text(sql), params))
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _execute(database_url: str, sql: str, params: dict[str, Any]) -> None:
    """One committed statement written around the tool."""

    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as conn:
                await conn.execute(text(sql), params)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def _plant_department(database_url: str, tenant_id: uuid.UUID) -> uuid.UUID:
    department_id = uuid.uuid4()
    _execute(
        database_url,
        "INSERT INTO departments (id, tenant_id, name, manager_user_id, created_at, updated_at)"
        " VALUES (:id, :tenant, :name, NULL, now(), now())",
        {
            "id": department_id,
            "tenant": tenant_id,
            "name": f"Fake Rollback Dept {uuid.uuid4().hex[:12]}",
        },
    )
    return department_id


def _plant_user(database_url: str, tenant_id: uuid.UUID, department_id: uuid.UUID) -> uuid.UUID:
    user_id = uuid.uuid4()
    _execute(
        database_url,
        "INSERT INTO users (id, tenant_id, department_id, email, display_name, password_hash,"
        " principals, status, start_date, end_date, created_at, updated_at)"
        " VALUES (:id, :tenant, :department, :email, :display_name, :password_hash,"
        " :principals, :status, :start_date, NULL, now(), now())",
        {
            "id": user_id,
            "tenant": tenant_id,
            "department": department_id,
            "email": f"fake.rollback.{uuid.uuid4().hex}@fake.invalid",
            "display_name": "Fake Rollback Person",
            "password_hash": PLANTED_PASSWORD_HASH,
            "principals": ["employee"],
            "status": ACTIVE_STATUS,
            # A `date` object, never the string: asyncpg binds by type.
            "start_date": PLANTED_START,
        },
    )
    return user_id


def _plant_role(database_url: str, tenant_id: uuid.UUID) -> uuid.UUID:
    system_id = uuid.uuid4()
    _execute(
        database_url,
        "INSERT INTO systems (id, tenant_id, slug, name, sensitivity, category, owner_user_id,"
        " created_at, updated_at)"
        " VALUES (:id, :tenant, :slug, :name, 'low', 'other', NULL, now(), now())",
        {
            "id": system_id,
            "tenant": tenant_id,
            "slug": f"fake-sys-{uuid.uuid4().hex[:12]}",
            "name": "Fake Rollback System",
        },
    )
    role_id = uuid.uuid4()
    _execute(
        database_url,
        "INSERT INTO roles (id, tenant_id, system_id, slug, name, privilege,"
        " default_expiry_days, created_at, updated_at)"
        " VALUES (:id, :tenant, :system, :slug, :name, 'read', 365, now(), now())",
        {
            "id": role_id,
            "tenant": tenant_id,
            "system": system_id,
            "slug": f"fake-role-{uuid.uuid4().hex[:12]}",
            "name": "Fake Rollback Role",
        },
    )
    return role_id


def _plant_request_row(
    database_url: str,
    tenant_id: uuid.UUID,
    *,
    user_id: uuid.UUID,
    role_id: uuid.UUID,
    status: RequestStatus,
) -> uuid.UUID:
    request_id = uuid.uuid4()
    _execute(
        database_url,
        "INSERT INTO access_requests (id, tenant_id, requester_user_id, target_user_id, role_id,"
        " justification, requested_expiry_days, risk_class, decision, decision_reasons, citations,"
        " status, approver_user_ids, approvals, session_id, trace_id, idempotency_key,"
        " created_at, updated_at)"
        " VALUES (:id, :tenant, :user, :user, :role, :justification, NULL, 'low', 'auto',"
        " '[]'::jsonb, '[]'::jsonb, :status, ARRAY[]::uuid[], '[]'::jsonb, NULL, NULL, NULL,"
        " now(), now())",
        {
            "id": request_id,
            "tenant": tenant_id,
            "user": user_id,
            "role": role_id,
            "justification": f"fake rollback fixture request {uuid.uuid4().hex}",
            "status": status.value,
        },
    )
    return request_id


def _plant_grant_row(
    database_url: str,
    tenant_id: uuid.UUID,
    *,
    user_id: uuid.UUID,
    role_id: uuid.UUID,
    request_id: uuid.UUID,
    age: timedelta = INSIDE_WINDOW,
    revoked: bool = False,
) -> uuid.UUID:
    grant_id = uuid.uuid4()
    _execute(
        database_url,
        "INSERT INTO grants (id, tenant_id, user_id, role_id, request_id, granted_by_user_id,"
        " granted_at, expires_at, revoked_at, revoke_reason, status, created_at, updated_at)"
        " VALUES (:id, :tenant, :user, :role, :request, NULL, :granted_at, :expires_at,"
        " :revoked_at, :revoke_reason, :status, now(), now())",
        {
            "id": grant_id,
            "tenant": tenant_id,
            "user": user_id,
            "role": role_id,
            "request": request_id,
            # `datetime` objects, never strings (same reason as above).
            "granted_at": datetime.now(UTC) - age,
            "expires_at": FAR_EXPIRY,
            "revoked_at": HISTORIC_REVOKED_AT if revoked else None,
            "revoke_reason": HISTORIC_REASON if revoked else None,
            "status": REVOKED_STATUS if revoked else ACTIVE_STATUS,
        },
    )
    return grant_id


@dataclass(frozen=True)
class Planted:
    """One request, its target and every grant row it produced."""

    tenant_id: uuid.UUID
    request_id: uuid.UUID
    user_id: uuid.UUID
    role_id: uuid.UUID
    grant_ids: tuple[uuid.UUID, ...]


def plant(
    env: SeededMcp,
    *,
    tenant_id: uuid.UUID | None = None,
    status: RequestStatus = RequestStatus.EXECUTED,
    ages: tuple[timedelta, ...] = (INSIDE_WINDOW,),
    revoked: bool = False,
) -> Planted:
    """A whole department, cast, request and grant set, owned by one case.

    Everything is planted fresh, so the only grants a rollback of this request
    can reach are the ones the case asked for and the counts below are exact.
    """
    tenant = env.tenant_a.tenant_id if tenant_id is None else tenant_id
    url = env.database_url
    department_id = _plant_department(url, tenant)
    user_id = _plant_user(url, tenant, department_id)
    role_id = _plant_role(url, tenant)
    request_id = _plant_request_row(url, tenant, user_id=user_id, role_id=role_id, status=status)
    grant_ids = tuple(
        _plant_grant_row(
            url,
            tenant,
            user_id=user_id,
            role_id=role_id,
            request_id=request_id,
            age=age,
            revoked=revoked,
        )
        for age in ages
    )
    return Planted(
        tenant_id=tenant,
        request_id=request_id,
        user_id=user_id,
        role_id=role_id,
        grant_ids=grant_ids,
    )


def _grant_row(database_url: str, grant_id: uuid.UUID) -> Any:
    rows = _rows(
        database_url,
        "SELECT id, status, revoked_at, revoke_reason FROM grants WHERE id = :id",
        {"id": grant_id},
    )
    assert len(rows) == 1, f"expected exactly one grant row, got {len(rows)}"
    return rows[0]


def _request_status(database_url: str, request_id: uuid.UUID) -> str:
    rows = _rows(
        database_url,
        "SELECT status FROM access_requests WHERE id = :id",
        {"id": request_id},
    )
    assert len(rows) == 1, f"expected exactly one access request row, got {len(rows)}"
    return str(rows[0][0])


def _audit_event_types(database_url: str, tenant_id: uuid.UUID) -> list[str]:
    rows = _rows(
        database_url,
        "SELECT event_type FROM audit_events WHERE tenant_id = :tenant ORDER BY seq",
        {"tenant": tenant_id},
    )
    return [str(row[0]) for row in rows]


def _events_for(database_url: str, subject_id: uuid.UUID) -> list[Any]:
    """Every (event_type, subject_type, payload) filed against one subject."""
    return _rows(
        database_url,
        "SELECT event_type, subject_type, payload FROM audit_events"
        " WHERE subject_id = :subject ORDER BY seq",
        {"subject": str(subject_id)},
    )


def _untouched(database_url: str, planted: Planted, *, status: RequestStatus) -> None:
    """A refusal rewrites nothing: the request and every grant read as planted."""
    assert _request_status(database_url, planted.request_id) == status.value
    for grant_id in planted.grant_ids:
        row = _grant_row(database_url, grant_id)
        assert row.revoke_reason != ROLLBACK_REVOKE_REASON, grant_id
        assert _events_for(database_url, grant_id) == [], grant_id


# --- the happy path (criteria 1 and 2) -------------------------------------------


def test_an_it_admin_rolls_back_an_executed_request(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Criterion 1 end to end.

    KILL: dropping the final `set_request_status(..., ROLLED_BACK)`. The grants
    would still die and the response would still name them, so only the
    re-read of the request row catches it — and a request left `executed` can
    be rolled back a second time, which is the §5 race hole.

    Three grant rows are planted on ONE request: two live and one revoked
    long ago. The set is the unit (criterion 1 says "every live grant the
    request produced"), so a handler that stops at the first row reports one id
    where two died, and one that ignores liveness rewrites a historical
    revocation — erasing who ended that access and why, the one thing the
    column exists to keep. `rolled_back_count` is asserted as 2 in both event
    payloads, so a hardcoded 1 fails; the revoked row is asserted byte for byte
    as it was planted.

    The audit is asserted per subject and as a tenant delta: each live grant
    carries exactly `grant.revoked` then `grant.rolled_back` (decision E, in
    that order, so a rollback that skips the shared revocation writer fails),
    and the pipeline's single `tool.succeeded` lands LAST — a handler declaring
    `grant.rolled_back` as its registry event would write one event naming the
    tool and fail here.
    """
    caller = _seeded(mcp_env)
    planted = plant(mcp_env, ages=(INSIDE_WINDOW, INSIDE_WINDOW))
    stale = _plant_grant_row(
        mcp_env.database_url,
        planted.tenant_id,
        user_id=planted.user_id,
        role_id=planted.role_id,
        request_id=planted.request_id,
        revoked=True,
    )
    events_before = _audit_event_types(mcp_env.database_url, planted.tenant_id)

    payload = _structured(
        call_tool(
            bearer(token_for(caller, principals=(CALLING_PRINCIPAL,))),
            ROLLBACK,
            _args(planted.request_id),
        )
    )

    assert str(payload["request_id"]) == str(planted.request_id)
    assert payload["status"] == RequestStatus.ROLLED_BACK.value
    assert set(payload["revoked_grant_ids"]) == {str(grant) for grant in planted.grant_ids}
    assert payload["revoked_count"] == 2
    assert _request_status(mcp_env.database_url, planted.request_id) == (
        RequestStatus.ROLLED_BACK.value
    )

    for grant_id in planted.grant_ids:
        row = _grant_row(mcp_env.database_url, grant_id)
        assert row.status == REVOKED_STATUS, grant_id
        assert row.revoked_at is not None
        assert row.revoke_reason == ROLLBACK_REVOKE_REASON, "decision B: the literal, not the text"
        written = _events_for(mcp_env.database_url, grant_id)
        assert [str(event.event_type) for event in written] == [
            GRANT_REVOKED_EVENT,
            GRANT_ROLLED_BACK_EVENT,
        ]
        assert {str(event.subject_type) for event in written} == {"grant"}
        rolled_back_payload = dict(written[-1].payload)
        assert rolled_back_payload["request_id"] == str(planted.request_id)
        assert rolled_back_payload["rolled_back_count"] == 2

    stale_row = _grant_row(mcp_env.database_url, stale)
    assert stale_row.revoked_at == HISTORIC_REVOKED_AT, "a rollback touches LIVE rows only"
    assert stale_row.revoke_reason == HISTORIC_REASON
    assert _events_for(mcp_env.database_url, stale) == []

    assert _audit_event_types(mcp_env.database_url, planted.tenant_id)[-1] == SUCCEEDED_EVENT
    delta = _audit_event_types(mcp_env.database_url, planted.tenant_id)[len(events_before) :]
    assert sorted(delta[:-1]) == sorted(
        [GRANT_REVOKED_EVENT, GRANT_REVOKED_EVENT, GRANT_ROLLED_BACK_EVENT, GRANT_ROLLED_BACK_EVENT]
    )


def test_a_platform_admin_also_reaches_the_tool(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """the design's second cell on this row.

    KILL: a matrix entry (or a registry `tool=`) giving the row `it_admin`
    alone. The happy path above passes on an `it_admin`-only wiring, and the
    denial case below never sends `platform_admin`, so this is the only case
    that fails. The token carries exactly ONE principal, so nothing but that
    cell can admit the call.
    """
    caller = _seeded(mcp_env)
    planted = plant(mcp_env)

    payload = _structured(
        call_tool(
            bearer(token_for(caller, principals=("platform_admin",))),
            ROLLBACK,
            _args(planted.request_id),
        )
    )

    assert payload["revoked_count"] == 1
    assert _grant_row(mcp_env.database_url, planted.grant_ids[0]).status == REVOKED_STATUS


# --- the design's five empty cells (criterion 7) ------------------------------------


@pytest.mark.parametrize("principal", DENIED_PRINCIPALS)
def test_the_five_denied_principals_roll_nothing_back(
    mcp_env: SeededMcp,  # noqa: F811
    principal: str,
) -> None:
    """the design's five empty cells on this row, and the record they leave.

    KILL: adding this principal to the `rollback_request` cell of the RBAC
    matrix. Unlike `revoke_access`, this handler runs no scope helper and
    therefore never re-runs `rbac.authorize` (plan §2), so the pipeline's
    matrix stage is the only guard that can refuse here and deleting it hands
    every one of these five the undo button (a project rule does not apply:
    there is no second copy of the guard).

    Every argument is valid, the request is `executed`, its grant is live and
    one hour old, and everything is in the caller's OWN tenant, so no other
    guard can produce this answer. `tool.denied` rather than `tool.failed`: a
    permission decision filed as an outage is a false record, and a
    count-only assertion cannot tell them apart.
    """
    caller = _seeded(mcp_env)
    planted = plant(mcp_env)
    events_before = _audit_event_types(mcp_env.database_url, planted.tenant_id)

    result = call_tool(
        bearer(token_for(caller, principals=(principal,))), ROLLBACK, _args(planted.request_id)
    )

    assert error_code(result) == "FORBIDDEN"
    _untouched(mcp_env.database_url, planted, status=RequestStatus.EXECUTED)
    assert _audit_event_types(mcp_env.database_url, planted.tenant_id) == [
        *events_before,
        DENIED_EVENT,
    ]


# --- the two absences (criterion 4) ----------------------------------------------


def test_a_missing_and_another_tenants_request_answer_the_same_not_found(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Criterion 4, both absences compared as whole strings.

    KILL: reading the request through anything but the tenant-bound session
    (a `select(AccessRequest).where(id == ...)` on a raw connection, say). The
    other tenant's request is a REAL executed row with a live grant, so a
    lookup that escapes the guard would roll it back and answer success; a
    random uuid would answer `NOT_FOUND` whether the guard exists or not,
    which is why both halves are here. If the two answers differed at all, a
    request id would be a cross-tenant existence oracle.

    The caller holds `platform_admin`, the widest principal the design has, so
    permission cannot be what refuses either call — the widest cell is still
    confined to its own tenant.
    """
    caller = _seeded(mcp_env)
    theirs = plant(mcp_env, tenant_id=mcp_env.tenant_b.tenant_id)
    token = bearer(token_for(caller, principals=("platform_admin",)))

    missing = call_tool(token, ROLLBACK, _args(uuid.uuid4()))
    cross_tenant = call_tool(token, ROLLBACK, _args(theirs.request_id))

    assert error_code(missing) == "NOT_FOUND"
    assert error_code(cross_tenant) == error_code(missing)
    _untouched(mcp_env.database_url, theirs, status=RequestStatus.EXECUTED)


# --- the status gate (criterion 5) -----------------------------------------------


@pytest.mark.parametrize(
    "status", NON_EXECUTED_STATUSES, ids=[s.value for s in NON_EXECUTED_STATUSES]
)
def test_a_request_in_any_status_but_executed_is_refused_policy_blocked(
    mcp_env: SeededMcp,  # noqa: F811
    status: RequestStatus,
) -> None:
    """Criterion 5, every row of the status column.

    KILL: deleting the `status is RequestStatus.EXECUTED` test in the handler.
    Each request here carries a LIVE grant granted one hour ago, so neither
    the window test nor the empty-live-set refusal can answer for it — without
    the status test every one of these rows would be rolled back, including a
    `pending` request whose grant nobody has approved yet and a `rolled_back`
    one, which is the §5 race loser and must not undo an undo.

    `POLICY_BLOCKED` and not `VALIDATION`: the arguments are perfectly well
    formed, and calling this a schema problem sends the caller to fix the wrong
    thing. The delta is exactly `tool.failed`, so a refusal that still revoked
    or still wrote an event fails here.
    """
    caller = _seeded(mcp_env)
    planted = plant(mcp_env, status=status)
    events_before = _audit_event_types(mcp_env.database_url, planted.tenant_id)

    result = call_tool(
        bearer(token_for(caller, principals=(CALLING_PRINCIPAL,))),
        ROLLBACK,
        _args(planted.request_id),
    )

    assert error_code(result) == "POLICY_BLOCKED"
    _untouched(mcp_env.database_url, planted, status=status)
    assert _audit_event_types(mcp_env.database_url, planted.tenant_id) == [
        *events_before,
        FAILED_EVENT,
    ]


# --- the window (criterion 3) ----------------------------------------------------


def test_a_request_granted_twenty_five_hours_ago_is_refused_policy_blocked(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Criterion 3's refusal half, at the transport.

    KILL: deleting the window test from the handler. The request is `executed`
    and its grant is live, `active` and unexpired, so the status gate and the
    empty-live-set refusal both admit it and only the window can refuse — that
    is what makes this case the window's own. The happy path
    above sends the same shape one hour old and succeeds, so a handler that
    refuses every request passes nothing.

    The exact inclusive edge is not asserted here (the handler reads its own
    clock over the wire); it is pinned purely by
    `test_access_rollback.py::test_the_rollback_window_is_inclusive_at_exactly_24_hours`.
    """
    caller = _seeded(mcp_env)
    planted = plant(mcp_env, ages=(OUTSIDE_WINDOW,))
    events_before = _audit_event_types(mcp_env.database_url, planted.tenant_id)

    result = call_tool(
        bearer(token_for(caller, principals=(CALLING_PRINCIPAL,))),
        ROLLBACK,
        _args(planted.request_id),
    )

    assert error_code(result) == "POLICY_BLOCKED"
    _untouched(mcp_env.database_url, planted, status=RequestStatus.EXECUTED)
    assert _audit_event_types(mcp_env.database_url, planted.tenant_id) == [
        *events_before,
        FAILED_EVENT,
    ]


def test_the_window_is_measured_from_the_earliest_grant_of_the_request(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Decision A's multi-row half.

    KILL: `max(granted_at)` (or "the first row the query happens to return")
    where the handler must use `min(granted_at)`. Two live grants hang off one
    request, one 25 hours old and one an hour old: measuring from the newest
    admits the call and quietly revokes an access that is a day past its undo
    window, measuring from the oldest refuses it. No other case in this file
    plants two rows of different ages, so this is the only one that separates
    them, and the single-row cases pass under either choice.
    """
    caller = _seeded(mcp_env)
    planted = plant(mcp_env, ages=(OUTSIDE_WINDOW, INSIDE_WINDOW))

    result = call_tool(
        bearer(token_for(caller, principals=(CALLING_PRINCIPAL,))),
        ROLLBACK,
        _args(planted.request_id),
    )

    assert error_code(result) == "POLICY_BLOCKED"
    _untouched(mcp_env.database_url, planted, status=RequestStatus.EXECUTED)


# --- decision C (criterion 6) ----------------------------------------------------


def test_a_request_whose_grants_are_already_revoked_is_refused_policy_blocked(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Decision C at the transport.

    KILL: dropping the liveness filter from `lock_live_grants_for_request`,
    which would re-revoke a row that died months ago — rewriting the reason and
    the timestamp that say who ended that access and why — and mark the request
    `rolled_back` on the strength of it. The grant here was planted one hour
    ago, so the window admits it and the status is `executed`, leaving the
    empty live set as the only thing that can refuse.

    `POLICY_BLOCKED` rather than `NOT_FOUND`: the caller reached a real request
    of their own, and telling them it does not exist would be a lie.
    """
    caller = _seeded(mcp_env)
    planted = plant(mcp_env, revoked=True)
    events_before = _audit_event_types(mcp_env.database_url, planted.tenant_id)

    result = call_tool(
        bearer(token_for(caller, principals=(CALLING_PRINCIPAL,))),
        ROLLBACK,
        _args(planted.request_id),
    )

    assert error_code(result) == "POLICY_BLOCKED"
    _untouched(mcp_env.database_url, planted, status=RequestStatus.EXECUTED)
    row = _grant_row(mcp_env.database_url, planted.grant_ids[0])
    assert row.revoked_at == HISTORIC_REVOKED_AT
    assert row.revoke_reason == HISTORIC_REASON
    assert _audit_event_types(mcp_env.database_url, planted.tenant_id) == [
        *events_before,
        FAILED_EVENT,
    ]


# --- the input surface (criterion 9, a project rule and 2) ---------------------


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
    """Criterion 9: both ends, both directions, against the exact number.

    KILL: `Field(min_length=1)` with no `max_length` on `RollbackRequestInput`,
    or a `gt`/`lt` where the plan says inclusive. The caller's reason lands in
    the unbounded `Text` payload of `grant.rolled_back`, so this Pydantic bound
    is the only backstop there is, and a one-sided assertion
    cannot tell an inclusive bound from an exclusive one. The lower bound is 1
    and not 0 because an undo with an empty reason is one nobody can audit.

    The bound is read off `mcp_server.tools.access`'s own constant, not retyped
    in the admin module (criterion 9, a project rule): widening one tool's
    reason without the other's is exactly the drift this import prevents. The
    refused rows are re-read to prove the check runs BEFORE any effect.
    """
    from helpdesk.mcp_server.tools import rollback
    from helpdesk.mcp_server.tools.access import REASON_MAX_LENGTH as WIRED_MAX_LENGTH

    assert WIRED_MAX_LENGTH == REASON_MAX_LENGTH == 2000
    assert rollback.RollbackRequestInput.model_fields["reason"].metadata[-1].max_length == (
        WIRED_MAX_LENGTH
    )

    caller = _seeded(mcp_env)
    planted = plant(mcp_env)

    result = call_tool(
        bearer(token_for(caller, principals=(CALLING_PRINCIPAL,))),
        ROLLBACK,
        {
            "request_id": str(planted.request_id),
            "reason": "r" * length,
            "idempotency_key": _key(),
        },
    )

    if accepted:
        assert result.is_error is False, result.content
        assert _grant_row(mcp_env.database_url, planted.grant_ids[0]).status == REVOKED_STATUS
        payload = dict(_events_for(mcp_env.database_url, planted.grant_ids[0])[-1].payload)
        assert payload["reason"] == "r" * length
    else:
        assert error_code(result) == "VALIDATION"
        _untouched(mcp_env.database_url, planted, status=RequestStatus.EXECUTED)


# --- replay (criterion 8) --------------------------------------------------------


def test_a_replay_returns_the_stored_response_and_revokes_nothing_further(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Criterion 8.

    KILL: registering the tool with `mutating=False`, which is what makes the
    pipeline run the body instead of answering from the stored idempotency
    row. The second call would then find the request already `rolled_back` and
    answer `POLICY_BLOCKED` for a call the caller already saw succeed — a retry
    after a dropped connection reading as a policy refusal.

    Byte identity is asserted, not just equality of meaning, so a response
    rebuilt rather than replayed fails; the audit delta is exactly
    `tool.replayed`, so a second `grant.rolled_back` — a false record of an
    undo that happened once — fails as well.
    """
    caller = _seeded(mcp_env)
    planted = plant(mcp_env)
    arguments = _args(planted.request_id)
    token = bearer(token_for(caller, principals=(CALLING_PRINCIPAL,)))

    first = call_tool(token, ROLLBACK, arguments)
    events_between = _audit_event_types(mcp_env.database_url, planted.tenant_id)
    second = call_tool(token, ROLLBACK, arguments)

    assert _structured(first)["revoked_count"] == 1
    assert _response_text(second) == _response_text(first)
    assert second.structured_content == first.structured_content
    assert [
        str(event.event_type) for event in _events_for(mcp_env.database_url, planted.grant_ids[0])
    ] == [GRANT_REVOKED_EVENT, GRANT_ROLLED_BACK_EVENT]
    assert _audit_event_types(mcp_env.database_url, planted.tenant_id) == [
        *events_between,
        REPLAYED_EVENT,
    ]


def test_two_callers_racing_the_same_rollback_undo_it_once(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """The §5 race, at the outcome. Two different callers send two different
    idempotency keys, so the guard cannot serialise them at all; the locks in
    `lock_request` and `lock_live_grants_for_request` are what must.

    NO KILL CLAIM: this case was run with
    `with_for_update()` deleted from `lock_request`, and again with it deleted
    from `lock_live_grants_for_request`, and it PASSED both times — two client
    calls submitted to a thread pool each reconnect and initialise an MCP
    session first, so their transactions do not reliably overlap and the
    second one usually starts after the first has committed. What it pins is
    therefore the OUTCOME an auditor depends on and not the mechanism: one
    caller undoes the request, the other is refused `POLICY_BLOCKED` (never
    `FORBIDDEN` — both are seeded `it_admin`s and genuinely entitled), and
    each grant carries exactly one `grant.revoked` and one `grant.rolled_back`
    however the two calls interleave. `test_access_rollback.py` owns the
    domain-level guarantees; the locks themselves have no test in this file
    that separates them.

    Two live grants are planted rather than one so the assertions below count
    events per ROW: a per-caller writer passes at one row and fails here.
    """
    first_admin = _seeded(mcp_env)
    second_admin = _second_seeded(mcp_env)
    assert first_admin.user_id != second_admin.user_id
    planted = plant(mcp_env, ages=(INSIDE_WINDOW, INSIDE_WINDOW))
    events_before = _audit_event_types(mcp_env.database_url, planted.tenant_id)

    def roll_back(caller: SeededUser) -> Any:
        return call_tool(
            bearer(token_for(caller, principals=(CALLING_PRINCIPAL,))),
            ROLLBACK,
            _args(planted.request_id),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(roll_back, first_admin), pool.submit(roll_back, second_admin)]
        results = [future.result() for future in futures]

    successes = [result for result in results if result.is_error is False]
    refusals = [result for result in results if result.is_error is True]
    assert len(successes) == 1, [result.content for result in results]
    assert len(refusals) == 1
    assert error_code(refusals[0]) == "POLICY_BLOCKED"
    assert set(_structured(successes[0])["revoked_grant_ids"]) == {
        str(grant) for grant in planted.grant_ids
    }

    delta = _audit_event_types(mcp_env.database_url, planted.tenant_id)[len(events_before) :]
    assert delta.count(GRANT_REVOKED_EVENT) == 2, "one grant.revoked per row, never one per caller"
    assert delta.count(GRANT_ROLLED_BACK_EVENT) == 2
    for grant_id in planted.grant_ids:
        assert [str(event.event_type) for event in _events_for(mcp_env.database_url, grant_id)] == [
            GRANT_REVOKED_EVENT,
            GRANT_ROLLED_BACK_EVENT,
        ], grant_id
