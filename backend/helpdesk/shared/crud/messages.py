"""Chat messages and the history window every caller must read the same way."""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.core.db import bound_tenant_id
from helpdesk.shared.models import Message, MessageRole

HISTORY_LIMIT = 20
"""How many past messages one turn sees. Raising it raises every turn's token cost."""


async def append_message(
    session: AsyncSession,
    *,
    session_id: uuid.UUID,
    role: MessageRole,
    content: str,
    tool_calls: dict[str, Any] | None = None,
    citations: list[Any] | None = None,
) -> Message:
    """Store one message. Length is checked earlier, before any row is written.

    The database numbers `ordinal`, so two turns cannot race on it.
    """
    row = Message(
        tenant_id=bound_tenant_id(session),
        session_id=session_id,
        role=role.value,
        content=content,
        tool_calls=tool_calls,
        citations=citations,
    )
    session.add(row)
    await session.flush()
    return row


async def list_recent_messages(
    session: AsyncSession, session_id: uuid.UUID, limit: int = HISTORY_LIMIT
) -> Sequence[Message]:
    """The last `limit` messages, oldest first.

    Ordered by `ordinal`, not time: all messages of one turn share a timestamp.
    """
    result = await session.execute(
        select(Message)
        .where(Message.session_id == session_id)
        .order_by(Message.ordinal.desc())
        .limit(limit)
    )
    return list(reversed(result.scalars().all()))
