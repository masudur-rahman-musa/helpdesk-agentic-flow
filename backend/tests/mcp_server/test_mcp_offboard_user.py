"""`offboard_user`, through the transport.

Surface under test (written before it exists):

``mcp_server.tools.lifecycle``
    ``OffboardUserInput(MutatingToolInput)`` — ``user_id: uuid.UUID`` and
    ``end_date: date``, plus the idempotency key and ``extra="forbid"`` from
    ``MutatingToolInput``. There is no tenant argument: the tenant is the
    token's.
    ``OffboardUserOutput`` — ``revoked_role_ids``, ``revoked_grant_count``
    and ``closed_request_ids``.
    ``offboard_user`` — a registered MUTATING tool under the wire name
    ``offboard_user``, whose registry ``event_type`` is the generic
    ``tool.succeeded``: the handler appends ``user.offboarded`` itself,
    naming the USER, so the pipeline's event lands last as the summary.

Refusals: ``FORBIDDEN`` (+ ``tool.denied``) for the FIVE principals the matrix
gives no cell on this row — including ``it_admin``, which holds a cell on
`onboard_user` and `transfer_user` and none here — one shared ``NOT_FOUND``
for a ``user_id`` that is missing OR another tenant's, ``POLICY_BLOCKED`` for
a target who is already a leaver, and ``VALIDATION`` for every
input the pre-checks refuse, all before any effect.

Audit deltas are asserted as exact ordered event-type lists against a before
snapshot, never as counts: a missing domain event, a duplicated one, or a
denial filed as an outage is invisible to a count.

Fixture cost: everything rides the session-scoped
``mcp_fixtures`` seam — one seeded throwaway database, one dedicated Redis
index flushed per test. Per-test isolation inside the shared database is a
namespace: every case plants its OWN leaver (a fresh uuid4 address), their
own grants and their own requests, and sends a fresh idempotency key per
call. Seeded identities are recomputed from ``scripts.seed.generate_tenant``
— deterministic uuid5 ids, never hardcoded UUIDs.

Every raw-SQL fixture below binds `date` and `datetime` OBJECTS, never
strings: asyncpg binds by type and does not coerce, so a string there kills
the whole file with a `DataError` that looks exactly like the red a
tests-first file is supposed to show.

No pytest-asyncio: each case is a sync function driving one ``asyncio.run``
(the concurrency case drives two, one per thread).
"""

import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import create_async_engine

from helpdesk.shared.models import AccessRequest, RequestStatus
from scripts.seed import CUAN, NORDLICHT, UserRow, generate_tenant
from tests.support.mcp import (
    SeededMcp,
    SeededUser,
    bearer,
    call_tool,
    error_code,
    token_for,
)

OFFBOARD = "offboard_user"

# The audit log's own name for the departure, appended by the handler, the
# closure event, and the pipeline's own events. Literals rather than imports:
# these strings are the audit contract, and reading them from the module under
# test would let a rename pass unnoticed.
USER_OFFBOARDED_EVENT = "user.offboarded"
REQUEST_REJECTED_EVENT = "request.rejected"
GRANT_REVOKED_EVENT = "grant.revoked"
SUCCEEDED_EVENT = "tool.succeeded"
DENIED_EVENT = "tool.denied"
REPLAYED_EVENT = "tool.replayed"
FAILED_EVENT = "tool.failed"

ROWS = generate_tenant(NORDLICHT)
OTHER_TENANT_ROWS = generate_tenant(CUAN)

# The matrix's `offboard_user` row: TWO principals may call it, five may not.
# `it_admin` is on the denied side deliberately — the matrix gives it `onboard_user`
# and `transfer_user` and no cell here, so a row copied from either of those
# by habit fails the denial sweep.
ALLOWED_PRINCIPALS = ("hr", "platform_admin")
DENIED_PRINCIPALS = ("employee", "manager", "system_owner", "it_admin", "auditor")

# The callers. Read only — no case writes to these rows — so they cannot
# collide with another module's namespace. The principal is always forced on
# the token, so a case pins the matrix cell rather than whatever the seed
# happened to give this user.
CALLER = ROWS.users_by_department["HR"][10]
# Two distinct seeded users for the race: two real callers, two tokens, two
# idempotency keys, so nothing but the row lock can serialise them.
HR_ONE = ROWS.users_by_department["HR"][11]
HR_TWO = ROWS.users_by_department["HR"][12]

# The roles a planted leaver holds. Neither is named by a seeded SoD rule and
# the planted leaver has no department, so the only grants any case sees are
# the ones it planted.
ROLE_ONE = ROWS.role_ids[("mes", "viewer")]
ROLE_TWO = ROWS.role_ids[("grafana", "viewer")]

# The second seeded tenant: all this user has to be is real somewhere else.
OTHER_TENANT_USER = OTHER_TENANT_ROWS.users_by_department["Operations"][6]

# The wire argument really is a JSON string; every fixture value below is a
# Python object.
END_DATE = "2026-10-31"
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
    *,
    end_date: str = END_DATE,
    idempotency_key: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Minimally valid arguments for one offboard."""
    return {
        "user_id": str(user_id),
        "end_date": end_date,
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


def _plant_user(database_url: str, *, status: str = ACTIVE_STATUS) -> uuid.UUID:
    """One committed, obviously fake leaver in the seeded tenant, no department."""
    user_id = uuid.uuid4()
    _execute(
        database_url,
        "INSERT INTO users (id, tenant_id, department_id, email, display_name, password_hash,"
        " principals, status, start_date, end_date, created_at, updated_at)"
        " VALUES (:id, :tenant, NULL, :email, :display_name, :password_hash,"
        " :principals, :status, :start_date, NULL, now(), now())",
        {
            "id": user_id,
            "tenant": ROWS.tenant_id,
            "email": f"fake.leaver.{uuid.uuid4().hex}@fake.invalid",
            "display_name": "Fake Leaver",
            "password_hash": "not-a-real-hash-this-user-never-logs-in",
            "principals": ["employee"],
            "status": status,
            # A `date` object, never the string: asyncpg binds by type.
            "start_date": TENURED_START,
        },
    )
    return user_id


def _plant_grant(database_url: str, *, user_id: uuid.UUID, role_id: uuid.UUID) -> uuid.UUID:
    """One committed live `grants` row, with an expiry far ahead of the call."""
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
            # `datetime` objects, never strings (same reason as above).
            "granted_at": GRANTED_AT,
            "expires_at": PLANTED_EXPIRY,
            "status": ACTIVE_STATUS,
        },
    )
    return grant_id


def _plant_request(
    database_url: str,
    *,
    requester_user_id: uuid.UUID,
    target_user_id: uuid.UUID,
    role_id: uuid.UUID,
    status: RequestStatus,
) -> uuid.UUID:
    """One committed `access_requests` row.

    Written through SQLAlchemy Core rather than raw text like the two above:
    the row has JSONB and `ARRAY(Uuid)` columns, which asyncpg cannot bind
    from Python lists without the type information the model carries.
    """
    request_id = uuid.uuid4()

    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    insert(AccessRequest).values(
                        id=request_id,
                        tenant_id=ROWS.tenant_id,
                        number=request_id.int % 2_000_000_000
                        + 1,  # unique per row without a counter
                        requester_user_id=requester_user_id,
                        target_user_id=target_user_id,
                        role_id=role_id,
                        justification=f"fake offboard fixture request {uuid.uuid4().hex}",
                        requested_expiry_days=None,
                        risk_class="critical",
                        decision="approval",
                        decision_reasons=[],
                        citations=[],
                        status=status.value,
                        approver_user_ids=[],
                        approvals=[],
                        session_id=None,
                        trace_id=None,
                        idempotency_key=None,
                    )
                )
        finally:
            await engine.dispose()

    asyncio.run(scenario())
    return request_id


def _user_row(database_url: str, user_id: uuid.UUID) -> Any:
    """The user's status and end date, read in EVERY tenant (no filter here)."""
    rows = _rows(database_url, "SELECT status, end_date FROM users WHERE id = :id", {"id": user_id})
    assert len(rows) == 1, f"expected exactly one users row, got {len(rows)}"
    return rows[0]


def _live_role_ids(database_url: str, user_id: uuid.UUID) -> set[uuid.UUID]:
    rows = _rows(
        database_url,
        "SELECT role_id FROM grants WHERE user_id = :user AND status = 'active'"
        " AND revoked_at IS NULL",
        {"user": user_id},
    )
    return {uuid.UUID(str(row[0])) for row in rows}


def _grant_rows(database_url: str, user_id: uuid.UUID) -> list[Any]:
    return _rows(
        database_url,
        "SELECT id, role_id, status, revoked_at, revoke_reason FROM grants WHERE user_id = :user",
        {"user": user_id},
    )


def _request_status(database_url: str, request_id: uuid.UUID) -> str:
    rows = _rows(
        database_url, "SELECT status FROM access_requests WHERE id = :id", {"id": request_id}
    )
    assert len(rows) == 1
    return str(rows[0][0])


def _audit_event_types(database_url: str, tenant_id: uuid.UUID) -> list[str]:
    rows = _rows(
        database_url,
        "SELECT event_type FROM audit_events WHERE tenant_id = :tenant ORDER BY seq",
        {"tenant": tenant_id},
    )
    return [str(row[0]) for row in rows]


def _offboarded_events(database_url: str, tenant_id: uuid.UUID) -> list[Any]:
    """Every (subject_type, subject_id, payload) written as `user.offboarded`."""
    return _rows(
        database_url,
        "SELECT subject_type, subject_id, payload FROM audit_events WHERE tenant_id = :tenant"
        " AND event_type = :event_type ORDER BY seq",
        {"tenant": tenant_id, "event_type": USER_OFFBOARDED_EVENT},
    )


def _plant_leaver(database_url: str) -> tuple[uuid.UUID, uuid.UUID]:
    """A person holding ONE role with ONE open request targeting them.

    One of each, so a clean offboard is exactly one revocation and one
    closure — which is what makes the audit sequence below a fixed, countable
    list.
    """
    user_id = _plant_user(database_url)
    _plant_grant(database_url, user_id=user_id, role_id=ROLE_ONE)
    request_id = _plant_request(
        database_url,
        requester_user_id=user_id,
        target_user_id=user_id,
        role_id=ROLE_TWO,
        status=RequestStatus.PENDING,
    )
    return user_id, request_id


# --- the matrix's allow set and its refusal direction ---------------


@pytest.mark.parametrize("principal", ALLOWED_PRINCIPALS)
def test_the_two_permitted_principals_each_offboard_somebody(
    mcp_env: SeededMcp,
    principal: str,
) -> None:
    """The allow direction every refusal below depends on, one row of the matrix's
    `offboard_user` cell at a time.

    Catches a registry entry wired to the wrong matrix member (say
    `TRANSFER_USER`, whose row admits `it_admin` as well): the denial case
    below sends `it_admin` among the five, so a wrong row that ALSO admits
    `hr` and `platform_admin` would pass this half and fail there — and a row
    narrower than the matrix fails here. Only running both directions over the
    same registered tool pins the cell.

    The token carries exactly one principal, so nothing but that principal's
    own cell can admit the call. The leaver's row is re-read with no tenant
    filter and asserted `leaver` in the TOKEN's tenant, with no tenant
    argument anywhere in the input.
    """
    caller = _seeded(CALLER)
    user_id, request_id = _plant_leaver(mcp_env.database_url)

    payload = _structured(
        call_tool(bearer(token_for(caller, principals=(principal,))), OFFBOARD, _args(user_id))
    )

    status, end_date = _user_row(mcp_env.database_url, user_id)
    assert str(status) == LEAVER_STATUS
    assert str(end_date) == END_DATE
    assert {uuid.UUID(str(role_id)) for role_id in payload["revoked_role_ids"]} == {ROLE_ONE}
    assert payload["revoked_grant_count"] == 1
    assert {uuid.UUID(str(value)) for value in payload["closed_request_ids"]} == {request_id}
    assert _live_role_ids(mcp_env.database_url, user_id) == set()
    assert _request_status(mcp_env.database_url, request_id) == RequestStatus.REJECTED.value


@pytest.mark.parametrize("principal", DENIED_PRINCIPALS)
def test_the_other_five_principals_are_denied_and_offboard_nobody(
    mcp_env: SeededMcp,
    principal: str,
) -> None:
    """The matrix row is the ONLY gate on this tool.

    Every argument is valid and the `user_id` resolves in the caller's OWN
    tenant as an `active` person, so neither the `NOT_FOUND` lookup nor the
    already-leaver refusal can produce this answer — deleting the pipeline's
    RBAC stage is the one change that flips these five cases from `FORBIDDEN`
    into completed offboards. Both permitted principals hold `Scope.ANY`, so
    there is no second, argument-dependent check here whose presence would
    blunt the kill.

    `it_admin` is the row that matters most: the matrix gives it the other two
    lifecycle tools and none of this one, so an implementation that reused
    `transfer_user`'s matrix member passes every other case in this file and
    fails this one.

    `tool.denied` rather than `tool.failed`: a permission decision filed as an
    outage is a false record. The status, the live roles and the request are
    all re-read, so a refusal that still offboarded somebody fails here.
    """
    caller = _seeded(CALLER)
    user_id, request_id = _plant_leaver(mcp_env.database_url)
    events_before = _audit_event_types(mcp_env.database_url, caller.tenant_id)

    result = call_tool(bearer(token_for(caller, principals=(principal,))), OFFBOARD, _args(user_id))

    assert error_code(result) == "FORBIDDEN"
    status, end_date = _user_row(mcp_env.database_url, user_id)
    assert str(status) == ACTIVE_STATUS
    assert end_date is None
    assert _live_role_ids(mcp_env.database_url, user_id) == {ROLE_ONE}
    assert _request_status(mcp_env.database_url, request_id) == RequestStatus.PENDING.value
    events_after = _audit_event_types(mcp_env.database_url, caller.tenant_id)
    assert events_after == [*events_before, DENIED_EVENT]


# --- the audit sequence -------------------------


def test_the_audit_sequence_is_revoked_then_rejected_then_offboarded_then_succeeded(
    mcp_env: SeededMcp,
) -> None:
    """The audit order as an exact ordered list, which is the only place the order
    of the three domain events is observable at all.

    Catches the registry declaring `user.offboarded` as this tool's event
    instead of the generic `tool.succeeded`: the pipeline writes its event
    with the TOOL as subject and a fixed payload, so the log would record that
    somebody called a tool and not who left, and the
    leaver's own event would be missing entirely. Catches a summary event
    written before the revocations and the closures, which would have to name
    lists nothing had computed yet, and one that is not last. Catches a
    closure that writes no event at all (the rejected alternative),
    which is invisible in a count of grant events.

    The subject and the five payload keys are asserted here as well as in the
    core test, because only the transport can show that the event the
    PIPELINE adds did not overwrite or duplicate the one the handler wrote.
    """
    caller = _seeded(CALLER)
    user_id, request_id = _plant_leaver(mcp_env.database_url)
    events_before = _audit_event_types(mcp_env.database_url, caller.tenant_id)

    call_tool(bearer(token_for(caller, principals=("hr",))), OFFBOARD, _args(user_id))

    events_after = _audit_event_types(mcp_env.database_url, caller.tenant_id)
    assert events_after == [
        *events_before,
        GRANT_REVOKED_EVENT,
        REQUEST_REJECTED_EVENT,
        USER_OFFBOARDED_EVENT,
        SUCCEEDED_EVENT,
    ]

    record = _offboarded_events(mcp_env.database_url, caller.tenant_id)[-1]
    assert str(record.subject_type) == "user"
    assert str(record.subject_id) == str(user_id)
    payload = dict(record.payload)
    assert set(payload) == {
        "user_id",
        "end_date",
        "revoked_role_ids",
        "revoked_grant_count",
        "closed_request_ids",
    }
    assert payload["user_id"] == str(user_id)
    assert payload["end_date"] == END_DATE
    assert payload["revoked_role_ids"] == [str(ROLE_ONE)]
    assert payload["revoked_grant_count"] == 1
    assert payload["closed_request_ids"] == [str(request_id)]


# --- the two absences ---------------------------------------------


def test_a_missing_and_another_tenants_user_answer_the_same_not_found(
    mcp_env: SeededMcp,
) -> None:
    """Catches a user lookup that escapes the tenant guard, and one that
    distinguishes the two absences.

    If another tenant's user answered anything other than the unknown id's
    answer, `user_id` would be a cross-tenant existence oracle: an HR officer
    could enumerate which people exist in a tenant they cannot see. The other
    tenant's user is re-read afterwards and must still be `active` with their
    grants intact — a cross-tenant offboard that answered `NOT_FOUND` and
    still stripped somebody fails here even though both codes match.
    """
    caller = _seeded(CALLER)
    foreign_before = _user_row(mcp_env.database_url, OTHER_TENANT_USER.id)
    foreign_roles_before = _live_role_ids(mcp_env.database_url, OTHER_TENANT_USER.id)

    missing = call_tool(
        bearer(token_for(caller, principals=("hr",))), OFFBOARD, _args(uuid.uuid4())
    )
    cross_tenant = call_tool(
        bearer(token_for(caller, principals=("hr",))), OFFBOARD, _args(OTHER_TENANT_USER.id)
    )

    assert error_code(missing) == "NOT_FOUND"
    assert error_code(cross_tenant) == "NOT_FOUND"
    foreign_after = _user_row(mcp_env.database_url, OTHER_TENANT_USER.id)
    assert str(foreign_after.status) == ACTIVE_STATUS
    assert foreign_after.end_date == foreign_before.end_date
    assert _live_role_ids(mcp_env.database_url, OTHER_TENANT_USER.id) == foreign_roles_before


# --- the typed refusal through the wire ---------------------------


def test_an_already_leaver_target_is_refused_policy_blocked(
    mcp_env: SeededMcp,
) -> None:
    """At the boundary: catches the "succeed idempotently"
    alternative, whose only trace is in the audit log.

    A second offboard has nothing left to revoke, so the tables end up looking
    the same either way; what differs is a `user.offboarded` recording a
    departure that already happened, with empty lists that read like the
    person had no access. The audit delta is asserted to be exactly
    `tool.failed`, which is what that alternative breaks. The code is
    `POLICY_BLOCKED` and not `VALIDATION`: the arguments are perfectly well
    formed, and calling this a schema problem would send the caller to fix the
    wrong thing.
    """
    caller = _seeded(CALLER)
    user_id = _plant_user(mcp_env.database_url, status=LEAVER_STATUS)
    events_before = _audit_event_types(mcp_env.database_url, caller.tenant_id)

    result = call_tool(bearer(token_for(caller, principals=("hr",))), OFFBOARD, _args(user_id))

    assert error_code(result) == "POLICY_BLOCKED"
    status, end_date = _user_row(mcp_env.database_url, user_id)
    assert str(status) == LEAVER_STATUS
    assert end_date is None, "a refused offboard writes no end date"
    assert _audit_event_types(mcp_env.database_url, caller.tenant_id) == [
        *events_before,
        FAILED_EVENT,
    ]


# --- the input surface -------------------------------------------


@pytest.mark.parametrize(
    ("user_id", "end_date", "accepted", "expected_code"),
    [
        pytest.param(None, END_DATE, True, "", id="baseline-ok"),
        pytest.param("not-a-uuid", END_DATE, False, "VALIDATION", id="user-not-uuid"),
        pytest.param("", END_DATE, False, "VALIDATION", id="user-empty"),
        pytest.param(None, "2026-13-01", False, "VALIDATION", id="date-month-13"),
        pytest.param(None, "2026-09-31", False, "VALIDATION", id="date-day-31-sept"),
        pytest.param(None, "not-a-date", False, "VALIDATION", id="date-text"),
        pytest.param(None, "", False, "VALIDATION", id="date-empty"),
    ],
)
def test_the_input_types_are_refused_before_any_effect(
    mcp_env: SeededMcp,
    user_id: str | None,
    end_date: str,
    accepted: bool,
    expected_code: str,
) -> None:
    """Both directions: catches `user_id` typed as `str` and
    `end_date` typed as `str`.

    A string `end_date` is written straight into `users.end_date` and into the
    `user.offboarded` payload, so `"2026-13-01"` and `"2026-09-31"` — a
    real-looking date with a month and a day that do not exist — either kill
    the insert as an `INTERNAL` or become a permanent audit record nobody can
    parse. Every other case in this file sends a valid one, so this table is
    the only place that shows. The malformed uuid catches the same mistake on
    the id, where a `str` compared against a real uuid answers `NOT_FOUND`
    instead of `VALIDATION`, telling the caller the person is missing when
    their argument is malformed.

    The baseline row is what stops this table passing vacuously: it proves the
    same arguments minus the one defect go through. Every refused row re-reads
    the status, the live roles and the request, so the check has to run BEFORE
    any effect.
    """
    caller = _seeded(CALLER)
    planted_user, request_id = _plant_leaver(mcp_env.database_url)

    result = call_tool(
        bearer(token_for(caller, principals=("hr",))),
        OFFBOARD,
        _args(planted_user if user_id is None else user_id, end_date=end_date),
    )

    status, _ = _user_row(mcp_env.database_url, planted_user)
    if accepted:
        assert result.is_error is False, result.content
        assert str(status) == LEAVER_STATUS
    else:
        assert error_code(result) == expected_code
        assert str(status) == ACTIVE_STATUS
        assert _live_role_ids(mcp_env.database_url, planted_user) == {ROLE_ONE}
        assert _request_status(mcp_env.database_url, request_id) == RequestStatus.PENDING.value


def test_an_unknown_field_is_refused_with_no_state_change(
    mcp_env: SeededMcp,
) -> None:
    """`MutatingToolInput` forbids extras, and this is the argument that proves
    why: a `tenant_id` in the arguments is a caller who believes they are
    offboarding somebody inside another tenant.

    Dropping it silently would strip the person in the TOKEN's tenant while
    the caller reads success as having acted elsewhere — the one mistake the identity rule
    exists to make impossible to make quietly. Catches an input model
    that copied `tools/read.py`'s `extra="ignore"` contract: nothing else in
    this call can refuse it, since the caller is permitted and every other
    argument is valid. The state is re-read in BOTH tenants, so a call that
    answered `VALIDATION` and still revoked something fails here.
    """
    caller = _seeded(CALLER)
    user_id, request_id = _plant_leaver(mcp_env.database_url)
    foreign_roles_before = _live_role_ids(mcp_env.database_url, OTHER_TENANT_USER.id)

    result = call_tool(
        bearer(token_for(caller, principals=("hr",))),
        OFFBOARD,
        _args(user_id, tenant_id=str(OTHER_TENANT_ROWS.tenant_id)),
    )

    assert error_code(result) == "VALIDATION"
    status, end_date = _user_row(mcp_env.database_url, user_id)
    assert str(status) == ACTIVE_STATUS
    assert end_date is None
    assert _live_role_ids(mcp_env.database_url, user_id) == {ROLE_ONE}
    assert _request_status(mcp_env.database_url, request_id) == RequestStatus.PENDING.value
    assert _live_role_ids(mcp_env.database_url, OTHER_TENANT_USER.id) == foreign_roles_before


# --- replay ------------------------------------------------------


def test_a_replay_returns_the_stored_response_and_revokes_nothing_twice(
    mcp_env: SeededMcp,
) -> None:
    """Catches a retry that re-runs the body.

    The second call under the same key must not reach the already-leaver
    check: by then the person IS a leaver, so a re-run answers
    `POLICY_BLOCKED` for a call the caller already saw succeed. Byte identity
    is asserted, not just equality of meaning, so a response rebuilt rather
    than replayed fails; the audit delta is exactly `tool.replayed`, so a
    second `user.offboarded` — a false record of a departure that never
    happened twice — and a second `grant.revoked` both fail. The grant row is
    counted and its `revoked_at` re-read, so a second pass that revoked an
    already-revoked row cannot hide behind a matching response.
    """
    caller = _seeded(CALLER)
    user_id, _ = _plant_leaver(mcp_env.database_url)
    arguments = _args(user_id, idempotency_key=_key())

    first = call_tool(bearer(token_for(caller, principals=("hr",))), OFFBOARD, arguments)
    events_between = _audit_event_types(mcp_env.database_url, caller.tenant_id)
    revoked_between = _grant_rows(mcp_env.database_url, user_id)
    second = call_tool(bearer(token_for(caller, principals=("hr",))), OFFBOARD, arguments)

    assert _structured(first)["revoked_grant_count"] == 1
    assert _response_text(second) == _response_text(first)
    assert second.structured_content == first.structured_content
    rows = _grant_rows(mcp_env.database_url, user_id)
    assert len(rows) == 1
    assert rows[0].revoked_at == revoked_between[0].revoked_at, "never revoked a second time"
    assert _audit_event_types(mcp_env.database_url, caller.tenant_id) == [
        *events_between,
        REPLAYED_EVENT,
    ]


# --- the lock ------------------------------------------------------


def test_two_callers_racing_the_same_offboard_leave_one_departure(
    mcp_env: SeededMcp,
) -> None:
    """The `lock_user` row lock. Two different callers send two different
    idempotency keys, so the guard cannot serialise them at all; only
    `SELECT … FOR UPDATE` on the `users` row can.

    Without the lock both calls read the person as `active`, both read the
    same live grant, and both run the revocation loop — the log carries TWO
    `user.offboarded` events and two `grant.revoked` for one row, which is an
    audit trail saying the same access was taken away twice from a person who
    departed twice. That is the state this asserts against: one success, one
    `POLICY_BLOCKED` from the re-check (the loser now reads the winner's
    `leaver`), one departure event, one revocation event.

    Both callers are real seeded users carrying the `hr` principal, so both
    are genuinely entitled and the race is real; the refusal is never
    `FORBIDDEN`.
    """
    first_caller = _seeded(HR_ONE)
    second_caller = _seeded(HR_TWO)
    assert first_caller.user_id != second_caller.user_id
    user_id, _ = _plant_leaver(mcp_env.database_url)
    events_before = _audit_event_types(mcp_env.database_url, first_caller.tenant_id)

    def offboard(caller: SeededUser) -> Any:
        return call_tool(bearer(token_for(caller, principals=("hr",))), OFFBOARD, _args(user_id))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(offboard, first_caller), pool.submit(offboard, second_caller)]
        results = [future.result() for future in futures]

    successes = [result for result in results if result.is_error is False]
    refusals = [result for result in results if result.is_error is True]
    assert len(successes) == 1, [result.content for result in results]
    assert len(refusals) == 1
    assert error_code(refusals[0]) == "POLICY_BLOCKED"
    status, _ = _user_row(mcp_env.database_url, user_id)
    assert str(status) == LEAVER_STATUS
    written = _audit_event_types(mcp_env.database_url, first_caller.tenant_id)[len(events_before) :]
    assert written.count(USER_OFFBOARDED_EVENT) == 1, "one departure, never one per caller"
    assert written.count(GRANT_REVOKED_EVENT) == 1, "the revocations happened once"
