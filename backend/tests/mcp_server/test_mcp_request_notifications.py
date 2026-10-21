"""The request tools write the notifications of an earlier change, through the transport.

Surface under test (written before it exists):

``create_access_request``, ``approve_request``, ``reject_request``,
``revoke_access``, ``decide_review_item`` and ``offboard_user`` each write
their notification rows in the same transaction as the change they report,
through ``helpdesk.shared.notifications.request_events``. This file asserts
the rows. The wording and recipient rules are table-driven in
``tests/shared/notifications/test_request_events.py``; here only Postgres can
show that the row exists, belongs to the right person, and is undone with a
refused change.

Fixture cost: everything rides the session-scoped ``mcp_env`` seam. Every case
plants its OWN requester, target and grants (fresh uuid4 users in the seeded
Production department) and reads notifications by the subject it created, so
no case sees another's rows. Engines come from ``tests.support.mcp``; this file
builds none.

No pytest-asyncio: each case is a sync function driving ``call_tool``.
"""

import asyncio
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from typing import Any

import pytest

from helpdesk.shared.core.db import tenant_session
from helpdesk.shared.crud import Repository
from helpdesk.shared.models import AccessRequest, RequestStatus
from helpdesk.shared.models.numbers import NumberKind, format_number
from helpdesk.shared.policy.types import ApproverSlot
from scripts.seed import NORDLICHT, UserRow, generate_tenant
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

CREATE = "create_access_request"
APPROVE = "approve_request"
REJECT = "reject_request"
REVOKE = "revoke_access"
DECIDE = "decide_review_item"
OFFBOARD = "offboard_user"

ROWS = generate_tenant(NORDLICHT)

# The seeded manager of every department is index 0; `prod-k8s` is owned by an
# IT user. A critical role on `prod-k8s` queues manager + owner + it_admin.
PRODUCTION_MANAGER = ROWS.users_by_department["Production"][0]
PROD_K8S_OWNER = ROWS.system_owners["prod-k8s"]
PLATFORM_ADMIN = ROWS.users_by_department["IT"][18]
HR_CALLER = ROWS.users_by_department["HR"][10]
PRODUCTION_DEPARTMENT = ROWS.department_ids["Production"]

APPROVAL_ROLE = ROWS.role_ids[("prod-k8s", "deployer")]
LOW_ROLE = ROWS.role_ids[("wiki", "viewer")]
GRANTABLE_ROLE = ROWS.role_ids[("grafana", "viewer")]
SOD_REQUEST_ROLE = ROWS.role_ids[("payments", "approver")]
SOD_CONFLICTING_ROLE = ROWS.role_ids[("payments", "initiator")]
GRANTED_ROLE = GRANTABLE_ROLE
OFFBOARD_ROLE = ROWS.role_ids[("mes", "viewer")]

GRANTED_AT = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
FAR_EXPIRY = datetime(2099, 6, 1, 12, 0, tzinfo=UTC)
PLANTED_START = date(2019, 3, 7)

REQUEST_SUBJECT = "access_request"
GRANT_SUBJECT = "grant"


def _seeded(user: UserRow) -> SeededUser:
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
    return f"fake-key-{uuid.uuid4().hex}"


def _structured(result: Any) -> dict[str, Any]:
    assert result.is_error is False, result.content
    assert result.structured_content is not None
    content: dict[str, Any] = result.structured_content
    return content


def _plant_user(
    env: SeededMcp, *, department_id: uuid.UUID | None = PRODUCTION_DEPARTMENT
) -> SeededUser:
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
            "department": department_id,
            "email": f"fake.notify.{uuid.uuid4().hex}@fake.invalid",
            "display_name": "Fake Notify",
            "password_hash": "not-a-real-hash-this-user-never-logs-in",
            "principals": ["employee"],
            "start_date": PLANTED_START,
        },
    )
    return SeededUser(
        tenant_id=ROWS.tenant_id,
        tenant_slug=NORDLICHT.slug,
        user_id=user_id,
        email=f"fake.notify.{user_id}@fake.invalid",
        display_name="Fake Notify",
        department_id=department_id or PRODUCTION_DEPARTMENT,
        principals=("employee",),
        status="active",
    )


def _plant_request(
    env: SeededMcp,
    *,
    requester: SeededUser,
    target: SeededUser,
    role_id: uuid.UUID,
    slots: Sequence[ApproverSlot],
) -> uuid.UUID:
    """One pending request. The department manager seat names the seeded manager."""
    approvals = [
        {
            "slot": slot.value,
            "user_id": str(PRODUCTION_MANAGER.id)
            if slot is ApproverSlot.DEPARTMENT_MANAGER
            else None,
        }
        for slot in slots
    ]

    async def scenario() -> uuid.UUID:
        factory = probe_session_factory(env.database_url)
        async with tenant_session(factory, ROWS.tenant_id) as session:
            row = await Repository(session, AccessRequest).create(
                requester_user_id=requester.user_id,
                target_user_id=target.user_id,
                role_id=role_id,
                justification=f"fake pending request {uuid.uuid4().hex}",
                requested_expiry_days=365,
                risk_class="critical",
                decision="approval",
                decision_reasons=[],
                citations=[],
                status=RequestStatus.PENDING.value,
                approver_user_ids=[],
                approvals=approvals,
                session_id=None,
                trace_id=None,
                idempotency_key=None,
            )
            return row.id

    return asyncio.run(scenario())


def _plant_grant(env: SeededMcp, *, user_id: uuid.UUID, role_id: uuid.UUID) -> uuid.UUID:
    grant_id = uuid.uuid4()
    sql_execute(
        env.database_url,
        "INSERT INTO grants (id, tenant_id, user_id, role_id, request_id, granted_by_user_id,"
        " granted_at, expires_at, revoked_at, revoke_reason, status, created_at, updated_at)"
        " VALUES (:id, :tenant, :user, :role, NULL, NULL, :granted_at, :expires_at, NULL, NULL,"
        " 'active', now(), now())",
        {
            "id": grant_id,
            "tenant": ROWS.tenant_id,
            "user": user_id,
            "role": role_id,
            "granted_at": GRANTED_AT,
            "expires_at": FAR_EXPIRY,
        },
    )
    return grant_id


def _revoke_reason_of(env: SeededMcp, grant_id: uuid.UUID) -> str:
    rows = sql_rows(
        env.database_url, "SELECT revoke_reason FROM grants WHERE id = :id", {"id": grant_id}
    )
    assert len(rows) == 1 and rows[0][0], "premise: the revoke stored a reason"
    return str(rows[0][0])


def _request_number(env: SeededMcp, request_id: uuid.UUID) -> str:
    rows = sql_rows(
        env.database_url, "SELECT number FROM access_requests WHERE id = :id", {"id": request_id}
    )
    return format_number(NumberKind.ACCESS_REQUEST, int(rows[0][0]))


class Note:
    """One stored notification, read around the tools."""

    def __init__(self, row: Any) -> None:
        self.user_id = uuid.UUID(str(row.user_id))
        self.kind = str(row.kind)
        self.title = str(row.title)
        self.body = str(row.body)
        self.subject_type = row.subject_type
        self.subject_id = row.subject_id
        self.read_at = row.read_at


def _notes_about(env: SeededMcp, subject_type: str, subject_id: uuid.UUID) -> list[Note]:
    rows = sql_rows(
        env.database_url,
        "SELECT user_id, kind, title, body, subject_type, subject_id, read_at FROM notifications"
        " WHERE subject_type = :type AND subject_id = :subject ORDER BY created_at, id",
        {"type": subject_type, "subject": str(subject_id)},
    )
    return [Note(row) for row in rows]


def _notes_for_user(env: SeededMcp, user_id: uuid.UUID) -> list[Note]:
    rows = sql_rows(
        env.database_url,
        "SELECT user_id, kind, title, body, subject_type, subject_id, read_at FROM notifications"
        " WHERE user_id = :user ORDER BY created_at, id",
        {"user": user_id},
    )
    return [Note(row) for row in rows]


def _pairs(notes: Sequence[Note]) -> list[tuple[uuid.UUID, str]]:
    return sorted((note.user_id, note.kind) for note in notes)


def _it_admin_ids() -> set[uuid.UUID]:
    return {
        user.id
        for users in ROWS.users_by_department.values()
        for user in users
        if "it_admin" in user.principals
    }


# --- filing ---------------------------------------------------------------------


def test_filing_for_approval_notifies_each_approver(mcp_env: SeededMcp) -> None:
    """Catches a filing that tells nobody, one that forgets the group seat's
    holders, one that tells the requester to approve their own request, and
    one that writes two rows to a person who sits on two seats.

    The approvers are the named manager, the named system owner and every
    seeded `it_admin`; the requester is a fresh employee, so any row for them
    is the filing telling the wrong person.
    """
    requester = _plant_user(mcp_env)
    expected = {PRODUCTION_MANAGER.id, PROD_K8S_OWNER.id} | _it_admin_ids()
    assert requester.user_id not in expected

    payload = _structured(
        call_tool(
            bearer(token_for(requester)),
            CREATE,
            {
                "target_user_id": str(requester.user_id),
                "role_id": str(APPROVAL_ROLE),
                "justification": f"fake access request {uuid.uuid4().hex}",
                "idempotency_key": _key(),
            },
        )
    )
    assert payload["decision"] == "approval", "premise: this role needs approval"

    notes = _notes_about(mcp_env, REQUEST_SUBJECT, uuid.UUID(str(payload["request_id"])))
    assert sorted(note.user_id for note in notes) == sorted(expected)
    assert {note.kind for note in notes} == {"approval_needed"}
    assert all(note.read_at is None for note in notes)
    number = _request_number(mcp_env, uuid.UUID(str(payload["request_id"])))
    assert all(number in note.title for note in notes)
    assert _notes_for_user(mcp_env, requester.user_id) == []


def test_auto_grant_for_someone_else_tells_the_target(mcp_env: SeededMcp) -> None:
    """Catches an auto grant that tells nobody, one that tells the requester
    about their own action, and one that tells a person who granted
    themselves access.

    Two calls: the manager files for a report (the report is told, the
    manager is not), and an employee files for themselves (nobody is told).
    """
    manager = _seeded(PRODUCTION_MANAGER)
    report = _plant_user(mcp_env)

    for_other = _structured(
        call_tool(
            bearer(token_for(manager)),
            CREATE,
            {
                "target_user_id": str(report.user_id),
                "role_id": str(LOW_ROLE),
                "justification": f"fake access request {uuid.uuid4().hex}",
                "idempotency_key": _key(),
            },
        )
    )
    assert for_other["decision"] == "auto", "premise: this role grants without approval"
    other_notes = _notes_about(mcp_env, REQUEST_SUBJECT, uuid.UUID(str(for_other["request_id"])))
    assert _pairs(other_notes) == [(report.user_id, "request_granted")]

    employee = _plant_user(mcp_env)
    for_self = _structured(
        call_tool(
            bearer(token_for(employee)),
            CREATE,
            {
                "target_user_id": str(employee.user_id),
                "role_id": str(LOW_ROLE),
                "justification": f"fake access request {uuid.uuid4().hex}",
                "idempotency_key": _key(),
            },
        )
    )
    assert for_self["decision"] == "auto"
    assert _notes_about(mcp_env, REQUEST_SUBJECT, uuid.UUID(str(for_self["request_id"]))) == []
    assert _notes_for_user(mcp_env, employee.user_id) == []


# --- deciding -------------------------------------------------------------------


def test_a_middle_approval_tells_the_requester_who_is_left(mcp_env: SeededMcp) -> None:
    """Catches an approval with seats still open that tells nobody, one that
    tells the target or the approver, and one that names the seat that was
    just decided instead of the one still waiting.
    """
    manager = _seeded(PRODUCTION_MANAGER)
    requester, target = _plant_user(mcp_env), _plant_user(mcp_env)
    request_id = _plant_request(
        mcp_env,
        requester=requester,
        target=target,
        role_id=GRANTABLE_ROLE,
        slots=[ApproverSlot.DEPARTMENT_MANAGER, ApproverSlot.IT_ADMIN],
    )

    payload = _structured(
        call_tool(
            bearer(token_for(manager)),
            APPROVE,
            {"request_id": str(request_id), "idempotency_key": _key()},
        )
    )
    assert payload["chain_complete"] is False, "premise: a seat is still open"

    notes = _notes_about(mcp_env, REQUEST_SUBJECT, request_id)
    assert _pairs(notes) == [(requester.user_id, "request_progress")]
    assert "IT admin" in notes[0].body
    assert "Department manager" not in notes[0].body


def test_the_last_approval_tells_requester_and_target(mcp_env: SeededMcp) -> None:
    """Catches a completing approval that tells only the requester, only the
    target, tells either twice, or tells the approver who just acted.
    """
    manager = _seeded(PRODUCTION_MANAGER)
    requester, target = _plant_user(mcp_env), _plant_user(mcp_env)
    request_id = _plant_request(
        mcp_env,
        requester=requester,
        target=target,
        role_id=GRANTABLE_ROLE,
        slots=[ApproverSlot.DEPARTMENT_MANAGER],
    )

    payload = _structured(
        call_tool(
            bearer(token_for(manager)),
            APPROVE,
            {"request_id": str(request_id), "idempotency_key": _key()},
        )
    )
    assert payload["chain_complete"] is True and payload["failure_code"] is None

    notes = _notes_about(mcp_env, REQUEST_SUBJECT, request_id)
    assert _pairs(notes) == sorted(
        [(requester.user_id, "request_granted"), (target.user_id, "request_granted")]
    )


def test_a_rejection_tells_requester_and_target_with_the_reason(mcp_env: SeededMcp) -> None:
    """Catches a rejection that drops the reason, tells only one of the two
    people, or tells the approver who rejected.
    """
    manager = _seeded(PRODUCTION_MANAGER)
    requester, target = _plant_user(mcp_env), _plant_user(mcp_env)
    request_id = _plant_request(
        mcp_env,
        requester=requester,
        target=target,
        role_id=GRANTABLE_ROLE,
        slots=[ApproverSlot.DEPARTMENT_MANAGER, ApproverSlot.IT_ADMIN],
    )

    _structured(
        call_tool(
            bearer(token_for(manager)),
            REJECT,
            {
                "request_id": str(request_id),
                "reason": "No business need",
                "idempotency_key": _key(),
            },
        )
    )

    notes = _notes_about(mcp_env, REQUEST_SUBJECT, request_id)
    assert _pairs(notes) == sorted(
        [(requester.user_id, "request_rejected"), (target.user_id, "request_rejected")]
    )
    assert all("No business need" in note.body for note in notes)


def test_a_failed_grant_tells_the_requester(mcp_env: SeededMcp) -> None:
    """Catches a failed grant that still says "granted", one that says nothing,
    and one that tells the target about a request that gave them nothing.

    The target holds `payments.initiator` before the approval, so the grant
    fails the SoD re-check on `payments.approver`.
    """
    manager = _seeded(PRODUCTION_MANAGER)
    requester, target = _plant_user(mcp_env), _plant_user(mcp_env)
    _plant_grant(mcp_env, user_id=target.user_id, role_id=SOD_CONFLICTING_ROLE)
    request_id = _plant_request(
        mcp_env,
        requester=requester,
        target=target,
        role_id=SOD_REQUEST_ROLE,
        slots=[ApproverSlot.DEPARTMENT_MANAGER],
    )

    payload = _structured(
        call_tool(
            bearer(token_for(manager)),
            APPROVE,
            {"request_id": str(request_id), "idempotency_key": _key()},
        )
    )
    assert payload["failure_code"] == "SOD_CONFLICT", "premise: the grant failed"

    notes = _notes_about(mcp_env, REQUEST_SUBJECT, request_id)
    assert _pairs(notes) == [(requester.user_id, "request_failed")]
    assert all(note.kind != "request_granted" for note in notes)


# --- taking access away ---------------------------------------------------------


def _by_revoke_access(env: SeededMcp) -> tuple[uuid.UUID, uuid.UUID]:
    target = _plant_user(env)
    grant_id = _plant_grant(env, user_id=target.user_id, role_id=GRANTED_ROLE)
    call_tool(
        bearer(token_for(_seeded(PRODUCTION_MANAGER))),
        REVOKE,
        {"grant_id": str(grant_id), "reason": "fake revoke reason", "idempotency_key": _key()},
    )
    return target.user_id, grant_id


def _by_review(env: SeededMcp) -> tuple[uuid.UUID, uuid.UUID]:
    manager = _seeded(PRODUCTION_MANAGER)
    target = _plant_user(env)
    grant_id = _plant_grant(env, user_id=target.user_id, role_id=GRANTED_ROLE)
    review_id, item_id = uuid.uuid4(), uuid.uuid4()
    sql_execute(
        env.database_url,
        "INSERT INTO access_reviews (id, tenant_id, reviewer_user_id, department_id, system_id,"
        " period, status, due_at, completed_at, created_at, updated_at)"
        " VALUES (:id, :tenant, :reviewer, :department, NULL, '2026-Q1', 'open', NULL, NULL,"
        " now(), now())",
        {
            "id": review_id,
            "tenant": ROWS.tenant_id,
            "reviewer": manager.user_id,
            "department": PRODUCTION_DEPARTMENT,
        },
    )
    sql_execute(
        env.database_url,
        "INSERT INTO review_items (id, tenant_id, review_id, grant_id, flag, decision,"
        " decided_at, created_at, updated_at)"
        " VALUES (:id, :tenant, :review, :grant, 'ok', NULL, NULL, now(), now())",
        {"id": item_id, "tenant": ROWS.tenant_id, "review": review_id, "grant": grant_id},
    )
    call_tool(
        bearer(token_for(manager, principals=("manager",))),
        DECIDE,
        {"item_id": str(item_id), "decision": "revoke", "idempotency_key": _key()},
    )
    return target.user_id, grant_id


def _by_offboarding(env: SeededMcp) -> tuple[uuid.UUID, uuid.UUID]:
    leaver = _plant_user(env, department_id=None)
    grant_id = _plant_grant(env, user_id=leaver.user_id, role_id=OFFBOARD_ROLE)
    call_tool(
        bearer(token_for(_seeded(HR_CALLER), principals=("hr",))),
        OFFBOARD,
        {"user_id": str(leaver.user_id), "end_date": "2026-10-31", "idempotency_key": _key()},
    )
    return leaver.user_id, grant_id


@pytest.mark.parametrize(
    "revoke_by",
    [
        pytest.param(_by_revoke_access, id="revoke_access"),
        pytest.param(_by_review, id="review-revoke"),
        pytest.param(_by_offboarding, id="offboarding"),
    ],
)
def test_every_revoke_path_tells_the_person(
    mcp_env: SeededMcp, revoke_by: Callable[[SeededMcp], tuple[uuid.UUID, uuid.UUID]]
) -> None:
    """Catches a revoke path that skips the notification (a manual revoke, a
    review decision or an offboarding that takes access away silently), one
    that tells the wrong person, and one that leaves the reason out.

    The grant row is read back, so "with the reason" means the reason the
    revoke really stored, whichever path wrote it.
    """
    user_id, grant_id = revoke_by(mcp_env)

    notes = _notes_about(mcp_env, GRANT_SUBJECT, grant_id)
    assert _pairs(notes) == [(user_id, "access_revoked")]
    assert _revoke_reason_of(mcp_env, grant_id) in notes[0].body


# --- a refused change -----------------------------------------------------------


def test_a_failed_change_leaves_no_notification(mcp_env: SeededMcp) -> None:
    """Catches a handler that writes the notification before the seat check
    and outside the change's transaction, so a refused rejection still tells
    the requester, the target or the real approver.

    A refusal writes nothing today either, so this stays green before and
    after; it goes red only on an implementation that notifies too early.
    """
    admin = _seeded(PLATFORM_ADMIN)
    requester, target = _plant_user(mcp_env), _plant_user(mcp_env)
    request_id = _plant_request(
        mcp_env,
        requester=requester,
        target=target,
        role_id=GRANTABLE_ROLE,
        slots=[ApproverSlot.DEPARTMENT_MANAGER],
    )

    result = call_tool(
        bearer(token_for(admin)),
        REJECT,
        {"request_id": str(request_id), "reason": "Not mine to decide", "idempotency_key": _key()},
    )

    assert error_code(result) == "FORBIDDEN"
    assert _notes_about(mcp_env, REQUEST_SUBJECT, request_id) == []
    assert _notes_for_user(mcp_env, requester.user_id) == []
    assert _notes_for_user(mcp_env, target.user_id) == []
