"""`solve`: the one writer of a ticket's solved fields.

It sits in the shared service, not the tool, so any later caller solves
through the same code.
"""

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.core.errors import NotFoundError
from helpdesk.shared.crud.tickets import fetch_ticket
from helpdesk.shared.models import Ticket, TicketStatus


async def solve(
    session: AsyncSession,
    *,
    ticket_id: uuid.UUID,
    solver_id: uuid.UUID,
    now: datetime,
) -> Ticket:
    """Mark one open ticket solved, or answer the solve it already has.

    The clock and the solver are passed in. A ticket solved before keeps its
    first time and solver whatever the key, so a second admin cannot move
    them (`test_a_solved_ticket_keeps_its_first_stamp`).

    An unknown id and another tenant's id both answer `NOT_FOUND`, and nothing
    is written. `FORBIDDEN` would confirm the id names a real ticket somewhere.
    """
    ticket = await fetch_ticket(session, ticket_id)
    if ticket is None:
        raise NotFoundError(f"no ticket {ticket_id} in this tenant")
    if ticket.status == TicketStatus.SOLVED.value:
        return ticket

    ticket.status = TicketStatus.SOLVED.value
    ticket.solved_at = now
    ticket.solved_by_user_id = solver_id
    await session.flush()
    return ticket
