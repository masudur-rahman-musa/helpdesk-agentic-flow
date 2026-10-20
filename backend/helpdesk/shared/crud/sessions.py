"""The chat session row. Its id comes from the token, which `Repository.create` refuses."""

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.core.db import bound_tenant_id
from helpdesk.shared.crud import Repository
from helpdesk.shared.models import Message, MessageRole, Session
from helpdesk.shared.models.conversation import SessionConflictError

CHAT_LIST_LIMIT = 100
"""How many chats the side list holds, newest first. Older ones stay stored."""

CHAT_TITLE_CHARS = 60
"""A chat's title is its first user message cut to this length."""


class ChatSummary(BaseModel):
    """One line of the chat list."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    title: str
    last_seen_at: datetime


async def get_owned_chat(
    session: AsyncSession, *, chat_id: uuid.UUID, user_id: uuid.UUID
) -> Session | None:
    """The chat if it is this user's in the bound tenant, else `None`.

    Missing, a colleague's and another tenant's all read the same, so a caller
    cannot tell them apart.
    """
    row = await Repository(session, Session).get(chat_id)
    if row is None or row.user_id != user_id:
        return None
    return row


async def list_user_chats(session: AsyncSession, *, user_id: uuid.UUID) -> Sequence[ChatSummary]:
    """The user's chats that hold a message, newest first, at most `CHAT_LIST_LIMIT`.

    The tenant filter is written out because the session guard only covers
    queries that load whole rows.
    """
    tenant_id = bound_tenant_id(session)
    first_user_message = (
        select(Message.session_id, func.min(Message.ordinal).label("first_ordinal"))
        .where(Message.tenant_id == tenant_id, Message.role == MessageRole.USER.value)
        .group_by(Message.session_id)
        .subquery()
    )
    result = await session.execute(
        select(Session.id, Session.last_seen_at, Message.content)
        .join(first_user_message, first_user_message.c.session_id == Session.id)
        .join(Message, Message.ordinal == first_user_message.c.first_ordinal)
        .where(Session.tenant_id == tenant_id, Session.user_id == user_id)
        .order_by(Session.last_seen_at.desc(), Session.id)
        .limit(CHAT_LIST_LIMIT)
    )
    return [
        ChatSummary(id=chat_id, title=content[:CHAT_TITLE_CHARS], last_seen_at=seen)
        for chat_id, seen, content in result.all()
    ]


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


OPEN_QUESTION_KEY = "open_question"
"""Where the "I'll file this: ..." question waits for the next message."""


async def get_open_question(session: AsyncSession, *, session_id: uuid.UUID) -> object | None:
    """The stored question as raw JSON, unchecked; `None` when there is none."""
    row = await Repository(session, Session).get(session_id)
    if row is None:
        return None
    return (row.session_metadata or {}).get(OPEN_QUESTION_KEY)


async def set_open_question(
    session: AsyncSession, *, session_id: uuid.UUID, value: dict[str, Any] | None
) -> None:
    """Store the question, or drop it with `None`. A new dict is assigned,
    since SQLAlchemy does not see a change made inside a JSONB value."""
    row = await Repository(session, Session).get(session_id)
    if row is None:
        return
    metadata = dict(row.session_metadata or {})
    if value is None:
        metadata.pop(OPEN_QUESTION_KEY, None)
    else:
        metadata[OPEN_QUESTION_KEY] = value
    row.session_metadata = metadata
    await session.flush()


ASKED_CLARIFYING_KEY = "asked_clarifying_at"
"""When the last clarifying question was asked, so the next one is not."""


async def get_asked_clarifying(session: AsyncSession, *, session_id: uuid.UUID) -> object | None:
    """The stored time as raw JSON, unchecked; `None` when there is none."""
    row = await Repository(session, Session).get(session_id)
    if row is None:
        return None
    return (row.session_metadata or {}).get(ASKED_CLARIFYING_KEY)


async def set_asked_clarifying(
    session: AsyncSession, *, session_id: uuid.UUID, value: str | None
) -> None:
    """Store the time, or drop it with `None`."""
    row = await Repository(session, Session).get(session_id)
    if row is None:
        return
    metadata = dict(row.session_metadata or {})
    if value is None:
        metadata.pop(ASKED_CLARIFYING_KEY, None)
    else:
        metadata[ASKED_CLARIFYING_KEY] = value
    row.session_metadata = metadata
    await session.flush()


ASKED_KIND_KEY = "asked_kind"
"""What the last question asked, with its time, so the next message knows
which question it answers."""


async def get_asked_kind(session: AsyncSession, *, session_id: uuid.UUID) -> object | None:
    """The stored kind as raw JSON, unchecked; `None` when there is none."""
    row = await Repository(session, Session).get(session_id)
    if row is None:
        return None
    return (row.session_metadata or {}).get(ASKED_KIND_KEY)


async def set_asked_kind(
    session: AsyncSession, *, session_id: uuid.UUID, value: object | None
) -> None:
    """Store the kind, or drop it with `None`."""
    row = await Repository(session, Session).get(session_id)
    if row is None:
        return
    metadata = dict(row.session_metadata or {})
    if value is None:
        metadata.pop(ASKED_KIND_KEY, None)
    else:
        metadata[ASKED_KIND_KEY] = value
    row.session_metadata = metadata
    await session.flush()
