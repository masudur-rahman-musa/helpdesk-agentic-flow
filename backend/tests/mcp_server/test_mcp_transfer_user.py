"""An earlier change, sections 1 and 6: `transfer_user`, through the transport.

Surface under test (written before it exists):

``mcp_server.tools.lifecycle``
    ``TransferUserInput(MutatingToolInput)`` — ``user_id: uuid.UUID``,
    ``new_department_id: uuid.UUID``, ``effective_date: date``, plus the
    idempotency key and ``extra="forbid"`` from ``MutatingToolInput``. There
    is no tenant argument: the tenant is the token's.
    ``TransferUserOutput`` — the diff, ``revoked_role_ids``,
    ``applied_role_ids``, ``skipped``, ``retained_role_ids`` and
    ``non_standard_role_ids``.
    ``transfer_user`` — a registered MUTATING tool under the wire name
    ``transfer_user``, whose registry ``event_type`` is the generic
    ``tool.succeeded`` (decision C): the handler appends ``user.transferred``
    itself, naming the USER, so the pipeline's event lands last as the
    summary.

Refusals: ``FORBIDDEN`` (+ ``tool.denied``) for the four principals the design
gives no cell on this row, one shared ``NOT_FOUND`` for a ``user_id`` or a
``new_department_id`` that is missing OR another tenant's, ``POLICY_BLOCKED``
for a move into the department the person is already in (decision E), and
``VALIDATION`` for every input the pre-checks refuse — all before any write.

Audit deltas are asserted as exact ordered event-type lists against a before
snapshot, never as counts: a missing domain event, a duplicated one, or a
denial filed as an outage is invisible to a count.

Fixture cost: everything rides an earlier change's session-scoped
``mcp_fixtures`` seam — one seeded throwaway database, one dedicated Redis
index flushed per test. Per-test isolation inside the shared database is a
namespace: every case plants its OWN two departments (fresh uuid names), its
OWN mover (a fresh uuid4 address) and its own grants, and sends a fresh
idempotency key per call. Seeded identities are recomputed from
``scripts.seed.generate_tenant`` — deterministic uuid5 ids, never hardcoded
UUIDs.

No pytest-asyncio: each case is a sync function driving one ``asyncio.run``
(the concurrency case drives two, one per thread).
"""

import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
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

TRANSFER = "transfer_user"

# the design's own name for the move, appended by the handler (decision C),
# and the pipeline's own events. Literals rather than imports: these strings
# are the audit contract, and reading them from the module under test would
# let a rename pass unnoticed.
USER_TRANSFERRED_EVENT = "user.transferred"
GRANT_REVOKED_EVENT = "grant.revoked"
GRANT_EXECUTED_EVENT = "grant.executed"
SUCCEEDED_EVENT = "tool.succeeded"
DENIED_EVENT = "tool.denied"
REPLAYED_EVENT = "tool.replayed"

ROWS = generate_tenant(NORDLICHT)
OTHER_TENANT_ROWS = generate_tenant(CUAN)

# the design's `transfer_user` row: three principals may call it, four may not.
# All three hold `Scope.ANY`, so there is no argument-dependent second check.
ALLOWED_PRINCIPALS = ("hr", "it_admin", "platform_admin")
DENIED_PRINCIPALS = ("employee", "manager", "system_owner", "auditor")

# The callers. Read only — no case writes to these rows — so they cannot
# collide with another module's namespace. The principal is always forced on
# the token, so a case pins the the design cell rather than whatever the seed
# happened to give this user.
CALLER = ROWS.users_by_department["HR"][8]
PLANTED_MANAGER = ROWS.users_by_department["HR"][9]
# Two distinct seeded `it_admin`s for the race: two real callers, two tokens,
# two idempotency keys, so nothing but the row lock can serialise them.
IT_ADMIN_ONE = ROWS.users_by_department["IT"][16]
IT_ADMIN_TWO = ROWS.users_by_department["IT"][17]

# The roles the planted templates are built from. None is named by a seeded
# SoD rule, so nothing here is skipped by accident.
OLD_ONLY_ROLE = ROWS.role_ids[("mes", "viewer")]
NEW_ONLY_ROLE = ROWS.role_ids[("grafana", "viewer")]

# The second seeded tenant: all these ids have to be is real somewhere else.
OTHER_TENANT_USER = OTHER_TENANT_ROWS.users_by_department["Operations"][6]
OTHER_TENANT_DEPARTMENT = OTHER_TENANT_ROWS.department_ids["Operations"]

EFFECTIVE_DATE = "2026-09-15"
TENURED_START = date(2019, 3, 7)
ACTIVE_STATUS = "active"
LEAVER_STATUS = "leaver"
GRANTED_AT = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
PLANTED_EXPIRY = datetime(2099, 6, 1, 12, 0, tzinfo=UTC)


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
        status=ACTIVE_STATUS,
    )


def _key() -> str:
    """A unique idempotency key: half the per-test namespace."""
    return f"fake-key-{uuid.uuid4().hex}"


def _args(
    user_id: uuid.UUID | str,
    new_department_id: uuid.UUID | str,
    *,
    effective_date: str = EFFECTIVE_DATE,
    idempotency_key: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Minimally valid arguments for one move."""
    return {
        "user_id": str(user_id),
        "new_department_id": str(new_department_id),
        "effective_date": effective_date,
        "idempotency_key": _key() if idempotency_key is None else idempotency_key,
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


def _plant_department(
    database_url: str,
    *,
    role_ids: tuple[uuid.UUID, ...] = (),
    tenant_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """One committed department owning exactly `role_ids` as its template."""
    department_id = uuid.uuid4()
    _execute(
        database_url,
        "INSERT INTO departments (id, tenant_id, name, manager_user_id, created_at, updated_at)"
        " VALUES (:id, :tenant, :name, :manager, now(), now())",
        {
            "id": department_id,
            "tenant": tenant_id or ROWS.tenant_id,
            "name": f"Fake Transfer Dept {uuid.uuid4().hex[:12]}",
            "manager": PLANTED_MANAGER.id if tenant_id is None else None,
        },
    )
    for role_id in role_ids:
        _execute(
            database_url,
            "INSERT INTO role_templates (id, tenant_id, department_id, role_id,"
            " created_at, updated_at)"
            " VALUES (:id, :tenant, :department, :role, now(), now())",
            {
                "id": uuid.uuid4(),
                "tenant": tenant_id or ROWS.tenant_id,
                "department": department_id,
                "role": role_id,
            },
        )
    return department_id


def _plant_user(
    database_url: str,
    *,
    department_id: uuid.UUID | None,
    status: str = ACTIVE_STATUS,
) -> uuid.UUID:
    """One committed, obviously fake mover in the seeded tenant."""
    user_id = uuid.uuid4()
    _execute(
        database_url,
        "INSERT INTO users (id, tenant_id, department_id, email, display_name, password_hash,"
        " principals, status, start_date, end_date, created_at, updated_at)"
        " VALUES (:id, :tenant, :department, :email, :display_name, :password_hash,"
        " :principals, :status, :start_date, NULL, now(), now())",
        {
            "id": user_id,
            "tenant": ROWS.tenant_id,
            "department": department_id,
            "email": f"fake.mover.{uuid.uuid4().hex}@fake.invalid",
            "display_name": "Fake Mover",
            "password_hash": "not-a-real-hash-this-user-never-logs-in",
            "principals": ["employee"],
            "status": status,
            "start_date": TENURED_START,
        },
    )
    return user_id


def _plant_grant(database_url: str, *, user_id: uuid.UUID, role_id: uuid.UUID) -> uuid.UUID:
    """One committed live `grants` row, with an expiry no engine would pick."""
    grant_id = uuid.uuid4()
    _execute(
        database_url,
        "INSERT INTO grants (id, tenant_id, user_id, role_id, request_id, granted_by_user_id,"
        " granted_at, expires_at, revoked_at, revoke_reason, status, created_at, updated_at)"
        " VALUES (:id, :tenant, :user, :role, NULL, NULL, :granted_at, :expires_at, NULL, NULL,"
        " :status, now(), now())",
        {
            "id": grant_id,
            "tenant": ROWS.tenant_id,
            "user": user_id,
            "role": role_id,
            "granted_at": GRANTED_AT,
            "expires_at": PLANTED_EXPIRY,
            "status": ACTIVE_STATUS,
        },
    )
    return grant_id


def _department_of(database_url: str, user_id: uuid.UUID) -> uuid.UUID | None:
    """The user's department, read in EVERY tenant (no tenant filter here)."""
    rows = _rows(database_url, "SELECT department_id FROM users WHERE id = :id", {"id": user_id})
    assert len(rows) == 1, f"expected exactly one users row, got {len(rows)}"
    value = rows[0][0]
    return None if value is None else uuid.UUID(str(value))


def _live_role_ids(database_url: str, user_id: uuid.UUID) -> set[uuid.UUID]:
    rows = _rows(
        database_url,
        "SELECT role_id FROM grants WHERE user_id = :user AND status = 'active'"
        " AND revoked_at IS NULL",
        {"user": user_id},
    )
    return {uuid.UUID(str(row[0])) for row in rows}


def _grant_rows(database_url: str, user_id: uuid.UUID, role_id: uuid.UUID) -> list[Any]:
    return _rows(
        database_url,
        "SELECT id, status, revoked_at, revoke_reason, expires_at FROM grants"
        " WHERE user_id = :user AND role_id = :role",
        {"user": user_id, "role": role_id},
    )


def _audit_event_types(database_url: str, tenant_id: uuid.UUID) -> list[str]:
    rows = _rows(
        database_url,
        "SELECT event_type FROM audit_events WHERE tenant_id = :tenant ORDER BY seq",
        {"tenant": tenant_id},
    )
    return [str(row[0]) for row in rows]


def _transferred_events(database_url: str, tenant_id: uuid.UUID) -> list[Any]:
    """Every (subject_type, subject_id, payload) written as `user.transferred`."""
    return _rows(
        database_url,
        "SELECT subject_type, subject_id, payload FROM audit_events WHERE tenant_id = :tenant"
        " AND event_type = :event_type ORDER BY seq",
        {"tenant": tenant_id, "event_type": USER_TRANSFERRED_EVENT},
    )


def _plant_move(database_url: str) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """A mover holding the old template's only role, and a new department.

    One old-template role and one new-template role, so a clean transfer is
    exactly one revocation and one application — which is what makes the
    audit sequence below a fixed, countable list.
    """
    old_department = _plant_department(database_url, role_ids=(OLD_ONLY_ROLE,))
    new_department = _plant_department(database_url, role_ids=(NEW_ONLY_ROLE,))
    user_id = _plant_user(database_url, department_id=old_department)
    _plant_grant(database_url, user_id=user_id, role_id=OLD_ONLY_ROLE)
    return user_id, old_department, new_department


# --- the design's allow set and its refusal direction (criterion 10) ---------------


@pytest.mark.parametrize("principal", ALLOWED_PRINCIPALS)
def test_the_three_permitted_principals_each_transfer_somebody(
    mcp_env: SeededMcp,  # noqa: F811
    principal: str,
) -> None:
    """The allow direction every refusal below depends on, one row of the design's
    `transfer_user` cell at a time.

    Catches a registry entry wired to the wrong matrix member (say
    `REVOKE_ACCESS`, whose row admits `manager` and `system_owner` instead):
    the three principals here would then be refused for a tool the spec gives
    them, and the denial case below — which sends the OTHER four — would still
    pass, because that wrong row refuses `employee` and `auditor` too. Only
    running both directions over the same registered tool pins the cell.

    The token carries exactly one principal, so nothing but that principal's
    own cell can admit the call. The mover's row is re-read with no tenant
    filter and asserted to sit in the NEW department of the TOKEN's tenant,
    with no tenant argument anywhere in the input.
    """
    caller = _seeded(CALLER)
    user_id, old_department, new_department = _plant_move(mcp_env.database_url)

    payload = _structured(
        call_tool(
            bearer(token_for(caller, principals=(principal,))),
            TRANSFER,
            _args(user_id, new_department),
        )
    )

    assert _department_of(mcp_env.database_url, user_id) == new_department
    assert new_department != old_department
    assert {uuid.UUID(str(role_id)) for role_id in payload["revoked_role_ids"]} == {OLD_ONLY_ROLE}
    assert {uuid.UUID(str(role_id)) for role_id in payload["applied_role_ids"]} == {NEW_ONLY_ROLE}
    assert payload["skipped"] == []
    assert _live_role_ids(mcp_env.database_url, user_id) == {NEW_ONLY_ROLE}


@pytest.mark.parametrize("principal", DENIED_PRINCIPALS)
def test_the_other_four_principals_are_denied_and_move_nobody(
    mcp_env: SeededMcp,  # noqa: F811
    principal: str,
) -> None:
    """a project rule: the design's matrix row is the ONLY gate on this tool.

    Every argument is valid and both ids resolve in the caller's own tenant,
    so neither the two `NOT_FOUND` lookups, the same-department refusal nor
    the leaver refusal can produce this answer — deleting the pipeline's RBAC
    stage is the one change that flips these four cases from `FORBIDDEN` into
    completed transfers. the design gives all three permitted principals
    `Scope.ANY`, so there is no second, argument-dependent check here whose
    presence would blunt the kill.

    `tool.denied` rather than `tool.failed`: a permission decision filed as an
    outage is a false record. The department and the mover's live roles are
    re-read, so a refusal that still moved somebody fails here.
    """
    caller = _seeded(CALLER)
    user_id, old_department, new_department = _plant_move(mcp_env.database_url)
    events_before = _audit_event_types(mcp_env.database_url, caller.tenant_id)

    result = call_tool(
        bearer(token_for(caller, principals=(principal,))),
        TRANSFER,
        _args(user_id, new_department),
    )

    assert error_code(result) == "FORBIDDEN"
    assert _department_of(mcp_env.database_url, user_id) == old_department
    assert _live_role_ids(mcp_env.database_url, user_id) == {OLD_ONLY_ROLE}
    events_after = _audit_event_types(mcp_env.database_url, caller.tenant_id)
    assert events_after == [*events_before, DENIED_EVENT]


# --- the audit sequence (criterion 5, decision C, a project rule) -------------


def test_the_audit_sequence_is_revoked_then_executed_then_transferred_then_succeeded(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Criterion 5 as an exact ordered list, which is also the only place
    decisions B and C are observable together.

    Catches the registry declaring `user.transferred` as this tool's event
    instead of the generic `tool.succeeded`: the pipeline writes its event
    with the TOOL as subject and a fixed payload, so the log would record that
    somebody called a tool and not who moved, and the
    mover's own event would be missing entirely. Catches apply-before-revoke
    (decision B) — the two grant events would come back swapped — and a
    `user.transferred` written before the grant events, which would have to
    name sets nothing had computed yet. Catches a summary event that is not
    last.

    The subject and the nine payload keys are asserted here as well as in the
    core test, because only the transport can show that the event the
    PIPELINE adds did not overwrite or duplicate the one the handler wrote.
    """
    caller = _seeded(CALLER)
    user_id, old_department, new_department = _plant_move(mcp_env.database_url)
    events_before = _audit_event_types(mcp_env.database_url, caller.tenant_id)

    call_tool(
        bearer(token_for(caller, principals=("hr",))), TRANSFER, _args(user_id, new_department)
    )

    events_after = _audit_event_types(mcp_env.database_url, caller.tenant_id)
    assert events_after == [
        *events_before,
        GRANT_REVOKED_EVENT,
        GRANT_EXECUTED_EVENT,
        USER_TRANSFERRED_EVENT,
        SUCCEEDED_EVENT,
    ]

    record = _transferred_events(mcp_env.database_url, caller.tenant_id)[-1]
    assert str(record.subject_type) == "user"
    assert str(record.subject_id) == str(user_id)
    payload = dict(record.payload)
    assert set(payload) == {
        "user_id",
        "from_department_id",
        "to_department_id",
        "effective_date",
        "revoked_role_ids",
        "applied_role_ids",
        "skipped_role_ids",
        "retained_role_ids",
        "non_standard_role_ids",
    }
    assert payload["from_department_id"] == str(old_department)
    assert payload["to_department_id"] == str(new_department)
    assert payload["effective_date"] == EFFECTIVE_DATE


# --- the two absences, twice over (criterion 8) ---------------------------------


def test_a_missing_and_another_tenants_user_answer_the_same_not_found(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches a user lookup that escapes the tenant guard, and one that
    distinguishes the two absences.

    If another tenant's user answered anything other than the unknown id's
    answer, `user_id` would be a cross-tenant existence oracle: an HR officer
    could enumerate which people exist in a tenant they cannot see. The other
    tenant's user is re-read afterwards and must still sit in their own
    department — a cross-tenant transfer that answered `NOT_FOUND` and still
    moved somebody fails here even though both codes match.
    """
    caller = _seeded(CALLER)
    new_department = _plant_department(mcp_env.database_url, role_ids=(NEW_ONLY_ROLE,))
    foreign_department_before = _department_of(mcp_env.database_url, OTHER_TENANT_USER.id)

    missing = call_tool(
        bearer(token_for(caller, principals=("hr",))),
        TRANSFER,
        _args(uuid.uuid4(), new_department),
    )
    cross_tenant = call_tool(
        bearer(token_for(caller, principals=("hr",))),
        TRANSFER,
        _args(OTHER_TENANT_USER.id, new_department),
    )

    assert error_code(missing) == "NOT_FOUND"
    assert error_code(cross_tenant) == "NOT_FOUND"
    assert _department_of(mcp_env.database_url, OTHER_TENANT_USER.id) == foreign_department_before


def test_a_missing_and_another_tenants_department_answer_the_same_not_found(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """The same oracle argument for the destination, over a REAL foreign row.

    The user is valid in both calls, so only the department lookup can refuse.
    A handler that resolved the department outside the tenant guard would
    happily import a foreign department's id into `users.department_id` — a
    row pointing at another tenant's department, which no later read would
    ever notice — so the mover's own department is asserted unchanged as well
    as the code.
    """
    caller = _seeded(CALLER)
    user_id, old_department, _ = _plant_move(mcp_env.database_url)

    missing = call_tool(
        bearer(token_for(caller, principals=("hr",))), TRANSFER, _args(user_id, uuid.uuid4())
    )
    cross_tenant = call_tool(
        bearer(token_for(caller, principals=("hr",))),
        TRANSFER,
        _args(user_id, OTHER_TENANT_DEPARTMENT),
    )

    assert error_code(missing) == "NOT_FOUND"
    assert error_code(cross_tenant) == "NOT_FOUND"
    assert _department_of(mcp_env.database_url, user_id) == old_department
    assert _live_role_ids(mcp_env.database_url, user_id) == {OLD_ONLY_ROLE}


# --- the two typed refusals through the wire (criteria 6 and 7) ----------------


def test_a_move_into_the_same_department_is_refused_policy_blocked(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Decision E at the boundary: catches the "succeed with an empty diff"
    alternative, which is invisible in the tables.

    The diff IS empty, so nothing about the grants or the department changes
    either way; the only difference is a `user.transferred` recording a move
    that never happened, and the audit delta below is what sees it. The code
    is asserted `POLICY_BLOCKED` and not `VALIDATION`: the arguments are
    perfectly well formed, and calling this a schema problem would send the
    caller to fix the wrong thing.
    """
    caller = _seeded(CALLER)
    user_id, old_department, _ = _plant_move(mcp_env.database_url)
    events_before = _audit_event_types(mcp_env.database_url, caller.tenant_id)

    result = call_tool(
        bearer(token_for(caller, principals=("hr",))), TRANSFER, _args(user_id, old_department)
    )

    assert error_code(result) == "POLICY_BLOCKED"
    assert _department_of(mcp_env.database_url, user_id) == old_department
    assert _live_role_ids(mcp_env.database_url, user_id) == {OLD_ONLY_ROLE}
    assert _audit_event_types(mcp_env.database_url, caller.tenant_id) == [
        *events_before,
        "tool.failed",
    ]


def test_a_leaver_is_refused_policy_blocked_with_their_access_intact(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Decision F through the wire: catches the alternative that lets a leaver
    through and reports every arriving role in `skipped`.

    That alternative looks harmless in the return value and is not: the
    revocations are not policy decisions, so they run, and the leaver's
    remaining access is stripped under an event that says they moved. The
    old-template grant is asserted still live and the department unchanged,
    which is exactly what that alternative breaks; the code is asserted
    `POLICY_BLOCKED` rather than `SOD_CONFLICT` or `VALIDATION`.
    """
    caller = _seeded(CALLER)
    old_department = _plant_department(mcp_env.database_url, role_ids=(OLD_ONLY_ROLE,))
    new_department = _plant_department(mcp_env.database_url, role_ids=(NEW_ONLY_ROLE,))
    user_id = _plant_user(mcp_env.database_url, department_id=old_department, status=LEAVER_STATUS)
    grant_id = _plant_grant(mcp_env.database_url, user_id=user_id, role_id=OLD_ONLY_ROLE)

    result = call_tool(
        bearer(token_for(caller, principals=("hr",))), TRANSFER, _args(user_id, new_department)
    )

    assert error_code(result) == "POLICY_BLOCKED"
    assert _department_of(mcp_env.database_url, user_id) == old_department
    rows = _grant_rows(mcp_env.database_url, user_id, OLD_ONLY_ROLE)
    assert len(rows) == 1
    assert uuid.UUID(str(rows[0].id)) == grant_id
    assert str(rows[0].status) == ACTIVE_STATUS, "a refused transfer strips nothing"
    assert rows[0].revoked_at is None


# --- the input surface (criterion 9) -------------------------------------------


@pytest.mark.parametrize(
    ("user_id", "department", "effective_date", "accepted", "expected_code"),
    [
        pytest.param(None, None, EFFECTIVE_DATE, True, "", id="baseline-ok"),
        pytest.param("not-a-uuid", None, EFFECTIVE_DATE, False, "VALIDATION", id="user-not-uuid"),
        pytest.param("", None, EFFECTIVE_DATE, False, "VALIDATION", id="user-empty"),
        pytest.param(None, "not-a-uuid", EFFECTIVE_DATE, False, "VALIDATION", id="dept-not-uuid"),
        pytest.param(None, "", EFFECTIVE_DATE, False, "VALIDATION", id="dept-empty"),
        pytest.param(None, None, "2026-13-01", False, "VALIDATION", id="date-month-13"),
        pytest.param(None, None, "2026-09-31", False, "VALIDATION", id="date-day-31-sept"),
        pytest.param(None, None, "not-a-date", False, "VALIDATION", id="date-text"),
        pytest.param(None, None, "", False, "VALIDATION", id="date-empty"),
    ],
)
def test_the_input_types_are_refused_before_any_effect(
    mcp_env: SeededMcp,  # noqa: F811
    user_id: str | None,
    department: str | None,
    effective_date: str,
    accepted: bool,
    expected_code: str,
) -> None:
    """Criterion 9, both directions: catches ids typed as `str` and
    `effective_date` typed as `str`.

    A string `effective_date` reaches the `user.transferred` payload verbatim,
    so `"2026-13-01"` and `"2026-09-31"` — a real-looking date with a month
    and a day that do not exist — become permanent audit records nobody can
    parse, and every other case in this file passes because they all send a
    valid one. The two malformed uuids catch the same mistake on the ids,
    where a `str` would be compared against a real uuid and answer `NOT_FOUND`
    instead of `VALIDATION`, telling the caller the row is missing when their
    argument is malformed.

    The baseline row is what stops this table passing vacuously: it proves the
    same arguments minus the one defect go through. Every refused row re-reads
    the mover's department, so the check has to run BEFORE any effect.
    """
    caller = _seeded(CALLER)
    planted_user, old_department, new_department = _plant_move(mcp_env.database_url)

    result = call_tool(
        bearer(token_for(caller, principals=("hr",))),
        TRANSFER,
        _args(
            planted_user if user_id is None else user_id,
            new_department if department is None else department,
            effective_date=effective_date,
        ),
    )

    if accepted:
        assert result.is_error is False, result.content
        assert _department_of(mcp_env.database_url, planted_user) == new_department
    else:
        assert error_code(result) == expected_code
        assert _department_of(mcp_env.database_url, planted_user) == old_department
        assert _live_role_ids(mcp_env.database_url, planted_user) == {OLD_ONLY_ROLE}


def test_an_unknown_field_is_refused_with_no_department_change(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """`MutatingToolInput` forbids extras, and this is the argument that proves
    why: a `tenant_id` in the arguments is a caller who believes they are
    moving somebody inside another tenant.

    Dropping it silently would move the person in the TOKEN's tenant while the
    caller reads success as having acted elsewhere — the one mistake hard rule
    6 exists to make impossible to make quietly. Catches an input model that
    copied `tools/read.py`'s `extra="ignore"` contract: nothing else in this
    call can refuse it, since the caller is permitted and every other argument
    is valid. The department is re-read in BOTH tenants, so a call that
    answered `VALIDATION` and still wrote somewhere fails here.
    """
    caller = _seeded(CALLER)
    user_id, old_department, new_department = _plant_move(mcp_env.database_url)
    foreign_department_before = _department_of(mcp_env.database_url, OTHER_TENANT_USER.id)

    result = call_tool(
        bearer(token_for(caller, principals=("hr",))),
        TRANSFER,
        _args(user_id, new_department, tenant_id=str(OTHER_TENANT_ROWS.tenant_id)),
    )

    assert error_code(result) == "VALIDATION"
    assert _department_of(mcp_env.database_url, user_id) == old_department
    assert _department_of(mcp_env.database_url, OTHER_TENANT_USER.id) == foreign_department_before


# --- replay (criterion 11) ------------------------------------------------------


def test_a_replay_returns_the_stored_response_and_moves_nobody_twice(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches a retry that re-runs the body.

    The second call under the same key must not reach the same-department
    check: by then the person IS in the new department, so a re-run answers
    `POLICY_BLOCKED` for a call the caller already saw succeed. Byte identity
    is asserted, not just equality of meaning, so a response rebuilt rather
    than replayed fails; the audit delta is exactly `tool.replayed`, so a
    second `user.transferred` — a false record of a move that never happened —
    fails too, and the applied role is counted so no second grant row can hide
    behind a matching response.
    """
    caller = _seeded(CALLER)
    user_id, _, new_department = _plant_move(mcp_env.database_url)
    arguments = _args(user_id, new_department, idempotency_key=_key())

    first = call_tool(bearer(token_for(caller, principals=("hr",))), TRANSFER, arguments)
    events_between = _audit_event_types(mcp_env.database_url, caller.tenant_id)
    second = call_tool(bearer(token_for(caller, principals=("hr",))), TRANSFER, arguments)

    assert _structured(first)["applied_role_ids"]
    assert _response_text(second) == _response_text(first)
    assert second.structured_content == first.structured_content
    assert _department_of(mcp_env.database_url, user_id) == new_department
    assert len(_grant_rows(mcp_env.database_url, user_id, NEW_ONLY_ROLE)) == 1
    assert _audit_event_types(mcp_env.database_url, caller.tenant_id) == [
        *events_between,
        REPLAYED_EVENT,
    ]


# --- the lock (decision D) ------------------------------------------------------


def test_two_callers_racing_the_same_transfer_leave_one_move(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Decision D, the `lock_user` row lock. Two different callers send two
    different idempotency keys, so the guard cannot serialise them at all;
    only `SELECT … FOR UPDATE` on the `users` row can.

    The mover is planted holding NOTHING, and the old department's template is
    empty, so there is no revocation and therefore no second control that
    could refuse the loser: without the lock both calls read the old
    department, both write the new one, both apply the arriving role, and the
    log carries TWO `user.transferred` events and the person holds two live
    grants for one role. That is the state this asserts against — one success,
    one `POLICY_BLOCKED` from decision E (the loser now sees the winner's
    department), one event, one grant row.

    Both callers are seeded `it_admin`s, whose the design scope is `any`, so both
    are genuinely entitled and the race is real; the refusal is never
    `FORBIDDEN`.
    """
    first_admin = _seeded(IT_ADMIN_ONE)
    second_admin = _seeded(IT_ADMIN_TWO)
    assert first_admin.user_id != second_admin.user_id
    assert "it_admin" in first_admin.principals and "it_admin" in second_admin.principals
    old_department = _plant_department(mcp_env.database_url)
    new_department = _plant_department(mcp_env.database_url, role_ids=(NEW_ONLY_ROLE,))
    user_id = _plant_user(mcp_env.database_url, department_id=old_department)
    transferred_before = len(_transferred_events(mcp_env.database_url, first_admin.tenant_id))

    def transfer(caller: SeededUser) -> Any:
        return call_tool(bearer(token_for(caller)), TRANSFER, _args(user_id, new_department))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(transfer, first_admin), pool.submit(transfer, second_admin)]
        results = [future.result() for future in futures]

    successes = [result for result in results if result.is_error is False]
    refusals = [result for result in results if result.is_error is True]
    assert len(successes) == 1, [result.content for result in results]
    assert len(refusals) == 1
    assert error_code(refusals[0]) == "POLICY_BLOCKED"
    assert _department_of(mcp_env.database_url, user_id) == new_department
    assert len(_grant_rows(mcp_env.database_url, user_id, NEW_ONLY_ROLE)) == 1, (
        "one arriving grant, never one per caller"
    )
    written = (
        len(_transferred_events(mcp_env.database_url, first_admin.tenant_id)) - transferred_before
    )
    assert written == 1, "exactly one user.transferred, never one per caller"
