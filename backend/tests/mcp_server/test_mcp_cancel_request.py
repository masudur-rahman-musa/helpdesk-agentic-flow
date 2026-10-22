"""`cancel_request`, through the transport.

Surface under test (written before it exists):

``mcp_server.tools.approvals``
    ``cancel_request(request_id, reason?, idempotency_key)`` — a MUTATING tool
    for every principal (scope self only). The caller must be the requester or
    the target of the request, else ``FORBIDDEN`` (checked BEFORE the status).
    Another company's or a missing request answers ``NOT_FOUND``. The row is
    locked with the same ``lock_request`` approve and reject use; a row that is
    not ``pending`` answers the new ``REQUEST_NOT_PENDING``. On success the
    status becomes ``cancelled``, one ``request.cancelled`` event is appended
    (``REQUEST_CANCELLED_EVENT_TYPE``), seats are left as they were, nothing is
    granted, and every approver on a still-open seat gets a
    ``request_cancelled`` notice. ``reason`` is optional, 1 to 2000 characters.
    ``CancelRequestOutput`` carries ``request_id``, ``request_number``,
    ``status`` and ``role_name``.
``list_my_requests`` also returns requests filed FOR the caller.

Fixture cost: everything rides the session-scoped ``mcp_env`` seam. Every case
plants its OWN requester and target (fresh uuid4 users) and its own request
row, so no case sees another's rows. Engines come from ``tests.support.mcp``;
this file builds none. No pytest-asyncio: each case is a sync function (the
race drives two threads).
"""

import asyncio
import uuid
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from typing import Any

import pytest

from helpdesk.mcp_server.tools.approvals import REQUEST_CANCELLED_EVENT_TYPE
from helpdesk.shared.core.db import tenant_session
from helpdesk.shared.crud import Repository
from helpdesk.shared.models import AccessRequest, RequestStatus
from helpdesk.shared.models.numbers import NumberKind, format_number
from helpdesk.shared.policy.types import ApproverSlot
from scripts.seed import CUAN, NORDLICHT, generate_tenant
from tests.support.mcp import (
    SeededMcp,
    SeededUser,
    bearer,
    call_tool,
    error_code,
    probe_session_factory,
    sql_execute,
    sql_rows,
    token_for,
)

pytestmark = pytest.mark.db

CANCEL = "cancel_request"
APPROVE = "approve_request"
LIST_MY_REQUESTS = "list_my_requests"

REQUEST_APPROVED_EVENT = "request.approved"
REQUEST_REJECTED_EVENT = "request.rejected"
GRANT_EXECUTED_EVENT = "grant.executed"
SUCCEEDED_EVENT = "tool.succeeded"
REPLAYED_EVENT = "tool.replayed"

REASON_MAX_LENGTH = 2000

ROWS = generate_tenant(NORDLICHT)
OTHER_TENANT_ROWS = generate_tenant(CUAN)

PRODUCTION_DEPARTMENT = ROWS.department_ids["Production"]
PRODUCTION_MANAGER = ROWS.users_by_department["Production"][0]
PROD_K8S_OWNER = ROWS.system_owners["prod-k8s"]
IT_ADMIN_ONE = ROWS.users_by_department["IT"][16]
GRANTABLE_ROLE = ROWS.role_ids[("grafana", "viewer")]

OTHER_TENANT_REQUESTER = OTHER_TENANT_ROWS.users_by_department["Operations"][2]
OTHER_TENANT_TARGET = OTHER_TENANT_ROWS.users_by_department["Operations"][3]
OTHER_TENANT_ROLE = next(iter(OTHER_TENANT_ROWS.role_ids.values()))

PLANTED_START = date(2019, 3, 7)


def _key() -> str:
    return f"fake-key-{uuid.uuid4().hex}"


def _structured(result: Any) -> dict[str, Any]:
    assert result.is_error is False, result.content
    assert result.structured_content is not None
    content: dict[str, Any] = result.structured_content
    return content


def _response_text(result: Any) -> str:
    assert result.is_error is False, result.content
    assert len(result.content) == 1, result.content
    return str(result.content[0].text)


def _seeded(user: Any) -> SeededUser:
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


def _plant_user(env: SeededMcp) -> SeededUser:
    """One fresh, token-ready, obviously fake employee."""
    user_id = uuid.uuid4()
    sql_execute(
        env.database_url,
        "INSERT INTO users (id, tenant_id, department_id, email, display_name, password_hash,"
        " principals, status, start_date, end_date, created_at, updated_at)"
        " VALUES (:id, :tenant, :department, :email, :display_name, :password_hash,"
        " :principals, 'active', :start_date, NULL, now(), now())",
        {
            "id": user_id,
            "tenant": ROWS.tenant_id,
            "department": PRODUCTION_DEPARTMENT,
            "email": f"fake.cancel.{user_id}@fake.invalid",
            "display_name": "Fake Cancel",
            "password_hash": "not-a-real-hash-this-user-never-logs-in",
            "principals": ["employee"],
            "start_date": PLANTED_START,
        },
    )
    return SeededUser(
        tenant_id=ROWS.tenant_id,
        tenant_slug=NORDLICHT.slug,
        user_id=user_id,
        email=f"fake.cancel.{user_id}@fake.invalid",
        display_name="Fake Cancel",
        department_id=PRODUCTION_DEPARTMENT,
        principals=("employee",),
        status="active",
    )


def _open_seat(slot: ApproverSlot, user_id: uuid.UUID | None = None) -> dict[str, Any]:
    return {"slot": slot.value, "user_id": None if user_id is None else str(user_id)}


def _approved_seat(slot: ApproverSlot, user_id: uuid.UUID) -> dict[str, Any]:
    return {
        "slot": slot.value,
        "user_id": str(user_id),
        "decision": "approve",
        "decided_by_user_id": str(user_id),
        "decided_at": datetime(2026, 1, 2, 9, 0, tzinfo=UTC).isoformat(),
        "comment": None,
    }


def _plant_request(
    env: SeededMcp,
    *,
    requester_user_id: uuid.UUID,
    target_user_id: uuid.UUID,
    approvals: Sequence[dict[str, Any]],
    status: RequestStatus = RequestStatus.PENDING,
    tenant_id: uuid.UUID | None = None,
    role_id: uuid.UUID = GRANTABLE_ROLE,
) -> uuid.UUID:
    async def scenario() -> uuid.UUID:
        factory = probe_session_factory(env.database_url)
        async with tenant_session(factory, tenant_id or ROWS.tenant_id) as session:
            row = await Repository(session, AccessRequest).create(
                requester_user_id=requester_user_id,
                target_user_id=target_user_id,
                role_id=role_id,
                justification=f"fake pending request {uuid.uuid4().hex}",
                requested_expiry_days=365,
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

    return asyncio.run(scenario())


def _plant_pending(
    env: SeededMcp,
    requester: SeededUser,
    target: SeededUser | None = None,
    *,
    approvals: Sequence[dict[str, Any]] | None = None,
    status: RequestStatus = RequestStatus.PENDING,
) -> uuid.UUID:
    return _plant_request(
        env,
        requester_user_id=requester.user_id,
        target_user_id=(target or requester).user_id,
        approvals=approvals
        if approvals is not None
        else [_open_seat(ApproverSlot.DEPARTMENT_MANAGER, PRODUCTION_MANAGER.id)],
        status=status,
    )


def _request_row(env: SeededMcp, request_id: uuid.UUID) -> Any:
    rows = sql_rows(
        env.database_url,
        "SELECT id, number, status, approvals FROM access_requests WHERE id = :id",
        {"id": request_id},
    )
    assert len(rows) == 1
    return rows[0]


def _audit_types(env: SeededMcp) -> list[str]:
    rows = sql_rows(
        env.database_url,
        "SELECT event_type FROM audit_events WHERE tenant_id = :tenant ORDER BY seq",
        {"tenant": ROWS.tenant_id},
    )
    return [str(row[0]) for row in rows]


def _grant_count(env: SeededMcp) -> int:
    rows = sql_rows(
        env.database_url,
        "SELECT count(*) FROM grants WHERE tenant_id = :tenant",
        {"tenant": ROWS.tenant_id},
    )
    return int(rows[0][0])


def _grants_for(env: SeededMcp, request_id: uuid.UUID) -> int:
    rows = sql_rows(
        env.database_url,
        "SELECT count(*) FROM grants WHERE request_id = :request",
        {"request": request_id},
    )
    return int(rows[0][0])


def _cancel(caller: SeededUser, request_id: uuid.UUID | str, **extra: Any) -> Any:
    return call_tool(
        bearer(token_for(caller)),
        CANCEL,
        {"request_id": str(request_id), "idempotency_key": _key(), **extra},
    )


def _notes_about(env: SeededMcp, request_id: uuid.UUID) -> list[Any]:
    return sql_rows(
        env.database_url,
        "SELECT user_id, kind, title, body FROM notifications"
        " WHERE subject_type = 'access_request' AND subject_id = :subject",
        {"subject": str(request_id)},
    )


def test_the_requester_cancels_a_pending_request(mcp_env: SeededMcp) -> None:
    """Catches a tool that does not move the row to `cancelled` (or files it as
    `rejected`), one that touches the seats, one that grants anyway, and one
    that appends no `request.cancelled` event (or a second one).
    """
    requester = _plant_user(mcp_env)
    request_id = _plant_pending(mcp_env, requester)
    seats_before = _request_row(mcp_env, request_id).approvals
    events_before = _audit_types(mcp_env)
    grants_before = _grant_count(mcp_env)

    payload = _structured(_cancel(requester, request_id))

    assert payload["request_id"] == str(request_id)
    assert payload["status"] == "cancelled"
    assert payload["request_number"] == format_number(
        NumberKind.ACCESS_REQUEST, int(_request_row(mcp_env, request_id).number)
    )
    assert payload["role_name"]
    row = _request_row(mcp_env, request_id)
    assert row.status == RequestStatus.CANCELLED.value
    assert row.approvals == seats_before
    assert _grant_count(mcp_env) == grants_before
    new_events = _audit_types(mcp_env)[len(events_before) :]
    assert new_events.count(REQUEST_CANCELLED_EVENT_TYPE) == 1
    assert REQUEST_CANCELLED_EVENT_TYPE == "request.cancelled"
    assert GRANT_EXECUTED_EVENT not in new_events
    assert REQUEST_REJECTED_EVENT not in new_events


def test_the_target_cancels_a_request_a_manager_filed_for_them(mcp_env: SeededMcp) -> None:
    """Catches a tool that lets only the requester cancel: a request filed for
    someone else by a manager would then be stuck for the person it is for.
    The caller is the target and NOT the requester, so only the target rule can
    admit them."""
    manager_filing = _plant_user(mcp_env)
    target = _plant_user(mcp_env)
    request_id = _plant_pending(mcp_env, manager_filing, target)

    payload = _structured(_cancel(target, request_id))

    assert payload["status"] == "cancelled"
    assert _request_row(mcp_env, request_id).status == "cancelled"


def test_a_partly_approved_request_can_still_be_cancelled(mcp_env: SeededMcp) -> None:
    """Catches a tool that treats "some approvals in" as decided: it must end
    `cancelled`, keep the approved seat exactly as it was, and not grant."""
    requester = _plant_user(mcp_env)
    seats = [
        _approved_seat(ApproverSlot.DEPARTMENT_MANAGER, PRODUCTION_MANAGER.id),
        _open_seat(ApproverSlot.IT_ADMIN),
    ]
    request_id = _plant_pending(mcp_env, requester, approvals=seats)
    seats_before = _request_row(mcp_env, request_id).approvals
    grants_before = _grant_count(mcp_env)

    _structured(_cancel(requester, request_id))

    row = _request_row(mcp_env, request_id)
    assert row.status == "cancelled"
    assert row.approvals == seats_before
    assert row.approvals[0]["decision"] == "approve"
    assert _grant_count(mcp_env) == grants_before


@pytest.mark.parametrize(
    "status",
    [
        RequestStatus.APPROVED,
        RequestStatus.REJECTED,
        RequestStatus.EXECUTED,
        RequestStatus.FAILED,
        RequestStatus.ROLLED_BACK,
        RequestStatus.EXPIRED,
        RequestStatus.CANCELLED,
    ],
)
def test_a_decided_request_answers_request_not_pending(
    mcp_env: SeededMcp, status: RequestStatus
) -> None:
    """Catches a tool that cancels any row it can see (a granted request turned
    into `cancelled` while the access stays on), and one that answers
    `POLICY_BLOCKED` like approve and reject instead of the new code. The caller
    is the requester, so only the status check can refuse."""
    requester = _plant_user(mcp_env)
    request_id = _plant_pending(mcp_env, requester, status=status)
    events_before = _audit_types(mcp_env)

    result = _cancel(requester, request_id)

    assert error_code(result) == "REQUEST_NOT_PENDING"
    assert _request_row(mcp_env, request_id).status == status.value
    assert REQUEST_CANCELLED_EVENT_TYPE not in _audit_types(mcp_env)[len(events_before) :]


def test_someone_elses_request_answers_forbidden_even_when_decided(mcp_env: SeededMcp) -> None:
    """Catches a tool that checks the status before the caller: a stranger
    would learn that a colleague's request is already approved. The row is
    `approved`, so a status-first tool answers `REQUEST_NOT_PENDING` here."""
    owner = _plant_user(mcp_env)
    stranger = _plant_user(mcp_env)
    request_id = _plant_pending(mcp_env, owner, status=RequestStatus.APPROVED)

    result = _cancel(stranger, request_id)

    assert error_code(result) == "FORBIDDEN"
    assert _request_row(mcp_env, request_id).status == "approved"


def test_a_stranger_cannot_cancel_a_pending_request(mcp_env: SeededMcp) -> None:
    """Catches a tool that lets any colleague cancel: the row stays `pending`
    and nothing is written."""
    owner = _plant_user(mcp_env)
    stranger = _plant_user(mcp_env)
    request_id = _plant_pending(mcp_env, owner)
    events_before = _audit_types(mcp_env)

    result = _cancel(stranger, request_id)

    assert error_code(result) == "FORBIDDEN"
    assert _request_row(mcp_env, request_id).status == "pending"
    assert REQUEST_CANCELLED_EVENT_TYPE not in _audit_types(mcp_env)[len(events_before) :]


def test_another_companys_request_and_a_missing_one_answer_not_found(mcp_env: SeededMcp) -> None:
    """Catches a lookup that escapes the tenant guard, and one that tells the
    two absences apart (an id oracle across companies). The other company's
    request is pending, so only the tenant guard can refuse it."""
    caller = _plant_user(mcp_env)
    theirs = _plant_request(
        mcp_env,
        requester_user_id=OTHER_TENANT_REQUESTER.id,
        target_user_id=OTHER_TENANT_TARGET.id,
        approvals=[_open_seat(ApproverSlot.IT_ADMIN)],
        tenant_id=OTHER_TENANT_ROWS.tenant_id,
        role_id=OTHER_TENANT_ROLE,
    )

    cross_tenant = _cancel(caller, theirs)
    missing = _cancel(caller, uuid.uuid4())

    assert error_code(cross_tenant) == "NOT_FOUND"
    assert error_code(missing) == "NOT_FOUND"
    rows = sql_rows(
        mcp_env.database_url,
        "SELECT status FROM access_requests WHERE id = :id",
        {"id": theirs},
    )
    assert rows[0][0] == "pending"


def test_approve_and_cancel_racing_end_with_exactly_one_winner(mcp_env: SeededMcp) -> None:
    """Catches a cancel that reads the status without the row lock approve
    and reject take: both would win, leaving a granted access on a `cancelled`
    request. Different callers and different keys, so only the lock can
    serialise them. Run a few rounds so a missing lock shows up.

    Either approve wins (one grant, cancel answers `REQUEST_NOT_PENDING`) or
    cancel wins (no grant, approve answers `POLICY_BLOCKED`); never both.
    """
    admin = _seeded(IT_ADMIN_ONE)
    assert "it_admin" in admin.principals
    for _ in range(3):
        requester = _plant_user(mcp_env)
        request_id = _plant_pending(
            mcp_env, requester, approvals=[_open_seat(ApproverSlot.IT_ADMIN)]
        )

        def approve(rid: uuid.UUID = request_id) -> Any:
            return call_tool(
                bearer(token_for(admin)),
                APPROVE,
                {"request_id": str(rid), "idempotency_key": _key()},
            )

        def cancel(rid: uuid.UUID = request_id, who: SeededUser = requester) -> Any:
            return _cancel(who, rid)

        with ThreadPoolExecutor(max_workers=2) as pool:
            approve_future = pool.submit(approve)
            cancel_future = pool.submit(cancel)
            approved = approve_future.result()
            cancelled = cancel_future.result()

        status = _request_row(mcp_env, request_id).status
        if approved.is_error is False:
            assert cancelled.is_error is True
            assert error_code(cancelled) == "REQUEST_NOT_PENDING"
            assert status != "cancelled"
            assert _grants_for(mcp_env, request_id) == 1
        else:
            assert cancelled.is_error is False, cancelled.content
            assert error_code(approved) == "POLICY_BLOCKED"
            assert status == "cancelled"
            assert _grants_for(mcp_env, request_id) == 0


def test_only_undecided_approvers_get_a_cancelled_notice(mcp_env: SeededMcp) -> None:
    """Catches a cancel that tells nobody, one that tells the approver who
    already decided, and one that tells the requester, the target or the person
    cancelling. The open seats are a named manager and the IT admin group; the
    decided seat names the system owner, who must stay out."""
    requester = _plant_user(mcp_env)
    target = _plant_user(mcp_env)
    seats = [
        _open_seat(ApproverSlot.DEPARTMENT_MANAGER, PRODUCTION_MANAGER.id),
        _approved_seat(ApproverSlot.SYSTEM_OWNER, PROD_K8S_OWNER.id),
        _open_seat(ApproverSlot.IT_ADMIN),
    ]
    request_id = _plant_pending(mcp_env, requester, target, approvals=seats)
    admins = {
        user.id
        for users in ROWS.users_by_department.values()
        for user in users
        if "it_admin" in user.principals
    }
    assert PROD_K8S_OWNER.id not in admins, "premise: the decided person is not in the group"
    assert PRODUCTION_MANAGER.id not in admins

    _structured(_cancel(requester, request_id))

    notes = _notes_about(mcp_env, request_id)
    assert sorted(note.user_id for note in notes) == sorted({PRODUCTION_MANAGER.id} | admins)
    assert {note.kind for note in notes} == {"request_cancelled"}
    assert all("cancelled" in note.body for note in notes)


def test_the_person_cancelling_and_the_other_party_get_no_notice(
    mcp_env: SeededMcp,
) -> None:
    """Catches a cancel that tells the person who cancelled, or the other
    party to the request: the only open seat is a named manager, so exactly
    one note exists and it is neither the requester's nor the target's."""
    requester = _plant_user(mcp_env)
    target = _plant_user(mcp_env)
    request_id = _plant_pending(mcp_env, requester, target)

    _structured(_cancel(target, request_id))

    notes = _notes_about(mcp_env, request_id)
    assert [note.user_id for note in notes] == [PRODUCTION_MANAGER.id]


@pytest.mark.parametrize(
    ("length", "accepted"),
    [(1, True), (REASON_MAX_LENGTH, True), (0, False), (REASON_MAX_LENGTH + 1, False)],
)
def test_the_reason_boundary_is_inclusive_at_1_and_2000(
    mcp_env: SeededMcp, length: int, accepted: bool
) -> None:
    """Catches a bound that is exclusive at either end (1 or 2000 refused) or
    missing (an empty or 2001-character reason accepted into the audit store).
    A refused call must leave the row `pending`."""
    requester = _plant_user(mcp_env)
    request_id = _plant_pending(mcp_env, requester)

    result = _cancel(requester, request_id, reason="r" * length)

    if accepted:
        assert result.is_error is False, result.content
        assert _request_row(mcp_env, request_id).status == "cancelled"
    else:
        assert error_code(result) == "VALIDATION"
        assert _request_row(mcp_env, request_id).status == "pending"


def test_no_reason_is_allowed(mcp_env: SeededMcp) -> None:
    """Catches a reason that is required: the person may just say "cancel it"."""
    requester = _plant_user(mcp_env)
    request_id = _plant_pending(mcp_env, requester)

    result = _cancel(requester, request_id)

    assert result.is_error is False, result.content
    assert _request_row(mcp_env, request_id).status == "cancelled"


def test_a_replay_with_the_same_key_returns_the_stored_response(mcp_env: SeededMcp) -> None:
    """Catches a replay that re-runs the body (the second call would answer
    `REQUEST_NOT_PENDING` for a cancel that worked), one that rebuilds the
    response with different bytes, and one that writes again."""
    requester = _plant_user(mcp_env)
    request_id = _plant_pending(mcp_env, requester)
    arguments = {"request_id": str(request_id), "idempotency_key": _key()}
    notes_before = len(_notes_about(mcp_env, request_id))

    first = call_tool(bearer(token_for(requester)), CANCEL, arguments)
    events_between = _audit_types(mcp_env)
    notes_between = len(_notes_about(mcp_env, request_id))
    second = call_tool(bearer(token_for(requester)), CANCEL, arguments)

    assert _response_text(second) == _response_text(first)
    assert second.structured_content == first.structured_content
    assert _audit_types(mcp_env) == [*events_between, REPLAYED_EVENT]
    assert len(_notes_about(mcp_env, request_id)) == notes_between
    assert notes_between > notes_before, "premise: the first call told an approver"


def test_list_my_requests_also_returns_requests_filed_for_the_caller(mcp_env: SeededMcp) -> None:
    """Catches a read that still lists only requests the caller filed: a
    person could not find a request a manager filed for them, and so could not
    cancel it. A colleague's request, filed by and for others, must stay out."""
    filer = _plant_user(mcp_env)
    target = _plant_user(mcp_env)
    other_a = _plant_user(mcp_env)
    other_b = _plant_user(mcp_env)
    for_me = _plant_pending(mcp_env, filer, target)
    by_me = _plant_pending(mcp_env, target)
    colleagues = _plant_pending(mcp_env, other_a, other_b)

    payload = _structured(call_tool(bearer(token_for(target)), LIST_MY_REQUESTS, {}))

    ids = {item["request_id"] for item in payload["requests"]}
    assert str(for_me) in ids
    assert str(by_me) in ids
    assert str(colleagues) not in ids
