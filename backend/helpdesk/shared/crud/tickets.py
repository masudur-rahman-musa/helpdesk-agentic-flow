"""Ticket reads: a person's own tickets, and every open ticket for an admin."""

import uuid
from collections.abc import Sequence

from sqlalchemy import Row, select
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.models import Ticket, TicketStatus, User


async def list_tickets_for_user(
    session: AsyncSession, user_id: uuid.UUID, limit: int
) -> Sequence[Ticket]:
    """This user's tickets, newest first, at most `limit`.

    Whole rows are selected, so the session's tenant guard scopes the query.
    The number breaks ties, since two tickets can share a `created_at`.
    """
    result = await session.execute(
        select(Ticket)
        .where(Ticket.user_id == user_id)
        .order_by(Ticket.created_at.desc(), Ticket.number.desc())
        .limit(limit)
    )
    return result.scalars().all()


async def list_open_tickets_with_opener(
    session: AsyncSession, limit: int
) -> Sequence[Row[tuple[Ticket, str]]]:
    """Every open ticket with its opener's display name, oldest first, at most `limit`.

    Both models are in the select, so the session's tenant guard scopes the
    ticket and the user alike; a name from another tenant cannot join in.
    The number breaks ties, lower first.
    """
    result = await session.execute(
        select(Ticket, User.display_name)
        .join(User, User.id == Ticket.user_id)
        .where(Ticket.status == TicketStatus.OPEN.value)
        .order_by(Ticket.created_at, Ticket.number)
        .limit(limit)
    )
    return result.all()
