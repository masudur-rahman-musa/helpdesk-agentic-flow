"""Giving up your own access with `revoke_access`, through the transport.

Surface under test (written before it exists):

``mcp_server.tools.access.revoke_access``
    Every principal may remove a grant whose ``user_id`` is the token's user,
    with no approval. The rule is a scope: ``employee`` and ``auditor`` gain a
    ``self_only`` cell on the revoke row, which reaches no one else's grant.
    Any other grant goes through today's scope check unchanged. The scope check
    still runs BEFORE the liveness check, so a colleague's dead grant answers
    ``FORBIDDEN``, never ``POLICY_BLOCKED``.
    A self removal stores the reason as ``"Removed by the user: <reason>"`` in
    ``grants.revoke_reason`` and in the ``grant.revoked`` payload. The 1..2000
    bound is on the text the user typed; the prefix counts outside it. Other
    revokes keep the reason as typed. A self removal sends no
    ``access_revoked`` notice.

Fixture cost: everything rides the session-scoped ``mcp_env`` seam. Every case
plants its OWN fresh users (uuid4 ids, fake emails) and its own grants, so no
case sees another's rows. Engines come from ``tests.support.mcp``; this file
builds none. Each case is a sync function, no pytest-asyncio.
"""

import uuid
from datetime import UTC, date, datetime
from typing import Any

import pytest

from scripts.seed import NORDLICHT, generate_tenant
from tests.support.mcp import (
    SeededMcp,
    SeededUser,
    bearer,
    call_tool,
    error_code,
    sql_execute,
    sql_rows,
    token_for,
)

pytestmark = pytest.mark.db

REVOKE = "revoke_access"

# The audit contract, written out so a rename in the code cannot pass unnoticed.
GRANT_REVOKED_EVENT = "grant.revoked"
SUCCEEDED_EVENT = "tool.succeeded"
DENIED_EVENT = "tool.denied"

SELF_PREFIX = "Removed by the user: "
REASON_MAX_LENGTH = 2000

ROWS = generate_tenant(NORDLICHT)
PRODUCTION_DEPARTMENT = ROWS.department_ids["Production"]

PRINCIPALS = (
    "employee",
    "manager",
    "system_owner",
    "hr",
    "it_admin",
    "auditor",
    "platform_admin",
)

# Outside the Production template, so no seeded row touches a fresh user.
ROLE = ROWS.role_ids[("grafana", "viewer")]
GRANTED_AT = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
REVOKED_AT = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
PLANTED_START = date(2019, 3, 7)


def _key() -> str:
    return f"fake-key-{uuid.uuid4().hex}"


def _args(grant_id: uuid.UUID, reason: str = "No longer needed.") -> dict[str, Any]:
    return {"grant_id": str(grant_id), "reason": reason, "idempotency_key": _key()}


def _plant_user(env: SeededMcp) -> SeededUser:
    """One fresh, token-ready, obviously fake Production employee."""
    user_id = uuid.uuid4()
    email = f"fake.giveup.{user_id}@fake.invalid"
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
            "email": email,
            "display_name": "Fake Giveup",
            "password_hash": "not-a-real-hash-this-user-never-logs-in",
            "principals": ["employee"],
            "start_date": PLANTED_START,
        },
    )
    return SeededUser(
        tenant_id=ROWS.tenant_id,
        tenant_slug=NORDLICHT.slug,
        user_id=user_id,
        email=email,
        display_name="Fake Giveup",
        department_id=PRODUCTION_DEPARTMENT,
        principals=("employee",),
        status="active",
    )


def _plant_grant(env: SeededMcp, user: SeededUser, *, live: bool = True) -> uuid.UUID:
    """One committed grant with no request (the shape of department default access)."""
    grant_id = uuid.uuid4()
    sql_execute(
        env.database_url,
        "INSERT INTO grants (id, tenant_id, user_id, role_id, request_id,"
        " granted_by_user_id, granted_at, expires_at, revoked_at, revoke_reason, status,"
        " created_at, updated_at)"
        " VALUES (:id, :tenant, :user, :role, NULL, NULL, :granted_at, NULL,"
        " :revoked_at, :reason, :status, now(), now())",
        {
            "id": grant_id,
            "tenant": ROWS.tenant_id,
            "user": user.user_id,
            "role": ROLE,
            "granted_at": GRANTED_AT,
            "revoked_at": None if live else REVOKED_AT,
            "reason": None if live else "fake historical revocation",
            "status": "active" if live else "revoked",
        },
    )
    return grant_id


def _grant(env: SeededMcp, grant_id: uuid.UUID) -> Any:
    rows = sql_rows(
        env.database_url,
        "SELECT status, revoked_at, revoke_reason, request_id FROM grants WHERE id = :id",
        {"id": grant_id},
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


def _revoked_events(env: SeededMcp, grant_id: uuid.UUID) -> list[Any]:
    return sql_rows(
        env.database_url,
        "SELECT actor_ref, payload FROM audit_events WHERE tenant_id = :tenant"
        " AND event_type = :event_type AND subject_id = :subject ORDER BY seq",
        {"tenant": ROWS.tenant_id, "event_type": GRANT_REVOKED_EVENT, "subject": str(grant_id)},
    )


def _notices_for(env: SeededMcp, user: SeededUser) -> list[Any]:
    return sql_rows(
        env.database_url,
        "SELECT kind FROM notifications WHERE user_id = :user AND kind = 'access_revoked'",
        {"user": user.user_id},
    )


def _revoke(caller: SeededUser, arguments: dict[str, Any], *principals: str) -> Any:
    token = token_for(caller, principals=principals or ("employee",))
    return call_tool(bearer(token), REVOKE, arguments)


def test_an_employee_removes_their_own_access(mcp_env: SeededMcp) -> None:
    """Catches a tool that still refuses an employee their own grant (the old
    matrix had no cell for them), one that leaves the grant `active`, one that
    stores the reason as typed with no self marker, one that files the event
    under another actor, and one that writes the event without the marker. The
    audit delta is the exact ordered list, so a missing or doubled
    `grant.revoked` fails too."""
    caller = _plant_user(mcp_env)
    grant_id = _plant_grant(mcp_env, caller)
    events_before = _audit_types(mcp_env)

    result = _revoke(caller, _args(grant_id, "No longer needed."), "employee")

    assert result.is_error is False, result.content
    row = _grant(mcp_env, grant_id)
    assert row.status == "revoked"
    assert row.revoked_at is not None
    assert row.revoke_reason == "Removed by the user: No longer needed."
    assert _audit_types(mcp_env) == [*events_before, GRANT_REVOKED_EVENT, SUCCEEDED_EVENT]
    events = _revoked_events(mcp_env, grant_id)
    assert len(events) == 1
    assert str(events[0].actor_ref) == str(caller.user_id)
    assert events[0].payload["reason"] == "Removed by the user: No longer needed."


def test_department_default_access_can_be_given_up(mcp_env: SeededMcp) -> None:
    """Catches a rule that lets a person remove only access they asked for (a
    grant with a `request_id`) and refuses the department default one, which
    has no request. The planted grant has `request_id` NULL, asserted as a
    premise, so only a rule keyed on the request would refuse it."""
    caller = _plant_user(mcp_env)
    grant_id = _plant_grant(mcp_env, caller)
    assert _grant(mcp_env, grant_id).request_id is None, "premise: a template grant"

    result = _revoke(caller, _args(grant_id), "employee")

    assert result.is_error is False, result.content
    assert _grant(mcp_env, grant_id).status == "revoked"


@pytest.mark.parametrize("principal", PRINCIPALS)
def test_every_role_may_remove_its_own_access(mcp_env: SeededMcp, principal: str) -> None:
    """Catches a self rule that works for the roles the matrix already admits
    but not for the rest: `employee` and `auditor` have no cell today, and a
    `system_owner` with no system of their own is refused by the `own_systems`
    scope. The fresh caller owns no system and the grant is on a system nobody
    here owns, so only the self rule can admit the call. The reason carries the
    marker for every role, so a self removal is never filed as a plain revoke."""
    caller = _plant_user(mcp_env)
    grant_id = _plant_grant(mcp_env, caller)

    result = _revoke(caller, _args(grant_id, "Done with it."), principal)

    assert result.is_error is False, (principal, result.content)
    row = _grant(mcp_env, grant_id)
    assert row.status == "revoked"
    assert row.revoke_reason == "Removed by the user: Done with it."


def test_an_employee_cannot_remove_a_colleagues_access(mcp_env: SeededMcp) -> None:
    """Catches a self rule that widens to every grant in the company, and a
    denial filed as an outage. Both users are in the same tenant and the same
    department and the grant is live, so only the `self_only` scope can refuse.
    The grant must stay `active`, one `tool.denied` is written and no
    `grant.revoked`."""
    caller = _plant_user(mcp_env)
    colleague = _plant_user(mcp_env)
    grant_id = _plant_grant(mcp_env, colleague)
    events_before = _audit_types(mcp_env)

    result = _revoke(caller, _args(grant_id), "employee")

    assert error_code(result) == "FORBIDDEN"
    row = _grant(mcp_env, grant_id)
    assert row.status == "active"
    assert row.revoked_at is None
    assert _audit_types(mcp_env) == [*events_before, DENIED_EVENT]
    assert _revoked_events(mcp_env, grant_id) == []


@pytest.mark.parametrize("principal", ["employee", "auditor"])
def test_a_colleagues_dead_grant_answers_forbidden_not_policy_blocked(
    mcp_env: SeededMcp, principal: str
) -> None:
    """Catches a handler that checks liveness before the scope: a stranger
    could learn that a colleague's access is already gone. The colleague's
    grant is revoked, so a liveness-first handler answers `POLICY_BLOCKED`;
    only scope-first answers `FORBIDDEN`."""
    caller = _plant_user(mcp_env)
    colleague = _plant_user(mcp_env)
    grant_id = _plant_grant(mcp_env, colleague, live=False)

    result = _revoke(caller, _args(grant_id), principal)

    assert error_code(result) == "FORBIDDEN"


def test_giving_up_access_sends_no_notice(mcp_env: SeededMcp) -> None:
    """Catches a self removal that tells the person about what they just did.
    A control in the same test: a manager removing a colleague's grant DOES
    send the holder an `access_revoked` notice, so the query would find a
    notice if one existed."""
    caller = _plant_user(mcp_env)
    own = _plant_grant(mcp_env, caller)
    holder = _plant_user(mcp_env)
    theirs = _plant_grant(mcp_env, holder)

    gave_up = _revoke(caller, _args(own), "employee")
    removed = _revoke(_plant_user(mcp_env), _args(theirs), "manager")

    assert gave_up.is_error is False, gave_up.content
    assert removed.is_error is False, removed.content
    assert len(_notices_for(mcp_env, holder)) == 1, "premise: a manager revoke sends a notice"
    assert _notices_for(mcp_env, caller) == []


def test_giving_up_a_removed_grant_answers_policy_blocked(mcp_env: SeededMcp) -> None:
    """Catches a self rule that skips the liveness check: the caller's own
    grant is already revoked, so a second removal must answer `POLICY_BLOCKED`
    and rewrite nothing. The caller owns the grant, so the scope cannot refuse."""
    caller = _plant_user(mcp_env)
    grant_id = _plant_grant(mcp_env, caller, live=False)
    events_before = _audit_types(mcp_env)

    result = _revoke(caller, _args(grant_id), "employee")

    assert error_code(result) == "POLICY_BLOCKED"
    row = _grant(mcp_env, grant_id)
    assert row.revoked_at == REVOKED_AT
    assert row.revoke_reason == "fake historical revocation"
    assert GRANT_REVOKED_EVENT not in _audit_types(mcp_env)[len(events_before) :]


@pytest.mark.parametrize(
    ("length", "accepted"),
    [(1, True), (REASON_MAX_LENGTH, True), (0, False), (REASON_MAX_LENGTH + 1, False)],
)
def test_the_self_reason_boundary_is_inclusive_at_1_and_2000(
    mcp_env: SeededMcp, length: int, accepted: bool
) -> None:
    """Catches a bound that counts the prefix (2000 typed characters refused),
    one that is exclusive at either end, and one that is missing (an empty or
    2001-character reason stored). Accepted rows must hold the prefix and the
    exact text; refused rows must stay `active`."""
    caller = _plant_user(mcp_env)
    grant_id = _plant_grant(mcp_env, caller)
    typed = "r" * length

    result = _revoke(caller, _args(grant_id, typed), "employee")

    row = _grant(mcp_env, grant_id)
    if accepted:
        assert result.is_error is False, result.content
        assert row.revoke_reason == SELF_PREFIX + typed
        assert _revoked_events(mcp_env, grant_id)[0].payload["reason"] == SELF_PREFIX + typed
    else:
        assert error_code(result) == "VALIDATION"
        assert row.status == "active"
        assert row.revoked_at is None


def test_a_manager_revoke_in_their_department_keeps_the_reason_as_typed(
    mcp_env: SeededMcp,
) -> None:
    """Catches a prefix added to every revoke: someone else's removal is not
    "removed by the user". The manager and the holder are different fresh
    Production users, so this is the old wider path, and the stored reason and
    the event reason must be exactly what was typed."""
    manager = _plant_user(mcp_env)
    holder = _plant_user(mcp_env)
    grant_id = _plant_grant(mcp_env, holder)

    result = _revoke(manager, _args(grant_id, "Left the project."), "manager")

    assert result.is_error is False, result.content
    assert _grant(mcp_env, grant_id).revoke_reason == "Left the project."
    events = _revoked_events(mcp_env, grant_id)
    assert events[0].payload["reason"] == "Left the project."
    assert str(events[0].actor_ref) == str(manager.user_id)
