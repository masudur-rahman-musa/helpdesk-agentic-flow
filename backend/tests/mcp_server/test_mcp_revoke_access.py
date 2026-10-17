"""`revoke_access`, through the transport.

Surface under test (written before it exists):

``mcp_server.tools.access``
    ``RevokeAccessInput`` — ``grant_id: uuid.UUID``, ``reason: str`` bounded
    ``1..REASON_MAX_LENGTH`` (2000), plus the idempotency key and
    ``extra="forbid"`` from ``MutatingToolInput``.
    ``RevokeAccessOutput`` — ``revoked_grant_ids`` (EVERY live row revoked for
    that ``(user, role)``, the named one included) and
    ``revoked_count``.
    ``revoke_access`` — a registered MUTATING tool whose registry
    ``event_type`` is ``tool.succeeded``; it appends one ``grant.revoked`` per
    revoked row itself, so the pipeline's summary event lands
    last.

Refusals, each a different typed error code: ``NOT_FOUND`` for a grant that does
not exist and for another tenant's (identical answers, so the id is not an
existence oracle), ``FORBIDDEN`` for a caller whose the matrix scope does not
reach the grant, ``POLICY_BLOCKED`` for a grant that is not live,
with the scope check running FIRST so an out-of-scope id cannot be probed for
its state.

Audit deltas are asserted as exact ordered event-type lists against a before
snapshot, never as counts: a missing domain event, a duplicated one, or a
denial filed as an outage is invisible to a count.

Fixture cost: everything rides the session-scoped
``mcp_fixtures`` seam — one seeded throwaway database, one dedicated Redis
index flushed per test. Per-test isolation inside the shared database is a
namespace: each case owns a `(user, role)` pair no other case revokes (proven
by a premise assertion before it plants), and sends a unique idempotency key
per call. Seeded identities are recomputed from
``scripts.seed.generate_tenant`` — deterministic uuid5 ids, never hardcoded
UUIDs.

No pytest-asyncio: each case is a sync function driving one ``asyncio.run``
(the concurrency case drives two, one per thread).
"""

import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from scripts.seed import CUAN, NORDLICHT, UserRow, generate_tenant
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

REVOKE = "revoke_access"

# The revocation event, appended per revoked row by the handler,
# and the pipeline's own events. Literals rather than imports:
# these strings are the audit contract, and reading them from the module under
# test would let a rename pass unnoticed.
GRANT_REVOKED_EVENT = "grant.revoked"
SUCCEEDED_EVENT = "tool.succeeded"
DENIED_EVENT = "tool.denied"
REPLAYED_EVENT = "tool.replayed"

# The bound the wire model must carry. Written out
# here and compared against the module's own constant inside the boundary
# case, so the number is pinned in the test rather than borrowed from the code.
REASON_MAX_LENGTH = 2000

ROWS = generate_tenant(NORDLICHT)
OTHER_TENANT_ROWS = generate_tenant(CUAN)

GRANTED_AT = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
# Both strictly in the past, so a row carrying either is not live.
PAST_EXPIRY = datetime(2026, 2, 1, 9, 0, tzinfo=UTC)
HISTORIC_REVOKED_AT = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
HISTORIC_REASON = "fake historical revocation"
ACTIVE_STATUS = "active"
REVOKED_STATUS = "revoked"

# The seeded manager of every department is index 0 (`scripts.seed`
# `_assign_principals`), so this token really carries the `manager` principal.
PRODUCTION_MANAGER = ROWS.users_by_department["Production"][0]
# The `prod-k8s` owner is an IT user carrying `system_owner`; the two seeded
# `it_admin`s follow the system owners in the IT department.
PROD_K8S_OWNER = ROWS.system_owners["prod-k8s"]
IT_ADMIN_ONE = ROWS.users_by_department["IT"][16]
IT_ADMIN_TWO = ROWS.users_by_department["IT"][17]

# Seeded users this module owns. `test_mcp_approvals.py` owns Production 40
# and up plus 0, 2, 6, and `test_mcp_create_access_request.py` 2 and 6, so
# these sit in the untouched middle. 18 is skipped (the seed plants an
# out-of-template grant where `(46 + index) % 15 == 4`) and 21 too (the
# planted stale grant, `% 20 == 7`).
HAPPY_TARGET = ROWS.users_by_department["Production"][8]
DIRECTORY_PRODUCTION_TARGET = ROWS.users_by_department["Production"][9]
OWNED_TARGET = ROWS.users_by_department["Production"][10]
UNOWNED_TARGET = ROWS.users_by_department["Production"][11]
NOT_LIVE_TARGET = ROWS.users_by_department["Production"][12]
REASON_TARGETS = (
    ROWS.users_by_department["Production"][13],
    ROWS.users_by_department["Production"][14],
    ROWS.users_by_department["Production"][15],
    ROWS.users_by_department["Production"][16],
)
EXTRA_ARG_TARGET = ROWS.users_by_department["Production"][17]
REPLAY_TARGET = ROWS.users_by_department["Production"][19]
DENIED_TARGET = ROWS.users_by_department["Production"][20]
RACE_TARGET = ROWS.users_by_department["Production"][22]

# Same tenant, another department: a cross-tenant target
# would be refused by the tenant guard whether the scope rule holds or not.
SCOPE_TARGET = ROWS.users_by_department["Sales"][15]
DIRECTORY_SALES_TARGET = ROWS.users_by_department["Sales"][16]
NOT_LIVE_OUT_OF_SCOPE_TARGET = ROWS.users_by_department["Sales"][17]

# The second seeded tenant: all this user has to be is somebody this caller's
# token cannot see.
OTHER_TENANT_TARGET = OTHER_TENANT_ROWS.users_by_department["Operations"][5]
OTHER_TENANT_ROLE = OTHER_TENANT_ROWS.role_ids[("datadog", "viewer")]

# Outside Production's and Sales' templates, so every grant a target holds on
# it was planted by the case that revokes it (asserted as a premise).
REVOKED_ROLE = ROWS.role_ids[("grafana", "viewer")]
# `prod-k8s` is owned by PROD_K8S_OWNER; `crm` is owned by somebody else, so
# an `own_systems` scope cannot reach a grant on it.
OWNED_SYSTEM_ROLE = ROWS.role_ids[("prod-k8s", "deployer")]
UNOWNED_ROLE = ROWS.role_ids[("crm", "editor")]


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


def _args(grant_id: uuid.UUID | str, **extra: Any) -> dict[str, Any]:
    """Minimally valid arguments naming one grant."""
    return {
        "grant_id": str(grant_id),
        "reason": f"fake revocation reason {uuid.uuid4().hex}",
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
                result = await conn.execute(text(sql), params)
                return list(result)
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


def _plant_grant(
    database_url: str,
    *,
    user_id: uuid.UUID,
    role_id: uuid.UUID,
    tenant_id: uuid.UUID | None = None,
    expires_at: datetime | None = None,
    revoked_at: datetime | None = None,
    revoke_reason: str | None = None,
    status: str = ACTIVE_STATUS,
) -> uuid.UUID:
    """One committed `grants` row, live unless a case says otherwise."""
    grant_id = uuid.uuid4()
    _execute(
        database_url,
        "INSERT INTO grants (id, tenant_id, user_id, role_id, request_id,"
        " granted_by_user_id, granted_at, expires_at, revoked_at, revoke_reason, status,"
        " created_at, updated_at)"
        " VALUES (:id, :tenant, :user, :role, NULL, NULL, :granted_at, :expires_at,"
        " :revoked_at, :revoke_reason, :status, now(), now())",
        {
            "id": grant_id,
            "tenant": tenant_id or ROWS.tenant_id,
            "user": user_id,
            "role": role_id,
            "granted_at": GRANTED_AT,
            "expires_at": expires_at,
            "revoked_at": revoked_at,
            "revoke_reason": revoke_reason,
            "status": status,
        },
    )
    return grant_id


def _grant_row(database_url: str, grant_id: uuid.UUID) -> Any:
    rows = _rows(
        database_url,
        "SELECT id, user_id, role_id, revoked_at, revoke_reason, status FROM grants WHERE id = :id",
        {"id": grant_id},
    )
    assert len(rows) == 1, f"expected exactly one grant row, got {len(rows)}"
    return rows[0]


def _grant_ids_for(database_url: str, user_id: uuid.UUID, role_id: uuid.UUID) -> list[uuid.UUID]:
    rows = _rows(
        database_url,
        "SELECT id FROM grants WHERE user_id = :user AND role_id = :role ORDER BY id",
        {"user": user_id, "role": role_id},
    )
    return [uuid.UUID(str(row[0])) for row in rows]


def _live_grant_ids(database_url: str, user_id: uuid.UUID, role_id: uuid.UUID) -> list[uuid.UUID]:
    """The same three conjuncts `list_active_grants_for_user` calls live."""
    rows = _rows(
        database_url,
        "SELECT id FROM grants WHERE user_id = :user AND role_id = :role"
        " AND status = 'active' AND revoked_at IS NULL"
        " AND (expires_at IS NULL OR expires_at > now()) ORDER BY id",
        {"user": user_id, "role": role_id},
    )
    return [uuid.UUID(str(row[0])) for row in rows]


def _audit_event_types(database_url: str, tenant_id: uuid.UUID) -> list[str]:
    rows = _rows(
        database_url,
        "SELECT event_type FROM audit_events WHERE tenant_id = :tenant ORDER BY seq",
        {"tenant": tenant_id},
    )
    return [str(row[0]) for row in rows]


def _revoked_events(database_url: str, tenant_id: uuid.UUID) -> list[Any]:
    """Every (subject_type, subject_id, payload) written as `grant.revoked`."""
    return _rows(
        database_url,
        "SELECT subject_type, subject_id, payload FROM audit_events WHERE tenant_id = :tenant"
        " AND event_type = :event_type ORDER BY seq",
        {"tenant": tenant_id, "event_type": GRANT_REVOKED_EVENT},
    )


def _set_user_department(database_url: str, user_id: uuid.UUID, department_id: uuid.UUID) -> None:
    """Move one user between departments, around the tool."""
    _execute(
        database_url,
        "UPDATE users SET department_id = :department WHERE id = :id",
        {"department": department_id, "id": user_id},
    )


def _premise_no_grants(database_url: str, user_id: uuid.UUID, role_id: uuid.UUID) -> None:
    """This case owns the (user, role) pair it is about to plant on."""
    assert _grant_ids_for(database_url, user_id, role_id) == [], (
        "premise: no seeded or previously planted row exists for this (user, role) pair"
    )


# --- the happy path -----------------------------------


def test_a_manager_revokes_in_their_own_department(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """The allow direction every refusal below depends on, and the revoke-all rule end
       to end: catches a handler that revokes only the grant it was NAMED,
       leaving the user holding the role under an audit event saying access
       ended, and a response that reports one id when two rows died.

       Two live rows are planted for one `(user, role)`, which is the state the
       create tool leaves reachable (two requests, two idempotency keys). The audit delta
       is the exact ordered list, so the per-row `grant.revoked` events
    and the pipeline's single summary `tool.succeeded` LAST are
       both pinned; a handler declaring `grant.revoked` as its registry event
       would write one event naming the tool and fail here.
    """
    manager = _seeded(PRODUCTION_MANAGER)
    target = _seeded(HAPPY_TARGET)
    assert "manager" in manager.principals, "premise: the seeded index 0 is the manager"
    assert manager.department_id == target.department_id
    _premise_no_grants(mcp_env.database_url, target.user_id, REVOKED_ROLE)
    named = _plant_grant(mcp_env.database_url, user_id=target.user_id, role_id=REVOKED_ROLE)
    sibling = _plant_grant(mcp_env.database_url, user_id=target.user_id, role_id=REVOKED_ROLE)
    events_before = _audit_event_types(mcp_env.database_url, manager.tenant_id)

    payload = _structured(call_tool(bearer(token_for(manager)), REVOKE, _args(named)))

    assert set(payload["revoked_grant_ids"]) == {str(named), str(sibling)}
    assert payload["revoked_count"] == 2
    for grant_id in (named, sibling):
        row = _grant_row(mcp_env.database_url, grant_id)
        assert row.status == REVOKED_STATUS, grant_id
        assert row.revoked_at is not None
        assert row.revoke_reason is not None
    assert _live_grant_ids(mcp_env.database_url, target.user_id, REVOKED_ROLE) == [], (
        "the role must actually be gone, not merely reported gone"
    )
    events_after = _audit_event_types(mcp_env.database_url, manager.tenant_id)
    assert events_after == [
        *events_before,
        GRANT_REVOKED_EVENT,
        GRANT_REVOKED_EVENT,
        SUCCEEDED_EVENT,
    ]

    records = _revoked_events(mcp_env.database_url, manager.tenant_id)[-2:]
    assert {str(record.subject_type) for record in records} == {"grant"}
    assert {str(record.subject_id) for record in records} == {str(named), str(sibling)}


# --- the matrix's scope, and where it comes from ---------------------


def test_a_manager_may_not_revoke_outside_their_department(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """The scope half: the caller is a `manager`, whom the
    RBAC matrix ADMITS on this row, so the matrix cannot produce this refusal
    and only the `own_department` scope check can. Deleting that check is the
    one change that flips this case.

    Both users are in the same tenant, so the tenant guard
    cannot refuse on the scope rule's behalf, and the grant is LIVE, so the
    liveness check cannot either. `tool.denied` rather than `tool.failed`: a
    permission decision filed as an outage is a false record.
    """
    manager = _seeded(PRODUCTION_MANAGER)
    outsider = _seeded(SCOPE_TARGET)
    assert manager.tenant_id == outsider.tenant_id, "the point of this test"
    assert manager.department_id != outsider.department_id
    _premise_no_grants(mcp_env.database_url, outsider.user_id, REVOKED_ROLE)
    grant_id = _plant_grant(mcp_env.database_url, user_id=outsider.user_id, role_id=REVOKED_ROLE)
    events_before = _audit_event_types(mcp_env.database_url, manager.tenant_id)

    result = call_tool(bearer(token_for(manager)), REVOKE, _args(grant_id))

    assert error_code(result) == "FORBIDDEN"
    row = _grant_row(mcp_env.database_url, grant_id)
    assert row.status == ACTIVE_STATUS
    assert row.revoked_at is None
    assert _live_grant_ids(mcp_env.database_url, outsider.user_id, REVOKED_ROLE) == [grant_id]
    events_after = _audit_event_types(mcp_env.database_url, manager.tenant_id)
    assert events_after == [*events_before, DENIED_EVENT]


def test_the_managers_department_comes_from_the_directory_not_the_token(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches the scope check reading the token's `department_id` claim
    instead of the caller's current directory row.

    The token is minted while the manager is in Production and is NOT
    reissued; the manager's row then moves to Sales. Two live grants are
    planted, one on a Production holder and one on a Sales holder — so an
    implementation trusting the claim revokes the Production one and refuses
    the Sales one, and an implementation reading the directory does the
    opposite. Asserting BOTH directions is what makes the case decisive: in
    the seed the claim and the row always agree, so without the move both
    implementations pass, and a one-sided assertion would also pass against a
    handler that refuses everybody.

    Caller and both holders are in the same tenant.
    """
    manager = _seeded(PRODUCTION_MANAGER)
    token = token_for(manager)
    sales_department_id = ROWS.department_ids["Sales"]
    assert sales_department_id != manager.department_id
    _premise_no_grants(mcp_env.database_url, DIRECTORY_PRODUCTION_TARGET.id, REVOKED_ROLE)
    _premise_no_grants(mcp_env.database_url, DIRECTORY_SALES_TARGET.id, REVOKED_ROLE)
    production_grant = _plant_grant(
        mcp_env.database_url, user_id=DIRECTORY_PRODUCTION_TARGET.id, role_id=REVOKED_ROLE
    )
    sales_grant = _plant_grant(
        mcp_env.database_url, user_id=DIRECTORY_SALES_TARGET.id, role_id=REVOKED_ROLE
    )

    _set_user_department(mcp_env.database_url, manager.user_id, sales_department_id)
    try:
        refused = call_tool(bearer(token), REVOKE, _args(production_grant))
        allowed = call_tool(bearer(token), REVOKE, _args(sales_grant))
    finally:
        _set_user_department(mcp_env.database_url, manager.user_id, manager.department_id)

    assert error_code(refused) == "FORBIDDEN"
    assert _grant_row(mcp_env.database_url, production_grant).status == ACTIVE_STATUS
    assert _structured(allowed)["revoked_grant_ids"] == [str(sales_grant)]
    assert _grant_row(mcp_env.database_url, sales_grant).status == REVOKED_STATUS


def test_a_system_owner_revokes_only_on_systems_they_own(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """The matrix's `own_systems` cell, both directions.

    The token carries `system_owner` and nothing else, so the caller's only
    scope is `own_systems` and the department comparison cannot reach either
    grant: both holders are Production users and the caller is in IT. One
    grant is on a role of the system this caller OWNS and one on a role of a
    system somebody else owns, so an implementation that ignores the owner
    comparison (or reads `Scope.ANY` for any recognised principal) revokes
    both, and one that never matches `own_systems` at all revokes neither.
    """
    owner = _seeded(PROD_K8S_OWNER)
    assert "system_owner" in owner.principals, "premise: the seeded owner carries the principal"
    token = token_for(owner, principals=("system_owner",))
    assert owner.department_id != ROWS.department_ids["Production"]
    _premise_no_grants(mcp_env.database_url, OWNED_TARGET.id, OWNED_SYSTEM_ROLE)
    _premise_no_grants(mcp_env.database_url, UNOWNED_TARGET.id, UNOWNED_ROLE)
    owned_grant = _plant_grant(
        mcp_env.database_url, user_id=OWNED_TARGET.id, role_id=OWNED_SYSTEM_ROLE
    )
    unowned_grant = _plant_grant(
        mcp_env.database_url, user_id=UNOWNED_TARGET.id, role_id=UNOWNED_ROLE
    )

    allowed = call_tool(bearer(token), REVOKE, _args(owned_grant))
    refused = call_tool(bearer(token), REVOKE, _args(unowned_grant))

    assert _structured(allowed)["revoked_grant_ids"] == [str(owned_grant)]
    assert _grant_row(mcp_env.database_url, owned_grant).status == REVOKED_STATUS
    assert error_code(refused) == "FORBIDDEN"
    assert _grant_row(mcp_env.database_url, unowned_grant).status == ACTIVE_STATUS


def test_a_denied_call_writes_tool_denied_and_revokes_nothing(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """The caller holds `employee`, which the matrix's revoke row gives NO cell at
    all, so the pipeline's matrix stage refuses them.

    NOT a standing-rule-16 kill of that stage, and deliberately not written as
    one: `_grant_in_scope` opens with `rbac.authorize`, which RAISES for a
    principal with no cell, so deleting the pipeline's RBAC stage leaves this
    answer exactly as it is. On this the matrix row no principal exists that the
    scope check admits and the matrix refuses, so the two guards cannot be
    killed independently. What this case does pin is that a principal with no
    cell is refused before any effect and that the refusal is recorded as
    `tool.denied` rather than `tool.failed` — a permission decision filed as an
    outage is a false record — and that no `grant.revoked` is written. The
    scope comparison has its own kill in
    `test_a_manager_may_not_revoke_outside_their_department`, whose caller the
    matrix admits.
    """
    caller = _seeded(DENIED_TARGET)
    # The grant is the caller's OWN, so the department comparison the scope
    # check makes is a user against themselves: it cannot refuse this call.
    _premise_no_grants(mcp_env.database_url, DENIED_TARGET.id, REVOKED_ROLE)
    grant_id = _plant_grant(mcp_env.database_url, user_id=DENIED_TARGET.id, role_id=REVOKED_ROLE)
    events_before = _audit_event_types(mcp_env.database_url, caller.tenant_id)

    result = call_tool(bearer(token_for(caller, principals=("employee",))), REVOKE, _args(grant_id))

    assert error_code(result) == "FORBIDDEN"
    assert _grant_row(mcp_env.database_url, grant_id).status == ACTIVE_STATUS
    events_after = _audit_event_types(mcp_env.database_url, caller.tenant_id)
    assert events_after == [*events_before, DENIED_EVENT]


# --- the two absences and the two states ---------------------


def test_another_tenants_grant_and_a_missing_one_answer_the_same_way(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches a lookup that escapes the tenant guard, and one that
    distinguishes the two absences: if another tenant's id answered
    `FORBIDDEN` (or `POLICY_BLOCKED`) while an unknown id answered
    `NOT_FOUND`, the seam would be an id oracle — a manager could probe which
    grant ids are live in a tenant they cannot see. The other tenant's row is
    re-read afterwards, so a cross-tenant revocation that answered `NOT_FOUND`
    and still revoked would fail here.
    """
    manager = _seeded(PRODUCTION_MANAGER)
    theirs = _plant_grant(
        mcp_env.database_url,
        user_id=OTHER_TENANT_TARGET.id,
        role_id=OTHER_TENANT_ROLE,
        tenant_id=OTHER_TENANT_ROWS.tenant_id,
    )

    cross_tenant = call_tool(bearer(token_for(manager)), REVOKE, _args(theirs))
    missing = call_tool(bearer(token_for(manager)), REVOKE, _args(uuid.uuid4()))

    assert error_code(cross_tenant) == "NOT_FOUND"
    assert error_code(missing) == "NOT_FOUND"
    row = _grant_row(mcp_env.database_url, theirs)
    assert row.status == ACTIVE_STATUS
    assert row.revoked_at is None


def test_an_already_revoked_grant_is_refused_policy_blocked(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Both halves.

    In scope: the caller is the target's own manager and the grant exists, so
    neither the matrix, the scope check nor the lookup can refuse — only the
    liveness test can, and `POLICY_BLOCKED` rather than `NOT_FOUND` is what
    tells a caller who legitimately reached the grant that it is already gone
    instead of lying that it never existed.

    Out of scope: the SAME not-live state on a grant the caller's scope does
    not reach must answer `FORBIDDEN`, because the scope check runs FIRST. A
    handler that checks liveness first turns the id into a state oracle —
    anyone could learn whether a stranger's grant is live by reading which of
    the two codes comes back — and that ordering bug is invisible to the
    in-scope half alone.
    """
    manager = _seeded(PRODUCTION_MANAGER)
    _premise_no_grants(mcp_env.database_url, NOT_LIVE_TARGET.id, REVOKED_ROLE)
    _premise_no_grants(mcp_env.database_url, NOT_LIVE_OUT_OF_SCOPE_TARGET.id, REVOKED_ROLE)
    in_scope = _plant_grant(
        mcp_env.database_url,
        user_id=NOT_LIVE_TARGET.id,
        role_id=REVOKED_ROLE,
        revoked_at=HISTORIC_REVOKED_AT,
        revoke_reason=HISTORIC_REASON,
        status=REVOKED_STATUS,
    )
    out_of_scope = _plant_grant(
        mcp_env.database_url,
        user_id=NOT_LIVE_OUT_OF_SCOPE_TARGET.id,
        role_id=REVOKED_ROLE,
        revoked_at=HISTORIC_REVOKED_AT,
        revoke_reason=HISTORIC_REASON,
        status=REVOKED_STATUS,
    )

    blocked = call_tool(bearer(token_for(manager)), REVOKE, _args(in_scope))
    forbidden = call_tool(bearer(token_for(manager)), REVOKE, _args(out_of_scope))

    assert error_code(blocked) == "POLICY_BLOCKED"
    assert error_code(forbidden) == "FORBIDDEN"
    for grant_id in (in_scope, out_of_scope):
        row = _grant_row(mcp_env.database_url, grant_id)
        assert row.revoked_at == HISTORIC_REVOKED_AT, "a refusal rewrites nothing"
        assert row.revoke_reason == HISTORIC_REASON


# --- the input surface --------------------


@pytest.mark.parametrize(
    ("index", "length", "accepted"),
    [
        pytest.param(0, 1, True, id="reason-1-ok"),
        pytest.param(1, REASON_MAX_LENGTH, True, id="reason-2000-ok"),
        pytest.param(2, 0, False, id="reason-empty-refused"),
        pytest.param(3, REASON_MAX_LENGTH + 1, False, id="reason-2001-refused"),
    ],
)
def test_the_reason_boundary_is_inclusive_at_1_and_2000(
    mcp_env: SeededMcp,  # noqa: F811
    index: int,
    length: int,
    accepted: bool,
) -> None:
    """Both ends, both directions, against the exact number.

    `grants.revoke_reason` is an unbounded `Text` column, so this Pydantic
    bound is the only backstop there is: without it 2001 characters are
    stored, not rejected. The lower bound is 1 and not 0
    because a revocation with an empty reason is an access removal nobody can
    audit. A one-sided assertion cannot tell an inclusive bound from an
    exclusive one, and the refused rows are re-read to prove the check runs
    BEFORE any effect.
    """
    from helpdesk.mcp_server.tools.access import REASON_MAX_LENGTH as WIRED_MAX_LENGTH

    assert WIRED_MAX_LENGTH == REASON_MAX_LENGTH == 2000

    manager = _seeded(PRODUCTION_MANAGER)
    target = REASON_TARGETS[index]
    _premise_no_grants(mcp_env.database_url, target.id, REVOKED_ROLE)
    grant_id = _plant_grant(mcp_env.database_url, user_id=target.id, role_id=REVOKED_ROLE)

    result = call_tool(
        bearer(token_for(manager)),
        REVOKE,
        {"grant_id": str(grant_id), "reason": "r" * length, "idempotency_key": _key()},
    )

    row = _grant_row(mcp_env.database_url, grant_id)
    if accepted:
        assert result.is_error is False, result.content
        assert row.status == REVOKED_STATUS
        assert row.revoke_reason == "r" * length
    else:
        assert error_code(result) == "VALIDATION"
        assert row.status == ACTIVE_STATUS
        assert row.revoked_at is None


def test_an_unknown_field_is_rejected(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches an input model that ignores extras instead of forbidding them
    (the read tools' contract, wrong for a mutation): a `user_id` in the
    arguments is a caller who believes they are revoking a colleague's access
    by name, and dropping it silently would revoke the grant the id points at
    while the caller reads success as having named the colleague.

    Both users are in the SAME tenant, so the tenant guard
    cannot produce this refusal on the model's behalf, and the caller is
    permitted and in scope, so nothing but `extra="forbid"` can.
    """
    manager = _seeded(PRODUCTION_MANAGER)
    colleague = _seeded(HAPPY_TARGET)
    assert manager.tenant_id == colleague.tenant_id, "the point of this test"
    _premise_no_grants(mcp_env.database_url, EXTRA_ARG_TARGET.id, REVOKED_ROLE)
    grant_id = _plant_grant(mcp_env.database_url, user_id=EXTRA_ARG_TARGET.id, role_id=REVOKED_ROLE)

    result = call_tool(
        bearer(token_for(manager)), REVOKE, _args(grant_id, user_id=str(colleague.user_id))
    )

    assert error_code(result) == "VALIDATION"
    row = _grant_row(mcp_env.database_url, grant_id)
    assert row.status == ACTIVE_STATUS
    assert row.revoked_at is None


# --- replay and concurrency (and the race) -----------------


def test_a_replay_returns_the_stored_response_and_writes_only_tool_replayed(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches a retry that re-runs the body.

    The second call under the same key must not re-read the live set: by then
    the row is revoked, so a re-run would answer `POLICY_BLOCKED` for a call
    the caller already saw succeed, and if a NEW grant on that pair had been
    executed in between it would revoke that one too. Byte identity is
    asserted, not just equality of meaning, so a response rebuilt rather than
    replayed fails; the audit delta is exactly `tool.replayed`, so a second
    `grant.revoked` — a false record of a revocation that never happened —
    fails as well.
    """
    manager = _seeded(PRODUCTION_MANAGER)
    _premise_no_grants(mcp_env.database_url, REPLAY_TARGET.id, REVOKED_ROLE)
    grant_id = _plant_grant(mcp_env.database_url, user_id=REPLAY_TARGET.id, role_id=REVOKED_ROLE)
    arguments = _args(grant_id)

    first = call_tool(bearer(token_for(manager)), REVOKE, arguments)
    events_between = _audit_event_types(mcp_env.database_url, manager.tenant_id)
    revoked_between = len(_revoked_events(mcp_env.database_url, manager.tenant_id))
    second = call_tool(bearer(token_for(manager)), REVOKE, arguments)

    assert _structured(first)["revoked_count"] == 1
    assert _response_text(second) == _response_text(first)
    assert second.structured_content == first.structured_content
    assert len(_revoked_events(mcp_env.database_url, manager.tenant_id)) == revoked_between
    events_after = _audit_event_types(mcp_env.database_url, manager.tenant_id)
    assert events_after == [*events_between, REPLAYED_EVENT]


def test_two_callers_racing_the_same_revocation_write_one_event_per_row(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """The lock. Two different callers send two different
    idempotency keys, so the idempotency guard cannot serialise them at all;
    only the `SELECT … FOR UPDATE` on the live set can.

    Catches its absence: without the lock both calls read the same two live
    rows, both revoke them, and the audit log carries FOUR `grant.revoked`
    events for two grants — one revocation recorded twice, which is exactly
    the record an auditor cannot reconcile. Both callers are seeded
    `it_admin`s, whose the matrix scope is `any`, so both are genuinely entitled
    and the race is real; the loser is refused `POLICY_BLOCKED` because by
    then the live set is empty, never `FORBIDDEN`.
    """
    first_admin = _seeded(IT_ADMIN_ONE)
    second_admin = _seeded(IT_ADMIN_TWO)
    assert first_admin.user_id != second_admin.user_id
    assert "it_admin" in first_admin.principals and "it_admin" in second_admin.principals
    _premise_no_grants(mcp_env.database_url, RACE_TARGET.id, REVOKED_ROLE)
    named = _plant_grant(mcp_env.database_url, user_id=RACE_TARGET.id, role_id=REVOKED_ROLE)
    sibling = _plant_grant(mcp_env.database_url, user_id=RACE_TARGET.id, role_id=REVOKED_ROLE)
    revoked_before = len(_revoked_events(mcp_env.database_url, first_admin.tenant_id))

    def revoke(caller: SeededUser) -> Any:
        return call_tool(bearer(token_for(caller)), REVOKE, _args(named))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(revoke, first_admin), pool.submit(revoke, second_admin)]
        results = [future.result() for future in futures]

    successes = [result for result in results if result.is_error is False]
    refusals = [result for result in results if result.is_error is True]
    assert len(successes) == 1, [result.content for result in results]
    assert len(refusals) == 1
    assert error_code(refusals[0]) == "POLICY_BLOCKED"
    assert set(_structured(successes[0])["revoked_grant_ids"]) == {str(named), str(sibling)}
    written = len(_revoked_events(mcp_env.database_url, first_admin.tenant_id)) - revoked_before
    assert written == 2, "exactly one grant.revoked per revoked row, never one per caller"
