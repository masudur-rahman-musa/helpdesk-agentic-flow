"""An earlier change §6: `notify()`, called directly against Postgres.

Written before the implementation exists. Expected surface (the implementer
must provide exactly this):

``helpdesk.notifications.notify``
    ``async def notify(session, *, user_id, kind, title, body, subject_type,
    subject_id) -> Notification`` — appends ONE `notifications` row inside the
    caller's transaction. The tenant is read off the session with
    ``helpdesk.core.db.bound_tenant_id`` and never taken from an argument: there is no tenant parameter at all. The row is stored
    unread (``read_at is None``).

    It refuses, BEFORE anything is added to the session:
      * a `title` longer than ``NOTIFICATION_TITLE_MAX_LENGTH`` and a `body`
        longer than ``NOTIFICATION_BODY_MAX_LENGTH`` — a ``ToolError`` whose
        ``code`` is ``ErrorCode.VALIDATION`` (a project rule: the check is
        against the store's own constant, and the boundary is inclusive);
      * a `kind` outside ``NotificationKind`` — the same code;
      * a recipient outside the bound tenant — ``NotFoundError``
        (``ErrorCode.NOT_FOUND``), never "created for nobody".

The refusal type is asserted as ``ToolError`` + the typed code rather than as
a class name: plan §6 writes "ValidationError", which is not a name
`helpdesk.errors` defines, and the repo's error contract is the the design
code carried by a `ToolError` subclass (the shape
`models/conversation.py::MessageTooLongError` already uses). Whatever the
class is called, the code is what crosses the boundary.

An earlier change §6 adds the two READS beside the writer, in the same file and on the
same fixtures (no new fixture, a project rule):

``helpdesk.notifications.list`` (exported from ``helpdesk.notifications``)
    ``NotificationPage`` — a frozen dataclass carrying
    ``notifications: Sequence[Notification]`` and ``unread_count: int``.
    ``async def list_for_user(session, *, user_id, unread_only, limit)
    -> NotificationPage`` — one person's own rows, newest ``created_at``
    first with the notification id breaking a tie (the direction is
    DESCENDING on both columns, the repo's `(time, id)` convention read
    backwards; the plan leaves it open, so this file fixes it). ``limit``
    bounds the ROWS only: ``unread_count`` is every unread row of that user,
    which is what the bell counts, so a page-derived count is a bell that
    goes quiet at fifty-one.

``helpdesk.notifications.mark`` (exported from ``helpdesk.notifications``)
    ``async def mark_read(session, *, user_id, notification_id, now)
    -> Notification`` — stamps ``read_at`` with the clock it is HANDED when
    the row is unread, answers an already-read row untouched, and raises
    ``helpdesk.errors.NotFoundError`` (``ErrorCode.NOT_FOUND``) for a row
    that is absent or belongs to somebody else.

a project rule: the session factory is the shared one from `conftest.py` --
one engine for the whole run, schema created once -- and one session-scoped
foreign tenant, built once. Per-test isolation is a namespace, not a rebuild -- every
case seeds its OWN tenant with `_seed.seed_tenant` and asserts over that
tenant's rows only, so no case can see another's notifications.

No pytest-asyncio: each case is a sync function driving one `asyncio.run`.
"""

import asyncio
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from helpdesk.mcp_server.services.notifications.list import list_for_user
from helpdesk.mcp_server.services.notifications.mark import mark_read
from helpdesk.shared.core.db import tenant_session
from helpdesk.shared.core.errors import ErrorCode, NotFoundError, ToolError
from helpdesk.shared.crud import Repository
from helpdesk.shared.models import Notification, User
from helpdesk.shared.models.notification import (
    NOTIFICATION_BODY_MAX_LENGTH,
    NOTIFICATION_TITLE_MAX_LENGTH,
    NotificationKind,
)
from helpdesk.shared.notifications.notify import notify
from tests.support.seed import SeededTenant, seed_tenant

TITLE = "fake notification title"
BODY = "fake notification body for the test suite"
SUBJECT_TYPE = "grant"


@dataclass(frozen=True)
class NotificationFacts:
    """What one `notifications` row says, copied out before its session closes.

    A detached ORM object would refresh against a closed session, so every
    value the assertions need is read inside the transaction that owns it.
    """

    tenant_id: uuid.UUID
    user_id: uuid.UUID
    kind: str
    title: str
    body: str
    subject_type: str | None
    subject_id: str | None
    read_at: Any


def _facts(row: Notification) -> NotificationFacts:
    return NotificationFacts(
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        kind=str(row.kind),
        title=row.title,
        body=row.body,
        subject_type=None if row.subject_type is None else str(row.subject_type),
        subject_id=None if row.subject_id is None else str(row.subject_id),
        read_at=row.read_at,
    )


@pytest.fixture(scope="session")
def notification_db(
    session_factory: async_sessionmaker[AsyncSession],
) -> Iterator[tuple[async_sessionmaker[AsyncSession], SeededTenant]]:
    """The shared session factory, plus one FOREIGN tenant.

    The foreign tenant is fully seeded so the cross-tenant case names a user
    that really exists somewhere: a `notify` that answered `NOT_FOUND` for
    every id would otherwise pass it for the wrong reason.
    """

    async def prepare() -> SeededTenant:
        return await seed_tenant(session_factory, "notify-foreign-tenant")

    foreign = asyncio.run(prepare())
    yield session_factory, foreign


def _own_tenant(factory: async_sessionmaker[AsyncSession]) -> SeededTenant:
    """A tenant of this case's own: the per-test namespace."""

    async def prepare() -> SeededTenant:
        return await seed_tenant(factory, f"notify-{uuid.uuid4().hex[:12]}")

    return asyncio.run(prepare())


def _notify_ok(
    factory: async_sessionmaker[AsyncSession],
    tenant: SeededTenant,
    *,
    user_id: uuid.UUID | None = None,
    title: str = TITLE,
    body: str = BODY,
    subject_id: str | None = None,
) -> list[NotificationFacts]:
    """One committed `notify`, then everything this tenant now holds."""

    async def scenario() -> list[NotificationFacts]:
        async with tenant_session(factory, tenant.tenant_id) as session:
            await notify(
                session,
                user_id=tenant.user_id if user_id is None else user_id,
                kind=NotificationKind.GRANT_EXPIRED,
                title=title,
                body=body,
                subject_type=SUBJECT_TYPE,
                subject_id=str(tenant.grant_id) if subject_id is None else subject_id,
            )
        async with tenant_session(factory, tenant.tenant_id) as session:
            rows = await Repository(session, Notification).list()
            return [_facts(row) for row in rows]

    return asyncio.run(scenario())


def _notify_refused(
    factory: async_sessionmaker[AsyncSession],
    tenant: SeededTenant,
    *,
    user_id: uuid.UUID | None = None,
    kind: NotificationKind = NotificationKind.GRANT_EXPIRED,
    title: str = TITLE,
    body: str = BODY,
) -> tuple[ToolError, int, int]:
    """A refused `notify`, with the rows pending IN its session and committed after.

    The pending count is read INSIDE the failed call's own transaction, where
    a `session.add` made before the check would still be sitting: that is what
    turns "refused" into "refused before any row is written". The committed
    count is read afterwards, in case the row was flushed and survived.
    """
    captured: list[ToolError] = []

    async def scenario() -> tuple[int, int]:
        async with tenant_session(factory, tenant.tenant_id) as session:
            try:
                await notify(
                    session,
                    user_id=tenant.user_id if user_id is None else user_id,
                    kind=kind,
                    title=title,
                    body=body,
                    subject_type=SUBJECT_TYPE,
                    subject_id=str(tenant.grant_id),
                )
            except ToolError as exc:
                captured.append(exc)
            # Autoflush: a row added before the check is written here and
            # comes back in this list.
            pending = len(await Repository(session, Notification).list())
        async with tenant_session(factory, tenant.tenant_id) as session:
            committed = len(await Repository(session, Notification).list())
        return pending, committed

    pending, committed = asyncio.run(scenario())
    assert captured, "notify was expected to refuse and did not raise a ToolError"
    return captured[0], pending, committed


def _rows_of(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> list[NotificationFacts]:
    async def scenario() -> list[NotificationFacts]:
        async with tenant_session(factory, tenant_id) as session:
            rows = await Repository(session, Notification).list()
            return [_facts(row) for row in rows]

    return asyncio.run(scenario())


# --- the happy path -----------------------------------------------------------


def test_notify_stores_one_unread_row_under_the_sessions_bound_tenant(
    notification_db: tuple[async_sessionmaker[AsyncSession], SeededTenant],
) -> None:
    """a project rule and criterion 2 together. `notify` takes no tenant
    argument, so the only place `tenant_id` can come from is the session:
    catches a row stamped from the RECIPIENT's own user row (which would
    silently follow a mis-resolved id into another tenant) or left to a
    column default. Also catches a row stored already read, which would make
    every expiry notification invisible to row 23's unread list the moment it
    was written.
    """
    factory, _ = notification_db
    tenant = _own_tenant(factory)

    rows = _notify_ok(factory, tenant)

    assert len(rows) == 1
    row = rows[0]
    assert row.tenant_id == tenant.tenant_id
    assert row.user_id == tenant.user_id
    assert row.kind == "grant_expired"
    assert row.title == TITLE
    assert row.body == BODY
    assert row.subject_type == SUBJECT_TYPE
    assert row.subject_id == str(tenant.grant_id)
    assert row.read_at is None


# --- the two length boundaries ----------------------


def test_a_title_of_exactly_the_cap_is_accepted(
    notification_db: tuple[async_sessionmaker[AsyncSession], SeededTenant],
) -> None:
    """The inclusive half of the boundary: catches an off-by-one `>=` that
    refuses the longest title the store is declared to hold, so a legitimate
    expiry notice is never written and the holder is never told."""
    factory, _ = notification_db
    tenant = _own_tenant(factory)
    at_cap = "t" * NOTIFICATION_TITLE_MAX_LENGTH

    rows = _notify_ok(factory, tenant, title=at_cap)

    assert [row.title for row in rows] == [at_cap]


def test_a_title_one_character_over_the_cap_is_refused_before_any_row_is_written(
    notification_db: tuple[async_sessionmaker[AsyncSession], SeededTenant],
) -> None:
    """The exclusive half: catches a missing pre-check, which leaves the
    length to the column and turns an over-long title into a database error
    raised in the middle of the expiry sweep — after the grant has already
    been marked expired and its audit event appended."""
    factory, _ = notification_db
    tenant = _own_tenant(factory)

    error, pending, committed = _notify_refused(
        factory, tenant, title="t" * (NOTIFICATION_TITLE_MAX_LENGTH + 1)
    )

    assert error.code is ErrorCode.VALIDATION
    assert pending == 0
    assert committed == 0


def test_a_body_of_exactly_the_cap_is_accepted(
    notification_db: tuple[async_sessionmaker[AsyncSession], SeededTenant],
) -> None:
    """The inclusive half for the second cap: catches one check written
    against the title's constant for both fields, which would refuse every
    body over 200 characters while the plan allows 2000."""
    factory, _ = notification_db
    tenant = _own_tenant(factory)
    at_cap = "b" * NOTIFICATION_BODY_MAX_LENGTH

    rows = _notify_ok(factory, tenant, body=at_cap)

    assert [row.body for row in rows] == [at_cap]


def test_a_body_one_character_over_the_cap_is_refused_before_any_row_is_written(
    notification_db: tuple[async_sessionmaker[AsyncSession], SeededTenant],
) -> None:
    """Catches a pre-check that guards the title only: the body is the field
    that carries the composed text, so it is the one an over-long value
    actually reaches."""
    factory, _ = notification_db
    tenant = _own_tenant(factory)

    error, pending, committed = _notify_refused(
        factory, tenant, body="b" * (NOTIFICATION_BODY_MAX_LENGTH + 1)
    )

    assert error.code is ErrorCode.VALIDATION
    assert pending == 0
    assert committed == 0


# --- the unhappy paths --------------------------------------------------------


def test_a_recipient_in_another_tenant_is_refused_not_found_and_writes_nothing(
    notification_db: tuple[async_sessionmaker[AsyncSession], SeededTenant],
) -> None:
    """Criterion 4. The session is bound to this case's own tenant and the
    recipient is a REAL user of the foreign tenant, so the id resolves
    somewhere and only the tenant scope can refuse it.

    Catches a `notify` that trusts the id it was handed and writes the row
    anyway: the notification would then be stamped with the bound tenant
    while pointing at a person in another one — a dangling recipient no route
    can render, and a cross-tenant existence oracle. The foreign tenant is
    re-read afterwards so a row written under the RECIPIENT's tenant instead
    fails too, and the pending count catches a check that runs after the add.
    """
    factory, foreign = notification_db
    tenant = _own_tenant(factory)

    error, pending, committed = _notify_refused(factory, tenant, user_id=foreign.user_id)

    assert isinstance(error, NotFoundError)
    assert error.code is ErrorCode.NOT_FOUND
    assert pending == 0
    assert committed == 0
    assert _rows_of(factory, foreign.tenant_id) == []


def test_a_kind_outside_the_enum_is_refused_before_any_row_is_written(
    notification_db: tuple[async_sessionmaker[AsyncSession], SeededTenant],
) -> None:
    """Decision F's other half: catches a `kind` written straight through to
    the column, which stores a value row 23's route cannot render and which
    no later query for `grant_expired` will ever find. The string is a
    plausible sibling of the real member, so a check comparing against a
    prefix or a substring fails here too."""
    factory, _ = notification_db
    tenant = _own_tenant(factory)

    error, pending, committed = _notify_refused(
        factory, tenant, kind=cast("NotificationKind", "grant_expired_soon")
    )

    assert error.code is ErrorCode.VALIDATION
    assert pending == 0
    assert committed == 0


# =============================================================================
# An earlier change: `list_for_user` and `mark_read`, the two reads of the same table.
# =============================================================================
#
# Fixed instants, never `now()`: an order or a stamp that depends on when the
# suite runs is a flaky test rather than a fixture. OLDER is strictly before
# NEWER, and the two TIE rows share NEWER exactly, which is what makes the
# id tie-break observable at all.
OLDER = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
NEWER = datetime(2026, 4, 1, 9, 0, tzinfo=UTC)
# Two different clocks for the two `mark_read` calls of the idempotence case:
# the second is strictly later, so a re-stamp is visible as a moved timestamp
# rather than as an equal one.
FIRST_CLOCK = datetime(2026, 5, 1, 10, 0, tzinfo=UTC)
SECOND_CLOCK = datetime(2026, 6, 2, 11, 30, tzinfo=UTC)
# A `read_at` planted directly on a row the case needs to be already read.
ALREADY_READ_AT = datetime(2026, 2, 2, 8, 0, tzinfo=UTC)

COLLEAGUE_PASSWORD_HASH = "argon2id$obviously-fake-test-hash"


def _plant_colleague(factory: async_sessionmaker[AsyncSession], tenant: SeededTenant) -> uuid.UUID:
    """A SECOND real user in an already-seeded tenant, for the same-tenant cases.

    `seed_tenant` makes exactly one user, and "one person's bell is not
    another's" cannot be asserted with one person. Planted through the ORM on
    the case's own tenant, so it is part of that tenant's namespace and no
    other case sees it (a project rule: no new fixture).
    """

    async def scenario() -> uuid.UUID:
        async with tenant_session(factory, tenant.tenant_id) as session:
            colleague = User(
                tenant_id=tenant.tenant_id,
                department_id=tenant.department_id,
                email=f"fake.colleague.{uuid.uuid4().hex[:12]}@fake.invalid",
                display_name="Fake Colleague",
                password_hash=COLLEAGUE_PASSWORD_HASH,
                principals=["employee"],
                status="active",
                start_date=date(2026, 1, 5),
                end_date=None,
            )
            session.add(colleague)
            await session.flush()
            return colleague.id

    return asyncio.run(scenario())


def _plant_notification(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    created_at: datetime = NEWER,
    read_at: datetime | None = None,
    title: str = TITLE,
) -> uuid.UUID:
    """One committed `notifications` row with the case's own clock and read state.

    Written through the tenant-scoped repository rather than through `notify`
    on purpose: `notify` leaves `created_at` to the server clock and `read_at`
    null, and these cases need a controlled ORDER and rows that are already
    read.
    """

    async def scenario() -> uuid.UUID:
        async with tenant_session(factory, tenant_id) as session:
            row = await Repository(session, Notification).create(
                user_id=user_id,
                kind=NotificationKind.GRANT_EXPIRED.value,
                title=title,
                body=BODY,
                subject_type=SUBJECT_TYPE,
                subject_id=None,
                read_at=read_at,
                created_at=created_at,
            )
            return row.id

    return asyncio.run(scenario())


def _page(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    unread_only: bool = False,
    limit: int = 50,
) -> tuple[list[uuid.UUID], int]:
    """One `list_for_user`, as (the row ids in answer order, the unread count)."""

    async def scenario() -> tuple[list[uuid.UUID], int]:
        async with tenant_session(factory, tenant_id) as session:
            page = await list_for_user(
                session, user_id=user_id, unread_only=unread_only, limit=limit
            )
            return [row.id for row in page.notifications], page.unread_count

    return asyncio.run(scenario())


def _mark(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    notification_id: uuid.UUID,
    now: datetime,
) -> Any:
    """One committed `mark_read`, as the `read_at` it answered with."""

    async def scenario() -> Any:
        async with tenant_session(factory, tenant_id) as session:
            row = await mark_read(
                session, user_id=user_id, notification_id=notification_id, now=now
            )
            return row.read_at

    return asyncio.run(scenario())


def _mark_refused(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    notification_id: uuid.UUID,
) -> ToolError:
    """A refused `mark_read`, as the error it raised."""
    captured: list[ToolError] = []

    async def scenario() -> None:
        async with tenant_session(factory, tenant_id) as session:
            try:
                await mark_read(
                    session,
                    user_id=user_id,
                    notification_id=notification_id,
                    now=FIRST_CLOCK,
                )
            except ToolError as exc:
                captured.append(exc)

    asyncio.run(scenario())
    assert captured, "mark_read was expected to refuse and did not raise a ToolError"
    return captured[0]


def _read_at_of(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, notification_id: uuid.UUID
) -> Any:
    """The stored `read_at` of one row, re-read in its own transaction."""

    async def scenario() -> Any:
        async with tenant_session(factory, tenant_id) as session:
            row = await Repository(session, Notification).get(notification_id)
            assert row is not None, f"no notification {notification_id} in this tenant"
            return row.read_at

    return asyncio.run(scenario())


# --- list_for_user: whose rows, in which order -------------------------------


def test_list_for_user_answers_one_users_rows_and_not_a_colleagues(
    notification_db: tuple[async_sessionmaker[AsyncSession], SeededTenant],
) -> None:
    """Criterion 2, with the tenant guard taken out of the picture: both
    people are in the SAME tenant, so the session's tenant
    filter cannot save an implementation that forgot the user filter.

    Catches a read filtered by tenant alone — the shape the session guard
    already gives for free — which would show every colleague's messages in
    one person's bell. The COUNT is asserted too, not only the row set,
    because the unread total is a separate aggregate and a `func.count` over a
    hand-written query is exactly where the user filter goes missing (plan
    §5).
    """
    factory, _ = notification_db
    tenant = _own_tenant(factory)
    colleague_id = _plant_colleague(factory, tenant)
    mine = _plant_notification(factory, tenant.tenant_id, tenant.user_id)
    theirs = _plant_notification(factory, tenant.tenant_id, colleague_id)
    assert mine != theirs

    ids, unread_count = _page(factory, tenant.tenant_id, tenant.user_id)

    assert ids == [mine]
    assert unread_count == 1


def test_list_for_user_answers_nothing_at_all_for_another_tenants_user(
    notification_db: tuple[async_sessionmaker[AsyncSession], SeededTenant],
) -> None:
    """Criterion 2's other half. The named user is a REAL user of the foreign
    tenant with a REAL unread notification there, so the id resolves
    somewhere and only the tenant scope can hide the row.

    Catches a read (or an aggregate) that filters by `user_id` alone: the row
    set would carry another tenant's message and the bell would count it. The
    session is bound to this case's own tenant, which is the only scope the
    answer may come from.
    """
    factory, foreign = notification_db
    tenant = _own_tenant(factory)
    foreign_row = _plant_notification(factory, foreign.tenant_id, foreign.user_id)
    assert _read_at_of(factory, foreign.tenant_id, foreign_row) is None

    ids, unread_count = _page(factory, tenant.tenant_id, foreign.user_id)

    assert ids == []
    assert unread_count == 0


def test_the_page_is_newest_first_with_the_notification_id_breaking_a_tie(
    notification_db: tuple[async_sessionmaker[AsyncSession], SeededTenant],
) -> None:
    """Criterion 3, asserted as an exact id SEQUENCE over two rows sharing a
    `created_at` to the microsecond.

    Catches two things a set comparison cannot see: an order that is
    ascending (the oldest message at the top of the bell, so a person reading
    the first page never sees what just happened), and an order with no
    tie-break at all — two rows written in one transaction by the same sweep
    share an instant, and without the id the two orders Postgres may answer
    with are both "correct", which makes paging non-deterministic. The
    direction is DESCENDING on both columns.
    """
    factory, _ = notification_db
    tenant = _own_tenant(factory)
    old = _plant_notification(factory, tenant.tenant_id, tenant.user_id, created_at=OLDER)
    tie_one = _plant_notification(factory, tenant.tenant_id, tenant.user_id, created_at=NEWER)
    tie_two = _plant_notification(factory, tenant.tenant_id, tenant.user_id, created_at=NEWER)
    newest_first_on_the_tie = sorted([tie_one, tie_two], reverse=True)

    ids, unread_count = _page(factory, tenant.tenant_id, tenant.user_id)

    assert ids == [*newest_first_on_the_tie, old]
    assert unread_count == 3


# --- list_for_user: the filter and the count ---------------------------------


def test_unread_only_narrows_the_rows_and_leaves_the_unread_count_alone(
    notification_db: tuple[async_sessionmaker[AsyncSession], SeededTenant],
) -> None:
    """Criterion 7 over a MIXED set: two unread rows and one already read.

    `unread_only=False` must answer all three and `True` only the two, with
    the SAME `unread_count` in both answers. Catches a filter applied to the
    list and not to the aggregate (or the reverse): the page and the bell
    would then contradict each other, and a bell reading three unread beside
    a page showing one read row is the bug nobody can explain. The
    `False` arm also catches an `unread_count` derived from the page — three
    rows there, two unread — which is the whole reason the count is its own
    aggregate.
    """
    factory, _ = notification_db
    tenant = _own_tenant(factory)
    unread_one = _plant_notification(factory, tenant.tenant_id, tenant.user_id, created_at=NEWER)
    unread_two = _plant_notification(factory, tenant.tenant_id, tenant.user_id, created_at=NEWER)
    read_row = _plant_notification(
        factory, tenant.tenant_id, tenant.user_id, created_at=OLDER, read_at=ALREADY_READ_AT
    )

    every_id, every_count = _page(factory, tenant.tenant_id, tenant.user_id, unread_only=False)
    unread_ids, unread_count = _page(factory, tenant.tenant_id, tenant.user_id, unread_only=True)

    assert set(every_id) == {unread_one, unread_two, read_row}
    assert set(unread_ids) == {unread_one, unread_two}
    assert every_count == 2
    assert unread_count == 2
    assert every_count == unread_count


def test_the_unread_count_counts_beyond_the_page(
    notification_db: tuple[async_sessionmaker[AsyncSession], SeededTenant],
) -> None:
    """Criterion 4: `limit=1` over three unread rows answers ONE row and
    `unread_count == 3`.

    Catches an `unread_count` computed as the length of the returned page (or
    an aggregate that inherits the list's `LIMIT`), which is a bell that
    cannot count past the page size — quiet at fifty-one, and wrong by two
    here where the arithmetic is small enough to read.
    """
    factory, _ = notification_db
    tenant = _own_tenant(factory)
    for _ in range(3):
        _plant_notification(factory, tenant.tenant_id, tenant.user_id)

    ids, unread_count = _page(factory, tenant.tenant_id, tenant.user_id, limit=1)

    assert len(ids) == 1
    assert unread_count == 3


def test_a_user_with_no_notifications_answers_no_rows_and_a_zero_count(
    notification_db: tuple[async_sessionmaker[AsyncSession], SeededTenant],
) -> None:
    """Criterion 10 at the library seam: an empty bell is a fact about the
    person, not a refusal.

    Catches a `list_for_user` that raises `NotFoundError` (or `None`) when a
    person has nothing — the tool takes no id, so there is nothing to be
    missing, and a refusal here would make every new joiner's notifications
    page an error screen. The colleague's row proves the read is not simply
    answering empty for everybody.
    """
    factory, _ = notification_db
    tenant = _own_tenant(factory)
    colleague_id = _plant_colleague(factory, tenant)
    theirs = _plant_notification(factory, tenant.tenant_id, colleague_id)
    assert _read_at_of(factory, tenant.tenant_id, theirs) is None

    ids, unread_count = _page(factory, tenant.tenant_id, tenant.user_id)

    assert ids == []
    assert unread_count == 0


# --- mark_read ----------------------------------------------------------------


def test_mark_read_stamps_the_clock_it_was_handed_and_a_second_clock_moves_nothing(
    notification_db: tuple[async_sessionmaker[AsyncSession], SeededTenant],
) -> None:
    """Criteria 8 and 9 in one arrangement: the first call is handed
    `FIRST_CLOCK` and the second a strictly LATER one.

    The first assertion catches a `mark_read` that stamps something other
    than the clock it was given — one that called `datetime.now` itself would
    answer a value this case cannot predict, and the stamp would then be
    untestable. The second catches the re-stamp: with an internal clock, or
    with no already-read branch, the second call moves `read_at` forward and
    "when did this person first see this message" drifts on every click of a
    double-clicked bell. The row is re-read from the store afterwards, so a
    stamp that answered the old value while writing the new one fails too.
    """
    factory, _ = notification_db
    tenant = _own_tenant(factory)
    row_id = _plant_notification(factory, tenant.tenant_id, tenant.user_id)
    assert _read_at_of(factory, tenant.tenant_id, row_id) is None
    assert SECOND_CLOCK > FIRST_CLOCK

    first = _mark(factory, tenant.tenant_id, tenant.user_id, row_id, FIRST_CLOCK)
    second = _mark(factory, tenant.tenant_id, tenant.user_id, row_id, SECOND_CLOCK)

    assert first == FIRST_CLOCK
    assert second == FIRST_CLOCK
    assert _read_at_of(factory, tenant.tenant_id, row_id) == FIRST_CLOCK


def test_mark_read_refuses_a_colleagues_notification_and_stamps_nothing(
    notification_db: tuple[async_sessionmaker[AsyncSession], SeededTenant],
) -> None:
    """Criterion 11 (decision G), with the tenant guard taken out of the
    picture: the row belongs to a colleague in the SAME tenant (standing rule
    9), so nothing but the user filter can refuse it.

    Catches a `mark_read` that reads the row by id under the tenant filter
    alone: any person could then mark any colleague's notification read, and
    the id would be a capability. Asserted as `NotFoundError` and
    `ErrorCode.NOT_FOUND` rather than `FORBIDDEN`, and the row is re-read to
    prove the refusal came BEFORE the write rather than after it.
    """
    factory, _ = notification_db
    tenant = _own_tenant(factory)
    colleague_id = _plant_colleague(factory, tenant)
    theirs = _plant_notification(factory, tenant.tenant_id, colleague_id)

    error = _mark_refused(factory, tenant.tenant_id, tenant.user_id, theirs)

    assert isinstance(error, NotFoundError)
    assert error.code is ErrorCode.NOT_FOUND
    assert _read_at_of(factory, tenant.tenant_id, theirs) is None


def test_mark_read_refuses_an_unknown_notification_id_with_not_found(
    notification_db: tuple[async_sessionmaker[AsyncSession], SeededTenant],
) -> None:
    """Criterion 11's other half: an id that resolves nowhere reads exactly
    like a colleague's, so the argument is not an existence oracle.

    Catches a `mark_read` that answers a missing row with something other
    than the typed `NOT_FOUND` — an `AttributeError` on `None`, or an
    `INTERNAL` — either of which would turn a stale bell in an open browser
    tab into an error the caller cannot act on.
    """
    factory, _ = notification_db
    tenant = _own_tenant(factory)
    unknown = uuid.uuid4()

    error = _mark_refused(factory, tenant.tenant_id, tenant.user_id, unknown)

    assert isinstance(error, NotFoundError)
    assert error.code is ErrorCode.NOT_FOUND
