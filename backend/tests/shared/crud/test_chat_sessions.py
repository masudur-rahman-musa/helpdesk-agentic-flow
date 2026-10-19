"""The chat list a person sees on the left of the chat page.

What is under test (does NOT exist yet; every case is red until it lands):

``helpdesk.shared.crud.sessions``
    ``CHAT_LIST_LIMIT = 100`` and ``CHAT_TITLE_CHARS = 60``.
    ``async list_user_chats(session, *, user_id) -> Sequence[ChatSummary]``:
        the user's chats in the bound tenant, newest ``last_seen_at`` first, at
        most 100. ``ChatSummary`` has ``id``, ``title`` and ``last_seen_at``.
        The title is the first user message of the chat, cut to 60 characters.
        A chat with no messages is left out.
    ``async get_owned_chat(session, *, chat_id, user_id) -> Session | None``.

Cost: the `db` marker. Newest-first order, the user and tenant filters and the
100 cap are what only Postgres shows. The engine and factory come from the
shared session fixtures in ``conftest.py``; each test seeds its own tenant, so
the tests share nothing.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from helpdesk.shared.core.db import tenant_session
from helpdesk.shared.crud.messages import append_message
from helpdesk.shared.crud.sessions import (
    CHAT_LIST_LIMIT,
    CHAT_TITLE_CHARS,
    ChatSummary,
    get_or_create_session,
    get_owned_chat,
    list_user_chats,
)
from helpdesk.shared.models import MessageRole, User
from tests.support.seed import seed_tenant

pytestmark = pytest.mark.db

T0 = datetime(2026, 6, 2, 10, 0, tzinfo=UTC)


async def _chat(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    seen: datetime,
    lines: list[tuple[MessageRole, str]],
) -> uuid.UUID:
    """One chat in the bound tenant with the given messages, oldest first."""
    chat_id = uuid.uuid4()
    await get_or_create_session(db, session_id=chat_id, user_id=user_id, now=seen)
    for role, content in lines:
        await append_message(db, session_id=chat_id, role=role, content=content)
    return chat_id


def test_pinned_values() -> None:
    """Catches a list that quietly changes how many chats or characters it shows.

    The exact numbers are the plan's; a range check would admit a wrong one.
    """
    assert CHAT_LIST_LIMIT == 100
    assert CHAT_TITLE_CHARS == 60


def test_list_returns_newest_first_with_first_message_title(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Catches a list ordered by creation, by id or by nothing, and a title taken
    from the last or an assistant message instead of the first user message.

    The OLDER chat is created second, so ordering by insert order puts it on
    top and fails; its `last_seen_at` is earlier, so only ordering by that
    column puts the other first. Each chat carries a later user message and an
    assistant message, so a title read from the wrong row differs.
    """

    async def scenario() -> None:
        seeded = await seed_tenant(session_factory, "chat-list-order")
        async with tenant_session(session_factory, seeded.tenant_id) as db:
            newer = await _chat(
                db,
                user_id=seeded.user_id,
                seen=T0 + timedelta(hours=2),
                lines=[
                    (MessageRole.USER, "newer first question"),
                    (MessageRole.ASSISTANT, "newer answer"),
                    (MessageRole.USER, "newer second question"),
                ],
            )
            older = await _chat(
                db,
                user_id=seeded.user_id,
                seen=T0,
                lines=[
                    (MessageRole.USER, "older first question"),
                    (MessageRole.ASSISTANT, "older answer"),
                ],
            )

        async with tenant_session(session_factory, seeded.tenant_id) as db:
            chats = await list_user_chats(db, user_id=seeded.user_id)

        assert [(c.id, c.title) for c in chats] == [
            (newer, "newer first question"),
            (older, "older first question"),
        ]
        assert chats[0].last_seen_at == T0 + timedelta(hours=2)
        assert isinstance(chats[0], ChatSummary)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("length", "expected_length"),
    [(60, 60), (61, 60)],
)
def test_title_is_cut_at_60_characters(
    session_factory: async_sessionmaker[AsyncSession], length: int, expected_length: int
) -> None:
    """Catches a title cut at 59 or 61, or not cut at all.

    60 characters must come back whole and 61 must lose exactly the last one,
    so an off-by-one on either side fails one of the two rows. The text is made
    of distinct characters so the kept part is proved to be the prefix.
    """
    text = ("0123456789abcdefghijklmnopqrstuvwxyz" * 3)[:length]

    async def scenario() -> None:
        seeded = await seed_tenant(session_factory, f"chat-title-{length}")
        async with tenant_session(session_factory, seeded.tenant_id) as db:
            await _chat(
                db,
                user_id=seeded.user_id,
                seen=T0,
                lines=[(MessageRole.USER, text)],
            )
        async with tenant_session(session_factory, seeded.tenant_id) as db:
            chats = await list_user_chats(db, user_id=seeded.user_id)
        assert len(chats) == 1
        assert len(chats[0].title) == expected_length
        assert chats[0].title == text[:60]

    asyncio.run(scenario())


def test_list_skips_empty_chats_and_other_users(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Catches a list that shows empty chats, or chats of another user or tenant.

    The foreign tenant's chat is stored under THIS user's id (the foreign key
    only checks the user exists), so the user filter cannot hide it; only the
    tenant filter can. The other user's chat is in the same tenant, so only
    the user filter hides it. The empty chat has a newer `last_seen_at` than
    the real one, so a list without the message join would put it on top.
    """

    async def scenario() -> None:
        seeded = await seed_tenant(session_factory, "chat-list-own")
        foreign = await seed_tenant(session_factory, "chat-list-foreign")

        async with tenant_session(session_factory, seeded.tenant_id) as db:
            colleague = User(
                tenant_id=seeded.tenant_id,
                department_id=seeded.department_id,
                email="fake.colleague@chat-list-own.example.test",
                display_name="Fake Colleague",
                password_hash="argon2id$obviously-fake-test-hash",
                principals=["employee"],
                status="active",
                start_date=T0.date(),
                end_date=None,
            )
            db.add(colleague)
            await db.flush()
            colleague_id = colleague.id

            mine = await _chat(
                db,
                user_id=seeded.user_id,
                seen=T0,
                lines=[(MessageRole.USER, "my real chat")],
            )
            await _chat(db, user_id=seeded.user_id, seen=T0 + timedelta(hours=5), lines=[])
            await _chat(
                db,
                user_id=colleague_id,
                seen=T0 + timedelta(hours=6),
                lines=[(MessageRole.USER, "the colleague's chat")],
            )

        async with tenant_session(session_factory, foreign.tenant_id) as db:
            await _chat(
                db,
                user_id=seeded.user_id,
                seen=T0 + timedelta(hours=7),
                lines=[(MessageRole.USER, "a chat stored in another tenant")],
            )

        async with tenant_session(session_factory, seeded.tenant_id) as db:
            chats = await list_user_chats(db, user_id=seeded.user_id)
            owned = await get_owned_chat(db, chat_id=mine, user_id=seeded.user_id)

        assert [(c.id, c.title) for c in chats] == [(mine, "my real chat")]
        assert owned is not None and owned.id == mine

    asyncio.run(scenario())


def test_list_stops_at_100_chats(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Catches a list with no cap, a cap of 99 or 101, or one that drops the
    NEWEST chats instead of the oldest.

    101 chats are stored with rising `last_seen_at`; the list must hold exactly
    100 and the one missing must be the very first, the oldest.
    """

    async def scenario() -> None:
        seeded = await seed_tenant(session_factory, "chat-list-cap")
        ids: list[uuid.UUID] = []
        async with tenant_session(session_factory, seeded.tenant_id) as db:
            for index in range(101):
                ids.append(
                    await _chat(
                        db,
                        user_id=seeded.user_id,
                        seen=T0 + timedelta(minutes=index),
                        lines=[(MessageRole.USER, f"fake question {index}")],
                    )
                )
        async with tenant_session(session_factory, seeded.tenant_id) as db:
            chats = await list_user_chats(db, user_id=seeded.user_id)

        assert len(chats) == 100
        assert [c.id for c in chats] == list(reversed(ids[1:]))
        assert ids[0] not in {c.id for c in chats}

    asyncio.run(scenario())


def test_get_owned_chat_is_none_for_missing_other_user_and_other_tenant(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Catches an ownership check that returns another user's or another
    tenant's chat, or raises for a missing one instead of answering None.

    The third row asks tenant B's bound session for tenant A's chat with tenant
    A's own user id, so the user match is satisfied and only the tenant bound
    to the session can refuse.
    """

    async def scenario() -> None:
        own = await seed_tenant(session_factory, "chat-owned-a")
        other = await seed_tenant(session_factory, "chat-owned-b")
        async with tenant_session(session_factory, own.tenant_id) as db:
            chat = await _chat(
                db,
                user_id=own.user_id,
                seen=T0,
                lines=[(MessageRole.USER, "fake question")],
            )

        async with tenant_session(session_factory, own.tenant_id) as db:
            assert (await get_owned_chat(db, chat_id=chat, user_id=own.user_id)) is not None
            assert await get_owned_chat(db, chat_id=uuid.uuid4(), user_id=own.user_id) is None
            assert await get_owned_chat(db, chat_id=chat, user_id=other.user_id) is None
        async with tenant_session(session_factory, other.tenant_id) as db:
            assert await get_owned_chat(db, chat_id=chat, user_id=own.user_id) is None

    asyncio.run(scenario())
