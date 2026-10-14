"""The conversation message store and its history window.

Named helpers rather than a plain `Repository` because the history window has
a fixed order and a fixed size that every caller must read the same way.
"""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.core.db import bound_tenant_id
from helpdesk.shared.models import Message, MessageRole

HISTORY_LIMIT = 20
"""How many past messages one turn sees (the design step 1).

Pinned by `test_conversation_models.py::test_max_message_chars_is_exactly_the_planned_value`. It bounds a prompt, so widening it widens every turn's
token bill; `MAX_MESSAGE_CHARS` bounds each row inside it.
"""


async def append_message(
    session: AsyncSession,
    *,
    session_id: uuid.UUID,
    role: MessageRole,
    content: str,
    tool_calls: dict[str, Any] | None = None,
    citations: list[Any] | None = None,
) -> Message:
    """Store one message on the bound tenant's conversation.

    The content is NOT checked here: `check_message_content` runs at the edge
    of the turn, before any row at all is written, which is the whole point of
    the pre-check. A caller that reaches this function with
    an oversized string gets the column's own error, and
    `test_agent_turn.py::test_a_message_one_character_over_the_cap_is_refused_before_any_row`
    is what proves no such caller exists on the turn path.

    `ordinal` is left to the database: it is an identity column, and a caller
    that numbered its own rows would race every other turn on the table.
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
    """The last `limit` messages of this conversation, OLDEST FIRST.

    Ordered by `ordinal` and by nothing else. `created_at` cannot order these
    rows: Postgres's `now()` is transaction time, so every message one turn
    writes carries an identical timestamp — pinned by
    `test_conversation_models.py::test_list_recent_messages_returns_the_last_twenty_oldest_first`,
    whose premise assertion proves the twenty-five planted rows share one
    value before it asserts the order.

    The statement takes the TAIL (descending, limited) and reverses it in
    Python, because "the last twenty" cannot be expressed as an ascending
    LIMIT. The session guard filters the SELECT, so another tenant's session
    id reads as an empty window rather than as somebody else's conversation.
    """
    result = await session.execute(
        select(Message)
        .where(Message.session_id == session_id)
        .order_by(Message.ordinal.desc())
        .limit(limit)
    )
    return list(reversed(result.scalars().all()))
