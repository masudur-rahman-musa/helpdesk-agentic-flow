"""The chat session row. Its id comes from the token, which `Repository.create` refuses."""

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
    """The session row for the token's `sid`, created if missing; bumps `last_seen_at`.

    An id already used by another tenant cannot be read, so the insert clashes
    and raises `SessionConflictError` instead of touching that row.
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
