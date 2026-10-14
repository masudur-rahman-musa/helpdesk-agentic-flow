"""The conversation session row.

A named helper rather than a plain `Repository` because the session row's
primary key is supplied by the caller (it is the token's `sid`, and
`Repository.create` refuses `id` on purpose).
"""

import uuid
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.core.db import bound_tenant_id
from helpdesk.shared.crud import Repository
from helpdesk.shared.models import Session
from helpdesk.shared.models.conversation import SessionConflictError


async def get_or_create_session(
    session: AsyncSession,
    *,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    now: datetime,
) -> Session:
    """The bound tenant's conversation row for `session_id`, created if absent.

    The id is the token's `sid` (a project rule: nothing here comes from a tool
    argument), so it is written explicitly instead of defaulted — the value
    the gateway sees as `X-Session` and the value this row is stored under
    have to be one. `Repository.create` refuses `id` by design, which is why
    this helper builds the model itself.

    `started_at` is stamped once and never moved; `last_seen_at` moves on
    every turn. Pinned by
    `test_conversation_models.py::test_get_or_create_session_reuses_the_row_and_moves_last_seen_at`,
    which fails both for a helper that inserts a second row and for one that
    loads without touching `last_seen_at`.

    A `session_id` that already names ANOTHER tenant's row is neither loaded
    nor overwritten: the guarded read cannot see it, so the insert collides on
    the primary key and becomes a typed `SessionConflictError`. That is the
    only reachable answer — `sessions.id` is table-wide, so "create a new row
    instead" is not something the schema allows.
    """
    existing = await Repository(session, Session).get(session_id)
    if existing is not None:
        existing.last_seen_at = now
        await session.flush()
        return existing

    row = Session(
        id=session_id,
        tenant_id=bound_tenant_id(session),
        user_id=user_id,
        started_at=now,
        last_seen_at=now,
        session_metadata={},
    )
    session.add(row)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise SessionConflictError("this session id is already in use") from exc
    return row
