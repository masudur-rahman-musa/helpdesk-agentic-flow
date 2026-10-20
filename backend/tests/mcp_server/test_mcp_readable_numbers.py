"""Readable numbers in the tool outputs.

Surface under test (written before it exists):

``create_ticket`` answers with ``ticket_number`` (a string like ``TCK-0001``).
``create_access_request``, ``list_my_requests`` rows and ``list_pending_approvals``
rows carry ``request_number`` (a string like ``REQ-0001``). The shown form is
``format_number`` of the number stored on the row.

Every case reads the stored number around the tool, with SQL, and compares the
shown form to it. The seeded tenants already hold numbered rows and parallel
tests file new ones, so no case assumes the count starts at 1: it asserts the
number is the stored one, that it is above what was there before, and that the
shown form is the exact padded string. The exact `TCK-0001` / `REQ-0001`
padding is pinned in ``tests/shared/test_readable_numbers.py``.

Cost: rides the session-scoped ``mcp_env`` seam. Per-test isolation inside it
is a unique idempotency key and a unique justification.
"""

import asyncio
import re
import uuid
from typing import Any

import pytest

from helpdesk.shared.core.db import tenant_session
from helpdesk.shared.crud import Repository
from helpdesk.shared.models import AccessRequest
from helpdesk.shared.models.numbers import NumberKind, format_number
from helpdesk.shared.policy.types import ApproverSlot
from scripts.seed import NORDLICHT, UserRow, generate_tenant
from tests.support.mcp import (
    SeededMcp,
    SeededUser,
    bearer,
    call_tool,
    probe_session_factory,
    sql_rows,
    token_for,
)

CREATE_TICKET = "create_ticket"
CREATE_REQUEST = "create_access_request"
LIST_MY_REQUESTS = "list_my_requests"
LIST_PENDING = "list_pending_approvals"
APPROVE = "approve_request"
REJECT = "reject_request"

ROWS = generate_tenant(NORDLICHT)

# Production index 2 has no planted anomaly (the create tests use it too); the
# deployer role on `prod-k8s` is outside its template, so the request is filed
# `pending` and no grant is made.
CLEAN_USER = ROWS.users_by_department["Production"][2]
APPROVAL_ROLE = ROWS.role_ids[("prod-k8s", "deployer")]
# Two Production users no other file uses, for the planted-queue case.
APPROVER = ROWS.users_by_department["Production"][33]
QUEUE_TARGET = ROWS.users_by_department["Production"][41]
# The one who files the planted requests: in the same company as the target,
# the role and the approver, so every lookup finds the row.
REQUESTER = ROWS.users_by_department["Production"][63]
GRANTABLE_ROLE = ROWS.role_ids[("grafana", "viewer")]
# The seeded manager of every department is index 0; the seat below names them.
DEPARTMENT_MANAGER = ROWS.users_by_department["Production"][0]
DECIDE_TARGETS = {
    APPROVE: ROWS.users_by_department["Production"][48],
    REJECT: ROWS.users_by_department["Production"][61],
}


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


def _structured(result: Any) -> dict[str, Any]:
    assert result.is_error is False, result.content
    assert result.structured_content is not None, "a successful call carries structured content"
    content: dict[str, Any] = result.structured_content
    return content


def _counter(database_url: str, tenant_id: uuid.UUID, kind: NumberKind) -> int:
    rows = sql_rows(
        database_url,
        "SELECT last FROM reference_counters WHERE tenant_id = :t AND kind = :k",
        {"t": tenant_id, "k": kind.value},
    )
    return int(rows[0][0]) if rows else 0


def _stored_number(database_url: str, table: str, row_id: uuid.UUID) -> int:
    rows = sql_rows(
        database_url,
        f"SELECT number FROM {table} WHERE id = :id",
        {"id": row_id},
    )
    assert len(rows) == 1, f"expected one {table} row for {row_id}"
    return int(rows[0][0])


def _plant_request(
    database_url: str,
    *,
    requester: SeededUser,
    target: UserRow,
    role_id: uuid.UUID,
    approvals: list[dict[str, Any]],
) -> uuid.UUID:
    """One pending request, made through the repository like production does."""

    async def scenario() -> uuid.UUID:
        factory = probe_session_factory(database_url)
        async with tenant_session(factory, requester.tenant_id) as session:
            row = await Repository(session, AccessRequest).create(
                requester_user_id=requester.user_id,
                target_user_id=target.id,
                role_id=role_id,
                justification=f"fake numbered request {uuid.uuid4().hex}",
                requested_expiry_days=365,
                risk_class="critical",
                decision="approval",
                decision_reasons=[],
                citations=[],
                status="pending",
                approver_user_ids=[],
                approvals=approvals,
                session_id=None,
                trace_id=None,
                idempotency_key=None,
            )
            return row.id

    return asyncio.run(scenario())


def test_create_ticket_answers_with_its_ticket_number(
    mcp_env: SeededMcp,
) -> None:
    """Catches a tool that answers without `ticket_number`, shows a number
    other than the one stored on the row (a second draw from the counter), or
    shows it unpadded or with the request prefix: the shown string must be
    exactly the padded `TCK-` form of the stored number, and that number must
    be new (above the counter's earlier value)."""
    caller = mcp_env.probe
    before = _counter(mcp_env.database_url, caller.tenant_id, NumberKind.TICKET)
    title = f"fake ticket {uuid.uuid4().hex}"

    payload = _structured(
        call_tool(
            bearer(token_for(caller)),
            CREATE_TICKET,
            {
                "kind": "general",
                "title": title,
                "body": "fake body",
                "idempotency_key": f"fake-key-{uuid.uuid4().hex}",
            },
        )
    )

    rows = sql_rows(
        mcp_env.database_url, "SELECT id, number FROM tickets WHERE title = :t", {"t": title}
    )
    assert len(rows) == 1
    stored = int(rows[0][1])
    assert stored > before
    assert payload["ticket_number"] == format_number(NumberKind.TICKET, stored)
    assert re.fullmatch(r"TCK-\d{4,}", payload["ticket_number"])
    assert _counter(mcp_env.database_url, caller.tenant_id, NumberKind.TICKET) >= stored


def test_create_access_request_answers_with_its_request_number(
    mcp_env: SeededMcp,
) -> None:
    """Catches a tool that files the request without drawing a number, answers
    without `request_number`, or draws twice (the shown number differs from the
    stored one): the shown string must be the padded `REQ-` form of the number
    stored on the new row, above the counter's earlier value."""
    caller = _seeded(CLEAN_USER)
    before = _counter(mcp_env.database_url, caller.tenant_id, NumberKind.ACCESS_REQUEST)

    payload = _structured(
        call_tool(
            bearer(token_for(caller)),
            CREATE_REQUEST,
            {
                "target_user_id": str(caller.user_id),
                "role_id": str(APPROVAL_ROLE),
                "justification": f"fake numbered ask {uuid.uuid4().hex}",
                "idempotency_key": f"fake-key-{uuid.uuid4().hex}",
            },
        )
    )

    stored = _stored_number(
        mcp_env.database_url, "access_requests", uuid.UUID(str(payload["request_id"]))
    )
    assert stored > before
    assert payload["request_number"] == format_number(NumberKind.ACCESS_REQUEST, stored)
    assert re.fullmatch(r"REQ-\d{4,}", payload["request_number"])


def test_my_requests_and_pending_approvals_carry_the_number(
    mcp_env: SeededMcp,
) -> None:
    """Catches a summary or queue row that leaves `request_number` out, or
    fills it from anything but the row's own stored number (a position in the
    list, or the database id): two requests are planted so a row count or
    index would give the same string to both, and each row is compared to the
    number read back from its own table row."""
    caller = _seeded(REQUESTER)
    approver = _seeded(APPROVER)
    mine = _plant_request(
        mcp_env.database_url,
        requester=caller,
        target=QUEUE_TARGET,
        role_id=GRANTABLE_ROLE,
        approvals=[],
    )
    queued_first = _plant_request(
        mcp_env.database_url,
        requester=caller,
        target=QUEUE_TARGET,
        role_id=GRANTABLE_ROLE,
        approvals=[
            {"slot": ApproverSlot.DEPARTMENT_MANAGER.value, "user_id": str(approver.user_id)}
        ],
    )
    queued_second = _plant_request(
        mcp_env.database_url,
        requester=caller,
        target=QUEUE_TARGET,
        role_id=GRANTABLE_ROLE,
        approvals=[
            {"slot": ApproverSlot.DEPARTMENT_MANAGER.value, "user_id": str(approver.user_id)}
        ],
    )

    mine_payload = _structured(
        call_tool(bearer(token_for(caller)), LIST_MY_REQUESTS, {"limit": 200})
    )
    queue_payload = _structured(
        call_tool(
            bearer(token_for(approver, principals=("it_admin",))), LIST_PENDING, {"limit": 200}
        )
    )

    my_row = next(row for row in mine_payload["requests"] if row["request_id"] == str(mine))
    assert my_row["request_number"] == format_number(
        NumberKind.ACCESS_REQUEST, _stored_number(mcp_env.database_url, "access_requests", mine)
    )
    shown: set[str] = set()
    for planted in (queued_first, queued_second):
        row = next(r for r in queue_payload["approvals"] if r["request_id"] == str(planted))
        expected = format_number(
            NumberKind.ACCESS_REQUEST,
            _stored_number(mcp_env.database_url, "access_requests", planted),
        )
        assert row["request_number"] == expected
        shown.add(row["request_number"])
    assert len(shown) == 2


@pytest.mark.parametrize("tool", [APPROVE, REJECT])
def test_approve_and_reject_answer_with_the_request_number(
    mcp_env: SeededMcp,
    tool: str,
) -> None:
    """Catches a deciding tool whose answer names the request only by its
    database id: the person who decides reads the number back, so the answer
    must carry the padded `REQ-` form of the number stored on the decided row,
    on both the approve and the reject path."""
    manager = _seeded(DEPARTMENT_MANAGER)
    request_id = _plant_request(
        mcp_env.database_url,
        requester=_seeded(REQUESTER),
        target=DECIDE_TARGETS[tool],
        role_id=GRANTABLE_ROLE,
        approvals=[
            {"slot": ApproverSlot.DEPARTMENT_MANAGER.value, "user_id": str(manager.user_id)}
        ],
    )
    arguments: dict[str, Any] = {
        "request_id": str(request_id),
        "idempotency_key": f"fake-key-{uuid.uuid4().hex}",
    }
    if tool == REJECT:
        arguments["reason"] = "fake rejection"

    payload = _structured(call_tool(bearer(token_for(manager)), tool, arguments))

    stored = _stored_number(mcp_env.database_url, "access_requests", request_id)
    assert payload["request_number"] == format_number(NumberKind.ACCESS_REQUEST, stored)
