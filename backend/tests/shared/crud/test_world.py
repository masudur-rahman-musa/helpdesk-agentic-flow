"""The world of one tenant, read for the eval route.

What is under test (does NOT exist yet; every case is red until it lands):

``helpdesk.shared.crud.world``
    ``async read_world_rows(session) -> WorldRows`` reads the tenant the
    session is bound to, and only that tenant. ``WorldRows`` holds five lists
    of frozen dataclasses:
    ``requests`` (``RequestRow``: number, requester_id, target_id, role_name
    written ``<system>.<role>``, status), ``grants`` (``GrantRow``: user_id,
    role_name, status, expires_at), ``tickets`` (``TicketRow``: number, user_id,
    kind, status), ``people`` (``PersonRow``: user_id, email, department,
    status, start_date, reset_asked, unlocked) and ``reviews`` (``ReviewRow``:
    status, items_decided).

Cost: the `db` marker. The joins from a request or grant to its role and
system name, the department name of a person and the count of decided review
items are what only Postgres shows. The engine and factory come from the shared
session fixtures in ``conftest.py``; each tenant is a new uuid-suffixed tenant,
so the tests share nothing.
"""

import asyncio
import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from helpdesk.shared.core.db import tenant_session
from helpdesk.shared.crud.world import (
    GrantRow,
    PersonRow,
    RequestRow,
    ReviewRow,
    TicketRow,
    WorldRows,
    read_world_rows,
)
from helpdesk.shared.models import AccessRequest, AccessReview, ReviewItem, User
from tests.support.seed import SeededTenant, seed_tenant

pytestmark = pytest.mark.db

ASKED_AT = datetime(2026, 6, 2, 10, 0, tzinfo=UTC)


async def _add_work(
    factory: async_sessionmaker[AsyncSession],
    seeded: SeededTenant,
    *,
    number: int,
    status: str,
    decided: int,
    undecided: int,
    reset_asked: bool,
    unlocked: bool,
) -> None:
    """One request, one open review with `decided` + `undecided` items, and
    optionally a password reset asked and an account unlocked, for the seeded person."""
    async with tenant_session(factory, seeded.tenant_id) as db:
        db.add(
            AccessRequest(
                tenant_id=seeded.tenant_id,
                number=number,
                requester_user_id=seeded.user_id,
                target_user_id=seeded.user_id,
                role_id=seeded.role_b_id,
                justification="fake justification",
                requested_expiry_days=None,
                risk_class="low",
                decision="approval",
                decision_reasons=[],
                citations=[],
                status=status,
                approver_user_ids=[],
                approvals=[],
                session_id=None,
                trace_id=None,
                idempotency_key=None,
            )
        )
        review = AccessReview(
            tenant_id=seeded.tenant_id,
            reviewer_user_id=seeded.user_id,
            department_id=seeded.department_id,
            system_id=None,
            period="2026-Q4",
            status="open",
            due_at=None,
            completed_at=None,
        )
        db.add(review)
        await db.flush()
        for n in range(decided + undecided):
            is_decided = n < decided
            db.add(
                ReviewItem(
                    tenant_id=seeded.tenant_id,
                    review_id=review.id,
                    grant_id=seeded.grant_id,
                    flag="ok",
                    decision="keep" if is_decided else None,
                    decided_at=ASKED_AT if is_decided else None,
                )
            )
        if reset_asked:
            await db.execute(
                update(User)
                .where(User.id == seeded.user_id)
                .values(password_reset_requested_at=ASKED_AT)
            )
        if unlocked:
            await db.execute(
                update(User).where(User.id == seeded.user_id).values(account_unlocked_at=ASKED_AT)
            )


async def _world(factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID) -> WorldRows:
    async with tenant_session(factory, tenant_id) as db:
        return await read_world_rows(db)


def test_the_world_reads_only_the_case_tenant(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Catches a world read that is not limited to the bound tenant (the other
    tenant's request, grant, ticket, person or review shows up), one that
    leaves out a list, joins a role or department name wrongly, miscounts the
    decided review items, or reads a reset as asked for someone who has not.

    Two tenants are seeded with different numbers, statuses and counts, so a
    read that mixes them, or returns the same rows for both, fails in one
    direction or the other. The first tenant has a reset asked and the second
    an unlock, so one flag read as the other fails, and the first review has 1
    decided item of 3 while the second has 2 of 2, so a count of all items or a
    flag set on everyone fails.
    """

    async def scenario() -> None:
        first = await seed_tenant(session_factory, "world-a")
        second = await seed_tenant(session_factory, "world-b")
        await _add_work(
            session_factory,
            first,
            number=1,
            status="pending",
            decided=1,
            undecided=2,
            reset_asked=True,
            unlocked=False,
        )
        await _add_work(
            session_factory,
            second,
            number=7,
            status="executed",
            decided=2,
            undecided=0,
            reset_asked=False,
            unlocked=True,
        )

        mine = await _world(session_factory, first.tenant_id)
        theirs = await _world(session_factory, second.tenant_id)

        assert mine.requests == [
            RequestRow(
                number=1,
                requester_id=first.user_id,
                target_id=first.user_id,
                role_name="sys-world-a.role-b-world-a",
                status="pending",
            )
        ]
        assert mine.grants == [
            GrantRow(
                user_id=first.user_id,
                role_name="sys-world-a.role-a-world-a",
                status="active",
                expires_at=None,
            )
        ]
        assert mine.tickets == [
            TicketRow(number=1, user_id=first.user_id, kind="general", status="open")
        ]
        assert mine.people == [
            PersonRow(
                user_id=first.user_id,
                email="fake.person@world-a.example.test",
                department="dept-world-a",
                status="active",
                start_date=date(2026, 1, 5),
                reset_asked=True,
                unlocked=False,
            )
        ]
        assert mine.reviews == [ReviewRow(status="open", items_decided=1)]

        assert [r.number for r in theirs.requests] == [7]
        assert [r.status for r in theirs.requests] == ["executed"]
        assert [g.role_name for g in theirs.grants] == ["sys-world-b.role-a-world-b"]
        assert [p.user_id for p in theirs.people] == [second.user_id]
        assert [(p.reset_asked, p.unlocked) for p in theirs.people] == [(False, True)]
        assert [t.user_id for t in theirs.tickets] == [second.user_id]
        assert theirs.reviews == [ReviewRow(status="open", items_decided=2)]

    asyncio.run(scenario())


def test_an_empty_tenant_reads_five_empty_lists(
    session_factory: async_sessionmaker[AsyncSession], fresh_tenant: uuid.UUID
) -> None:
    """Catches a read that fails or invents a row for a tenant with nothing in
    it, such as a person read through a join that drops to one row, or a list
    left as `None`.

    The tenant is brand new, so every list must come back empty, and the five
    lists are all there.
    """
    world = asyncio.run(_world(session_factory, fresh_tenant))

    assert world.requests == []
    assert world.grants == []
    assert world.tickets == []
    assert world.people == []
    assert world.reviews == []
