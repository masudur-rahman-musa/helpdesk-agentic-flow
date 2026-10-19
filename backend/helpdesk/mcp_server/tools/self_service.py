"""The self-service tools: password reset, account unlock, new ticket, my tickets.

All four act only for the token's user. Reset and unlock are simulated:
each stamps a time on the user's own row, and no real credential or
directory change happens anywhere.
"""

import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field
from sqlalchemy import String
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.mcp_server.core.identity import Identity
from helpdesk.mcp_server.tools.base import MutatingToolInput
from helpdesk.mcp_server.tools.context import ToolContext
from helpdesk.shared.core.errors import NotFoundError
from helpdesk.shared.crud import Repository
from helpdesk.shared.crud.tickets import list_tickets_for_user
from helpdesk.shared.models import Ticket, TicketStatus, User
from helpdesk.shared.models.numbers import NumberKind, format_number

RESET_EVENT_TYPE = "user.password_reset"
"""Success event for `reset_own_password`."""

UNLOCK_EVENT_TYPE = "user.account_unlocked"
"""Declared success event for `unlock_own_account`."""

TICKET_EVENT_TYPE = "ticket.created"
"""Success event for `create_ticket`."""

RESET_TOKEN_PLACEHOLDER = "FAKE-RESET-TOKEN-0000-simulated-nothing-was-issued"
"""Shown instead of a credential. Plainly fake, since the reset is simulated."""

BODY_MAX_LENGTH = 10_000
"""Cap on a ticket body. The column has no limit, so this check is the only one."""

# Read off the column, so the input check and the storage never disagree.
_TITLE_TYPE = Ticket.__table__.c.title.type
assert isinstance(_TITLE_TYPE, String), _TITLE_TYPE
assert _TITLE_TYPE.length is not None, "tickets.title must be a sized column"
TITLE_MAX_LENGTH: int = _TITLE_TYPE.length

TICKETS_LIMIT_MIN = 1
TICKETS_LIMIT_DEFAULT = 50
TICKETS_LIMIT_MAX = 200
"""Page bounds for `list_my_tickets`, the same as the requests list."""


class TicketKind(StrEnum):
    """The four ticket kinds. As an enum, it refuses anything not exactly one of them."""

    PASSWORD_RESET = "password_reset"
    UNLOCK = "unlock"
    GENERAL = "general"
    ESCALATION = "escalation"


class ResetOwnPasswordInput(MutatingToolInput):
    """No fields beyond the key: the user reset is the one on the token."""


class ResetOwnPasswordOutput(BaseModel):
    reset_token: str
    requested_at: datetime


class UnlockOwnAccountInput(MutatingToolInput):
    """No fields beyond the key: the account is the token user's own."""


class UnlockOwnAccountOutput(BaseModel):
    unlocked_at: datetime


class CreateTicketInput(MutatingToolInput):
    kind: TicketKind
    title: str = Field(max_length=TITLE_MAX_LENGTH)
    body: str = Field(max_length=BODY_MAX_LENGTH)


class CreateTicketOutput(BaseModel):
    ticket_id: uuid.UUID
    ticket_number: str
    """What the user is told, like `TCK-0042`; the id stays for code."""

    kind: TicketKind
    title: str
    status: str


class ListMyTicketsInput(BaseModel):
    limit: int = Field(default=TICKETS_LIMIT_DEFAULT, ge=TICKETS_LIMIT_MIN, le=TICKETS_LIMIT_MAX)
    """Checked by the input model, before any database access."""


class TicketSummary(BaseModel):
    """One ticket in a list. The body is left out; a list shows titles."""

    ticket_id: uuid.UUID
    ticket_number: str
    kind: TicketKind
    title: str
    status: TicketStatus
    created_at: datetime


class ListMyTicketsOutput(BaseModel):
    tickets: list[TicketSummary]


async def _stamp_user(session: AsyncSession, identity: Identity, **field: datetime) -> datetime:
    """Set one timestamp flag on the token's own user row."""
    (when,) = field.values()
    user = await Repository(session, User).update(identity.user_id, **field)
    if user is None:
        raise NotFoundError("no such user in this tenant")
    return when


async def reset_own_password(
    ctx: ToolContext, args: ResetOwnPasswordInput
) -> ResetOwnPasswordOutput:
    """Simulated password reset: stamp the flag, display the fake token."""
    when = await _stamp_user(ctx.session, ctx.identity, password_reset_requested_at=ctx.now)
    return ResetOwnPasswordOutput(reset_token=RESET_TOKEN_PLACEHOLDER, requested_at=when)


async def unlock_own_account(
    ctx: ToolContext, args: UnlockOwnAccountInput
) -> UnlockOwnAccountOutput:
    """Simulated account unlock: stamp the flag on the token's user."""
    when = await _stamp_user(ctx.session, ctx.identity, account_unlocked_at=ctx.now)
    return UnlockOwnAccountOutput(unlocked_at=when)


async def create_ticket(ctx: ToolContext, args: CreateTicketInput) -> CreateTicketOutput:
    """File a helpdesk ticket for the token's user, status `open`.

    The tenant comes from the bound session (`Repository.create` stamps it)
    and the user from the token; neither is an argument.
    """
    ticket = await Repository(ctx.session, Ticket).create(
        user_id=ctx.identity.user_id,
        kind=args.kind.value,
        title=args.title,
        body=args.body,
        status=TicketStatus.OPEN.value,
        session_id=None,
    )
    return CreateTicketOutput(
        ticket_id=ticket.id,
        ticket_number=format_number(NumberKind.TICKET, ticket.number),
        kind=args.kind,
        title=ticket.title,
        status=ticket.status,
    )


async def list_my_tickets(ctx: ToolContext, args: ListMyTicketsInput) -> ListMyTicketsOutput:
    """The token user's own tickets, newest first."""
    rows = await list_tickets_for_user(ctx.session, ctx.identity.user_id, args.limit)
    return ListMyTicketsOutput(
        tickets=[
            TicketSummary(
                ticket_id=row.id,
                ticket_number=format_number(NumberKind.TICKET, row.number),
                kind=TicketKind(row.kind),
                title=row.title,
                status=TicketStatus(row.status),
                created_at=row.created_at,
            )
            for row in rows
        ]
    )
