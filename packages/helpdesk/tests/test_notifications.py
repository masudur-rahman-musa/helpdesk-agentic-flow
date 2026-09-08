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

a project rule: ONE session-scoped database and one session-scoped foreign
tenant, built once. Per-test isolation is a namespace, not a rebuild — every
case seeds its OWN tenant with `_seed.seed_tenant` and asserts over that
tenant's rows only, so no case can see another's notifications.

No pytest-asyncio: each case is a sync function driving one `asyncio.run`.
"""

import asyncio
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, cast

import pytest
from _seed import SeededTenant, seed_tenant
from conftest import TempDatabaseFactory
from helpdesk.core.db import create_engine, create_session_factory, tenant_session
from helpdesk.core.repository import Repository
from helpdesk.errors import ErrorCode, NotFoundError, ToolError
from helpdesk.models import Base, Notification
from helpdesk.models.notification import (
    NOTIFICATION_BODY_MAX_LENGTH,
    NOTIFICATION_TITLE_MAX_LENGTH,
    NotificationKind,
)
from helpdesk.notifications.notify import notify

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
def notification_db(pg: TempDatabaseFactory) -> Iterator[tuple[str, SeededTenant]]:
    """One database with the schema created once, plus one FOREIGN tenant.

    The foreign tenant is fully seeded so the cross-tenant case names a user
    that really exists somewhere: a `notify` that answered `NOT_FOUND` for
    every id would otherwise pass it for the wrong reason.
    """
    url = pg.create()

    async def prepare() -> SeededTenant:
        engine = create_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            return await seed_tenant(create_session_factory(engine), "notify-foreign-tenant")
        finally:
            await engine.dispose()

    foreign = asyncio.run(prepare())
    yield url, foreign
    pg.drop(url)


def _own_tenant(url: str) -> SeededTenant:
    """A tenant of this case's own: the per-test namespace."""

    async def prepare() -> SeededTenant:
        engine = create_engine(url)
        try:
            return await seed_tenant(
                create_session_factory(engine), f"notify-{uuid.uuid4().hex[:12]}"
            )
        finally:
            await engine.dispose()

    return asyncio.run(prepare())


def _notify_ok(
    url: str,
    tenant: SeededTenant,
    *,
    user_id: uuid.UUID | None = None,
    title: str = TITLE,
    body: str = BODY,
    subject_id: str | None = None,
) -> list[NotificationFacts]:
    """One committed `notify`, then everything this tenant now holds."""

    async def scenario() -> list[NotificationFacts]:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
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
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _notify_refused(
    url: str,
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
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
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
        finally:
            await engine.dispose()

    pending, committed = asyncio.run(scenario())
    assert captured, "notify was expected to refuse and did not raise a ToolError"
    return captured[0], pending, committed


def _rows_of(url: str, tenant_id: uuid.UUID) -> list[NotificationFacts]:
    async def scenario() -> list[NotificationFacts]:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, tenant_id) as session:
                rows = await Repository(session, Notification).list()
                return [_facts(row) for row in rows]
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


# --- the happy path -----------------------------------------------------------


def test_notify_stores_one_unread_row_under_the_sessions_bound_tenant(
    notification_db: tuple[str, SeededTenant],
) -> None:
    """a project rule and criterion 2 together. `notify` takes no tenant
    argument, so the only place `tenant_id` can come from is the session:
    catches a row stamped from the RECIPIENT's own user row (which would
    silently follow a mis-resolved id into another tenant) or left to a
    column default. Also catches a row stored already read, which would make
    every expiry notification invisible to row 23's unread list the moment it
    was written.
    """
    url, _ = notification_db
    tenant = _own_tenant(url)

    rows = _notify_ok(url, tenant)

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
    notification_db: tuple[str, SeededTenant],
) -> None:
    """The inclusive half of the boundary: catches an off-by-one `>=` that
    refuses the longest title the store is declared to hold, so a legitimate
    expiry notice is never written and the holder is never told."""
    url, _ = notification_db
    tenant = _own_tenant(url)
    at_cap = "t" * NOTIFICATION_TITLE_MAX_LENGTH

    rows = _notify_ok(url, tenant, title=at_cap)

    assert [row.title for row in rows] == [at_cap]


def test_a_title_one_character_over_the_cap_is_refused_before_any_row_is_written(
    notification_db: tuple[str, SeededTenant],
) -> None:
    """The exclusive half: catches a missing pre-check, which leaves the
    length to the column and turns an over-long title into a database error
    raised in the middle of the expiry sweep — after the grant has already
    been marked expired and its audit event appended."""
    url, _ = notification_db
    tenant = _own_tenant(url)

    error, pending, committed = _notify_refused(
        url, tenant, title="t" * (NOTIFICATION_TITLE_MAX_LENGTH + 1)
    )

    assert error.code is ErrorCode.VALIDATION
    assert pending == 0
    assert committed == 0


def test_a_body_of_exactly_the_cap_is_accepted(
    notification_db: tuple[str, SeededTenant],
) -> None:
    """The inclusive half for the second cap: catches one check written
    against the title's constant for both fields, which would refuse every
    body over 200 characters while the plan allows 2000."""
    url, _ = notification_db
    tenant = _own_tenant(url)
    at_cap = "b" * NOTIFICATION_BODY_MAX_LENGTH

    rows = _notify_ok(url, tenant, body=at_cap)

    assert [row.body for row in rows] == [at_cap]


def test_a_body_one_character_over_the_cap_is_refused_before_any_row_is_written(
    notification_db: tuple[str, SeededTenant],
) -> None:
    """Catches a pre-check that guards the title only: the body is the field
    that carries the composed text, so it is the one an over-long value
    actually reaches."""
    url, _ = notification_db
    tenant = _own_tenant(url)

    error, pending, committed = _notify_refused(
        url, tenant, body="b" * (NOTIFICATION_BODY_MAX_LENGTH + 1)
    )

    assert error.code is ErrorCode.VALIDATION
    assert pending == 0
    assert committed == 0


# --- the unhappy paths --------------------------------------------------------


def test_a_recipient_in_another_tenant_is_refused_not_found_and_writes_nothing(
    notification_db: tuple[str, SeededTenant],
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
    url, foreign = notification_db
    tenant = _own_tenant(url)

    error, pending, committed = _notify_refused(url, tenant, user_id=foreign.user_id)

    assert isinstance(error, NotFoundError)
    assert error.code is ErrorCode.NOT_FOUND
    assert pending == 0
    assert committed == 0
    assert _rows_of(url, foreign.tenant_id) == []


def test_a_kind_outside_the_enum_is_refused_before_any_row_is_written(
    notification_db: tuple[str, SeededTenant],
) -> None:
    """Decision F's other half: catches a `kind` written straight through to
    the column, which stores a value row 23's route cannot render and which
    no later query for `grant_expired` will ever find. The string is a
    plausible sibling of the real member, so a check comparing against a
    prefix or a substring fails here too."""
    url, _ = notification_db
    tenant = _own_tenant(url)

    error, pending, committed = _notify_refused(
        url, tenant, kind=cast("NotificationKind", "grant_expired_soon")
    )

    assert error.code is ErrorCode.VALIDATION
    assert pending == 0
    assert committed == 0
