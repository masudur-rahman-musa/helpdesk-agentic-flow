"""The admin side of tickets: `list_open_tickets`.

Only `it_admin` and `platform_admin` may call it. The reading lives in the
tickets CRUD helper; this is the wire contract.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from helpdesk.mcp_server.tools.context import ToolContext
from helpdesk.mcp_server.tools.self_service import (
    TICKETS_LIMIT_DEFAULT,
    TICKETS_LIMIT_MAX,
    TICKETS_LIMIT_MIN,
    TicketKind,
)
from helpdesk.shared.crud.tickets import list_open_tickets_with_opener
from helpdesk.shared.models.numbers import NumberKind, format_number


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


__all__ = [
    "ListOpenTicketsInput",
    "ListOpenTicketsOutput",
    "OpenTicketRow",
    "list_open_tickets",
]
