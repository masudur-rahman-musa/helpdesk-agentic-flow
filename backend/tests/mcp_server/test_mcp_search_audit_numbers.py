"""`search_audit` rows carry the readable request number.

Surface under test (written before it exists):

``mcp_server.tools.read.AuditEventRow.subject_number`` — ``str | None``. It is
``format_number(NumberKind.ACCESS_REQUEST, n)`` only when the row's
``subject_type`` is ``access_request`` and its ``subject_id`` is the id of a
request found in the CALLER's company. Every other row answers ``None``. The
stored ``subject_id`` is never changed. The number is looked up when the page
is read, with one batched, tenant-filtered read of the page's request ids.

Every case calls as an ``auditor`` (``Scope.ANY``) so the scope filter's own
reads of ``access_requests`` never run: the only thing that can read that table
for this call is the number lookup. Each case files its audit rows under its
own ``event_type`` marker, which the search filters on, so the pages asserted
are exact. Requests are real rows made through the repository, the way
production makes them, then read back for their stored number.

Cost: rides the session-scoped ``mcp_env`` seam (one seeded database). The
probes below reach the database through the shared probe helpers in
`tests/support/mcp.py`. Per-test isolation is the marker and the fresh request rows.
"""

import asyncio
import json
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine

from helpdesk.shared.core.db import tenant_session
from helpdesk.shared.crud import Repository
from helpdesk.shared.models import AccessRequest
from helpdesk.shared.models.numbers import NumberKind, format_number
from scripts.seed import CUAN, NORDLICHT, TenantRows, UserRow, generate_tenant
from tests.support.mcp import (  # noqa: F401 - fixtures are resolved by name
    SeededMcp,
    SeededUser,
    bearer,
    call_tool,
    mcp_env,
    mcp_pg,
    mcp_redis_base_url,
    mcp_redis_url,
    probe_session_factory,
    seeded_mcp_db,
    sql_execute,
    sql_rows,
    token_for,
)

pytestmark = pytest.mark.db

SEARCH = "search_audit"
OCCURRED_AT = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)

HERE = generate_tenant(NORDLICHT)
THERE = generate_tenant(CUAN)


def _first_user(rows: TenantRows) -> UserRow:
    return rows.users_by_department[rows.spec.departments[0]][2]


def _first_role(rows: TenantRows) -> uuid.UUID:
    return next(iter(rows.role_ids.values()))


def _caller(rows: TenantRows) -> SeededUser:
    user = _first_user(rows)
    return SeededUser(
        tenant_id=rows.tenant_id,
        tenant_slug=rows.spec.slug,
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        department_id=rows.department_ids[user.department],
        principals=tuple(sorted(user.principals)),
        status="active",
    )


def _plant_request(database_url: str, rows: TenantRows) -> uuid.UUID:
    """One pending request in `rows`' company, drawn through the repository."""
    user = _first_user(rows)

    async def scenario() -> uuid.UUID:
        factory = probe_session_factory(database_url)
        async with tenant_session(factory, rows.tenant_id) as session:
            row = await Repository(session, AccessRequest).create(
                requester_user_id=user.id,
                target_user_id=user.id,
                role_id=_first_role(rows),
                justification=f"fake audit numbered request {uuid.uuid4().hex}",
                requested_expiry_days=365,
                risk_class="critical",
                decision="approval",
                decision_reasons=[],
                citations=[],
                status="pending",
                approver_user_ids=[],
                approvals=[],
                session_id=None,
                trace_id=None,
                idempotency_key=None,
            )
            return row.id

    return asyncio.run(scenario())


def _stored_number(database_url: str, request_id: uuid.UUID) -> int:
    found = sql_rows(
        database_url, "SELECT number FROM access_requests WHERE id = :id", {"id": request_id}
    )
    assert len(found) == 1, f"expected one access_requests row for {request_id}"
    return int(found[0][0])


def _shown(database_url: str, request_id: uuid.UUID) -> str:
    return format_number(NumberKind.ACCESS_REQUEST, _stored_number(database_url, request_id))


def _plant_event(
    database_url: str,
    tenant_id: uuid.UUID,
    *,
    marker: str,
    subject_type: str,
    subject_id: str | None,
) -> None:
    """One committed `audit_events` row written AROUND the tool (fake digests)."""
    sql_execute(
        database_url,
        "INSERT INTO audit_events (id, tenant_id, occurred_at, actor_kind, actor_ref,"
        " session_id, trace_id, event_type, subject_type, subject_id, payload, prev_hash, hash,"
        " created_at, updated_at)"
        " VALUES (:id, :tenant, :occurred_at, 'system', NULL, NULL, :trace, :event_type,"
        " :subject_type, :subject_id, CAST(:payload AS jsonb), :prev_hash, :hash, now(), now())",
        {
            "id": uuid.uuid4(),
            "tenant": tenant_id,
            "occurred_at": OCCURRED_AT,
            "trace": f"fake-trace-{uuid.uuid4().hex}",
            "event_type": marker,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "payload": json.dumps({"fake": True}),
            "prev_hash": uuid.uuid4().hex * 2,
            "hash": uuid.uuid4().hex * 2,
        },
    )


def _marker() -> str:
    return f"fake.numbers.{uuid.uuid4().hex}"


def _search_rows(caller: SeededUser, marker: str) -> list[dict[str, Any]]:
    result = call_tool(
        bearer(token_for(caller, principals=("auditor",))), SEARCH, {"event_type": marker}
    )
    assert result.is_error is False, result.content
    assert result.structured_content is not None
    events: list[dict[str, Any]] = result.structured_content["events"]
    return events


def _by_subject(events: list[dict[str, Any]]) -> dict[tuple[str, str | None], str | None]:
    return {(row["subject_type"], row["subject_id"]): row["subject_number"] for row in events}


@contextmanager
def _count_selects_on(table: str) -> Iterator[list[str]]:
    """Every SELECT statement that reads `table`, on any engine in this process.

    The server runs in-process, so a listener on the `Engine` class sees its
    reads. Removed on exit so no other test sees it.
    """
    seen: list[str] = []
    pattern = re.compile(rf"\bFROM\s+{table}\b", re.IGNORECASE)

    def listener(*args: Any) -> None:
        statement = str(args[2])
        if statement.lstrip().upper().startswith("SELECT") and pattern.search(statement):
            seen.append(statement)

    event.listen(Engine, "before_cursor_execute", listener)
    try:
        yield seen
    finally:
        event.remove(Engine, "before_cursor_execute", listener)


def test_a_request_event_carries_its_request_number(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches an implementation that leaves `subject_number` null for a request
    event, formats a different number than the one stored on the row (a
    position or a second draw), or replaces `subject_id` with the number: the
    row must show the padded `REQ-` form of the stored number AND keep the
    stored uuid in `subject_id`."""
    request_id = _plant_request(mcp_env.database_url, HERE)
    marker = _marker()
    _plant_event(
        mcp_env.database_url,
        HERE.tenant_id,
        marker=marker,
        subject_type="access_request",
        subject_id=str(request_id),
    )

    events = _search_rows(_caller(HERE), marker)

    assert len(events) == 1
    assert events[0]["subject_id"] == str(request_id)
    assert events[0]["subject_number"] == _shown(mcp_env.database_url, request_id)
    assert re.fullmatch(r"REQ-\d{4,}", events[0]["subject_number"])


def test_events_that_are_not_requests_have_no_number(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches an implementation that numbers any row whose `subject_id` happens
    to be a request's id, without checking `subject_type`. Every row below
    carries the id of a REAL request in the caller's own company, so the
    subject type is the only thing that can keep it from a number; the one
    genuine request row on the same page proves the lookup runs at all, so a
    lookup that never numbers anything cannot pass."""
    request_id = _plant_request(mcp_env.database_url, HERE)
    marker = _marker()
    cases = (
        ("grant", str(request_id)),
        ("user", str(request_id)),
        ("tool", str(request_id)),
        ("tool", "create_ticket"),  # what `ticket.created` really names
    )
    for subject_type, subject_id in cases:
        _plant_event(
            mcp_env.database_url,
            HERE.tenant_id,
            marker=marker,
            subject_type=subject_type,
            subject_id=subject_id,
        )
    _plant_event(
        mcp_env.database_url,
        HERE.tenant_id,
        marker=marker,
        subject_type="access_request",
        subject_id=str(request_id),
    )

    shown = _by_subject(_search_rows(_caller(HERE), marker))

    for case in cases:
        assert shown[case] is None, case
    assert shown[("access_request", str(request_id))] == _shown(mcp_env.database_url, request_id)


def test_a_request_from_another_company_gets_no_number(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches a lookup that is not filtered by the caller's company: the
    caller is in one company and the audit row names a REAL request that
    carries a number in the other, so only the tenant filter can answer null
    (a random uuid would be missing either way). A request of the caller's own
    company sits on the same page and must be numbered, so a lookup that
    numbers nothing cannot pass either. The foreign number must appear
    nowhere in the answer."""
    foreign_id = _plant_request(mcp_env.database_url, HERE)
    own_id = _plant_request(mcp_env.database_url, THERE)
    foreign_number = _shown(mcp_env.database_url, foreign_id)
    marker = _marker()
    for request_id in (foreign_id, own_id):
        _plant_event(
            mcp_env.database_url,
            THERE.tenant_id,
            marker=marker,
            subject_type="access_request",
            subject_id=str(request_id),
        )

    events = _search_rows(_caller(THERE), marker)

    shown = _by_subject(events)
    assert shown[("access_request", str(foreign_id))] is None
    assert shown[("access_request", str(own_id))] == _shown(mcp_env.database_url, own_id)
    own_number = shown[("access_request", str(own_id))]
    if foreign_number != own_number:
        assert foreign_number not in json.dumps(events)


def test_a_subject_id_that_is_not_a_uuid_gets_no_number(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches a lookup that parses `subject_id` as a uuid without a guard: the
    free-text column holds `not-a-uuid`, and the page must still load with null
    for that row (not an INTERNAL error), while a real request row on the same
    page is still numbered, so one bad id does not take the batch down."""
    request_id = _plant_request(mcp_env.database_url, HERE)
    marker = _marker()
    for subject_id in ("not-a-uuid", str(request_id)):
        _plant_event(
            mcp_env.database_url,
            HERE.tenant_id,
            marker=marker,
            subject_type="access_request",
            subject_id=subject_id,
        )

    shown = _by_subject(_search_rows(_caller(HERE), marker))

    assert shown[("access_request", "not-a-uuid")] is None
    assert shown[("access_request", str(request_id))] == _shown(mcp_env.database_url, request_id)


def test_one_page_of_requests_is_numbered_in_one_read(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches a lookup that reads one request per row (a read inside the loop):
    three request events on one page must all be numbered, each with its own
    number, by exactly one select on `access_requests`. The caller holds
    `Scope.ANY`, so the scope filter's reads cannot add to the count."""
    request_ids = [_plant_request(mcp_env.database_url, HERE) for _ in range(3)]
    marker = _marker()
    for request_id in request_ids:
        _plant_event(
            mcp_env.database_url,
            HERE.tenant_id,
            marker=marker,
            subject_type="access_request",
            subject_id=str(request_id),
        )

    with _count_selects_on("access_requests") as selects:
        shown = _by_subject(_search_rows(_caller(HERE), marker))

    for request_id in request_ids:
        assert shown[("access_request", str(request_id))] == _shown(
            mcp_env.database_url, request_id
        )
    assert len(set(shown.values())) == 3
    assert len(selects) == 1, selects
