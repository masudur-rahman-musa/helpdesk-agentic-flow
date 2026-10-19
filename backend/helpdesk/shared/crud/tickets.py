"""Ticket reads for the person who opened them."""

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.models import Ticket


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
