"""`list_my_tickets`, the person's own tickets, newest first.

Surface under test (written before it exists):

``shared.models.TicketStatus``
    ``StrEnum`` with exactly ``open`` and ``solved``, exported beside
    ``Ticket``. ``create_ticket`` writes ``TicketStatus.OPEN``.

``mcp_server.tools.self_service``
    ``ListMyTicketsInput(limit)`` with ``limit`` 1 to 200, default 50.
    ``TicketSummary`` with exactly ``ticket_id``, ``ticket_number``, ``kind``,
    ``title``, ``status``, ``created_at``. ``ListMyTicketsOutput(tickets)``.
    ``list_my_tickets(ctx, args)`` answers the token user's own tickets in the
    token's company, newest first, cut at ``limit``.

``mcp_server.tools.registry``
    ``list_my_tickets`` registered as a read.

Cost: the cases marked `db` ride the session-scoped ``mcp_env`` seam (one
seeded throwaway database). Tickets are filed through ``create_ticket`` itself
and found again by their own ids, so other tests' tickets cannot change an
answer; no case builds an engine.
"""

import re
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from helpdesk.mcp_server.tools import self_service
from helpdesk.mcp_server.tools.registry import REGISTRY
from helpdesk.shared.models import TicketStatus
from tests.support.mcp import (
    SeededMcp,
    SeededUser,
    bearer,
    call_tool,
    error_code,
    token_for,
)

LIST_MY_TICKETS = "list_my_tickets"
CREATE_TICKET = "create_ticket"

# The exact bounds; pinned as values, not as a band.
LIMIT_MIN = 1
LIMIT_DEFAULT = 50
LIMIT_MAX = 200

SIX_FIELDS = {"ticket_id", "ticket_number", "kind", "title", "status", "created_at"}

BASE_TIME = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)


def _structured(result: Any) -> dict[str, Any]:
    assert result.is_error is False, result.content
    assert result.structured_content is not None, "a successful call carries structured content"
    content: dict[str, Any] = result.structured_content
    return content


def _file(caller: SeededUser) -> tuple[str, str]:
    """File one ticket through the real tool as `caller`; returns (id, number).

    Planting goes through `create_ticket` so no test builds its own engine.
    Each call is its own transaction, so a later call has a later `created_at`.
    """
    payload = _structured(
        call_tool(
            bearer(token_for(caller)),
            CREATE_TICKET,
            {
                "kind": "general",
                "title": f"fake ticket {uuid.uuid4().hex}",
                "body": "fake body that a list must leave out",
                "idempotency_key": f"fake-key-{uuid.uuid4().hex}",
            },
        )
    )
    return str(payload["ticket_id"]), str(payload["ticket_number"])


def _listed(caller: SeededUser, arguments: dict[str, Any]) -> list[dict[str, Any]]:
    payload = _structured(call_tool(bearer(token_for(caller)), LIST_MY_TICKETS, arguments))
    rows: list[dict[str, Any]] = payload["tickets"]
    return rows


@pytest.mark.db
def test_the_list_holds_only_the_callers_tickets_newest_first(
    mcp_env: SeededMcp,
) -> None:
    """Catches a query that drops the user filter (a colleague's ticket shows),
    one that drops the company guard (the other company's ticket shows), one
    that sorts oldest first or by insert order, and one that shows the id
    instead of a `TCK-` number. The caller files the older ticket FIRST, so a
    list in insert order and a list newest first give different answers."""
    me = mcp_env.probe
    colleague = mcp_env.tenant_a
    assert colleague.user_id != me.user_id and colleague.tenant_id == me.tenant_id
    older_id, older_number = _file(me)
    colleague_id, _ = _file(colleague)
    other_company_id, _ = _file(mcp_env.tenant_b)
    newer_id, newer_number = _file(me)

    rows = _listed(me, {"limit": LIMIT_MAX})

    ids = [row["ticket_id"] for row in rows]
    assert colleague_id not in ids
    assert other_company_id not in ids
    assert [i for i in ids if i in {older_id, newer_id}] == [newer_id, older_id]
    by_id = {row["ticket_id"]: row for row in rows}
    assert by_id[newer_id]["ticket_number"] == newer_number
    assert by_id[older_id]["ticket_number"] == older_number
    assert all(re.fullmatch(r"TCK-\d{4,}", row["ticket_number"]) for row in rows)


@pytest.mark.db
def test_the_limit_keeps_the_newest_rows(
    mcp_env: SeededMcp,
) -> None:
    """Catches a limit that is validated but never reaches the query (more than
    two come back), and a limit applied before the sort (the two oldest come
    back). Three are filed in order, so the oldest-first insert order would
    answer the first two."""
    me = mcp_env.probe
    oldest_id, _ = _file(me)
    middle_id, _ = _file(me)
    newest_id, _ = _file(me)

    rows = _listed(me, {"limit": 2})

    assert [row["ticket_id"] for row in rows] == [newest_id, middle_id]
    assert oldest_id not in [row["ticket_id"] for row in rows]


def test_a_row_carries_exactly_the_six_fields() -> None:
    """Catches a summary that leaks the `body` into a list (or grows any
    field beyond the six), and an output that renames its `tickets` list.
    Compared as a set so a missing field and an extra one both fail."""
    assert set(self_service.TicketSummary.model_fields) == SIX_FIELDS
    assert set(self_service.ListMyTicketsOutput.model_fields) == {"tickets"}
    row = self_service.TicketSummary(
        ticket_id=uuid.uuid4(),
        ticket_number="TCK-0042",
        kind=self_service.TicketKind.GENERAL,
        title="fake title",
        status=TicketStatus.OPEN,
        created_at=BASE_TIME,
    )
    assert set(row.model_dump()) == SIX_FIELDS
    assert "body" not in row.model_dump()


@pytest.mark.parametrize("limit", [LIMIT_MIN, LIMIT_MAX])
def test_the_limit_bounds_are_inclusive(limit: int) -> None:
    """Catches an exclusive bound (`gt=1` refuses 1, `lt=200` refuses 200).
    Both ends are rows because a one-sided check cannot tell the two apart."""
    assert self_service.ListMyTicketsInput(limit=limit).limit == limit


def test_the_default_limit_is_50() -> None:
    """Catches a drifted default (every caller who omits `limit` silently gets
    another page size), pinned as the exact value."""
    assert self_service.ListMyTicketsInput().limit == LIMIT_DEFAULT == 50


@pytest.mark.parametrize("limit", [LIMIT_MIN - 1, LIMIT_MAX + 1])
def test_the_input_model_refuses_a_limit_of_0_or_201(limit: int) -> None:
    """Catches a bound that is not on the input model at all (a handler that
    clamps or checks after the query would let these construct)."""
    with pytest.raises(ValidationError):
        self_service.ListMyTicketsInput(limit=limit)


@pytest.mark.db
@pytest.mark.parametrize("limit", [LIMIT_MIN - 1, LIMIT_MAX + 1])
def test_a_limit_of_0_or_201_is_refused_as_validation(
    mcp_env: SeededMcp,
    limit: int,
) -> None:
    """Catches a tool that answers a page for an out-of-range limit by clamping
    it, and one that fails with a crash instead of the typed `VALIDATION`
    code. The caller owns a ticket, so a clamp would answer a page, not an
    error."""
    me = mcp_env.probe
    _file(me)

    result = call_tool(bearer(token_for(me)), LIST_MY_TICKETS, {"limit": limit})

    assert error_code(result) == "VALIDATION"


def test_the_tool_is_registered_as_a_read() -> None:
    """Catches a tool written but never registered (it would not exist on the
    wire), and one registered as mutating (callers would owe an idempotency
    key to read their own list)."""
    spec = REGISTRY[LIST_MY_TICKETS]
    assert spec.mutating is False
    assert spec.input_model is self_service.ListMyTicketsInput
    assert spec.output_model is self_service.ListMyTicketsOutput


def test_ticket_status_values_are_exactly_open_and_solved() -> None:
    """Catches a third status slipping in, or a renamed word: the stored word
    `open` is what the rest of the system already reads."""
    assert {member.value for member in TicketStatus} == {"open", "solved"}
    assert TicketStatus.OPEN.value == "open"
    assert TicketStatus.SOLVED.value == "solved"


@pytest.mark.db
def test_a_new_ticket_is_written_open_from_the_enum(
    mcp_env: SeededMcp,
) -> None:
    """Catches a `create_ticket` that stores any word but the enum's `open`
    (the answer and the stored row, read back through the list, must both say
    the enum's value)."""
    me = mcp_env.probe
    ticket_id, _ = _file(me)

    rows = {row["ticket_id"]: row for row in _listed(me, {"limit": LIMIT_MAX})}

    assert rows[ticket_id]["status"] == TicketStatus.OPEN.value == "open"
