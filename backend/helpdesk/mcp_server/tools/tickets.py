"""The admin side of tickets: `list_open_tickets` and `solve_ticket`.

Only `it_admin` and `platform_admin` may call them. Neither is offered to the
agent: solving is a person's action on the web page. The reading lives in the
tickets CRUD helper and the solving in the tickets service; this is the wire
contract.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from helpdesk.mcp_server.services.tickets.solve import solve
from helpdesk.mcp_server.tools.base import MutatingToolInput
from helpdesk.mcp_server.tools.context import ToolContext
from helpdesk.mcp_server.tools.self_service import (
    TICKETS_LIMIT_DEFAULT,
    TICKETS_LIMIT_MAX,
    TICKETS_LIMIT_MIN,
    TicketKind,
)
from helpdesk.shared.crud.tickets import list_open_tickets_with_opener
from helpdesk.shared.models import TicketStatus
from helpdesk.shared.models.numbers import NumberKind, format_number

SOLVED_EVENT_TYPE = "ticket.solved"
"""Success event for `solve_ticket`."""


class ListOpenTicketsInput(BaseModel):
    limit: int = Field(default=TICKETS_LIMIT_DEFAULT, ge=TICKETS_LIMIT_MIN, le=TICKETS_LIMIT_MAX)
    """The same bounds as My tickets, checked before any database access."""


class OpenTicketRow(BaseModel):
    """One open ticket. No body and no status: a list shows titles, and every row is open."""

    ticket_id: uuid.UUID
    ticket_number: str
    kind: TicketKind
    title: str
    opened_by: str
    """The opener's display name, never their id or email."""

    created_at: datetime


class ListOpenTicketsOutput(BaseModel):
    tickets: list[OpenTicketRow]


async def list_open_tickets(ctx: ToolContext, args: ListOpenTicketsInput) -> ListOpenTicketsOutput:
    """Every open ticket in the token's tenant, oldest first. Nothing is written."""
    rows = await list_open_tickets_with_opener(ctx.session, args.limit)
    return ListOpenTicketsOutput(
        tickets=[
            OpenTicketRow(
                ticket_id=ticket.id,
                ticket_number=format_number(NumberKind.TICKET, ticket.number),
                kind=TicketKind(ticket.kind),
                title=ticket.title,
                opened_by=opened_by,
                created_at=ticket.created_at,
            )
            for ticket, opened_by in rows
        ]
    )


class SolveTicketInput(MutatingToolInput):
    """One ticket id, plus the idempotency key the base adds. No solver field:
    the solver is the token's user."""

    ticket_id: uuid.UUID


class SolveTicketOutput(BaseModel):
    """The solved ticket, with when and by whom it was first solved."""

    ticket_id: uuid.UUID
    ticket_number: str
    status: TicketStatus
    solved_at: datetime
    solved_by_user_id: uuid.UUID


async def solve_ticket(ctx: ToolContext, args: SolveTicketInput) -> SolveTicketOutput:
    """Mark one open ticket of the token's tenant solved, by the token's user.

    The time is set here, not by the caller. An already solved ticket answers
    its first solve. An unknown id and another tenant's id give `NOT_FOUND`.
    """
    ticket = await solve(
        ctx.session, ticket_id=args.ticket_id, solver_id=ctx.identity.user_id, now=ctx.now
    )
    assert ticket.solved_at is not None and ticket.solved_by_user_id is not None, (
        "solve either stamped this ticket or found it stamped"
    )
    return SolveTicketOutput(
        ticket_id=ticket.id,
        ticket_number=format_number(NumberKind.TICKET, ticket.number),
        status=TicketStatus(ticket.status),
        solved_at=ticket.solved_at,
        solved_by_user_id=ticket.solved_by_user_id,
    )


__all__ = [
    "SOLVED_EVENT_TYPE",
    "ListOpenTicketsInput",
    "ListOpenTicketsOutput",
    "OpenTicketRow",
    "SolveTicketInput",
    "SolveTicketOutput",
    "list_open_tickets",
    "solve_ticket",
]
