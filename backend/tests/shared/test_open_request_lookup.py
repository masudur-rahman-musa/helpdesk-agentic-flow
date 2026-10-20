"""The open-request lookup.

Surface under test (written before it exists):

``helpdesk.shared.crud.access_requests``
    ``async fetch_open_request(session, target_user_id, role_id)`` returns the
    access request for that target and role whose status is ``pending`` or
    ``approved``, or ``None``. ``executed``, ``rejected`` and ``failed``
    requests are closed and never returned; another role never matches; the
    tenant guard keeps another company's rows out.

Cost: the `db` marker. The status filter and the tenant scope are what only
Postgres shows. Engine and session factory come from the shared session
fixtures in ``conftest.py``; each case seeds its own tenant.
"""

import asyncio
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from helpdesk.shared.core.db import tenant_session
from helpdesk.shared.crud.access_requests import fetch_open_request
from helpdesk.shared.models import AccessRequest
from tests.support.seed import SeededTenant, seed_tenant

pytestmark = pytest.mark.db


def _request(seeded: SeededTenant, *, role_id: uuid.UUID, status: str) -> AccessRequest:
    """One request row for the seeded user, obviously fake."""
    return AccessRequest(
        id=uuid.uuid4(),
        tenant_id=seeded.tenant_id,
        number=uuid.uuid4().int % 2_000_000_000 + 1,
        requester_user_id=seeded.user_id,
        target_user_id=seeded.user_id,
        role_id=role_id,
        justification="Obviously fake justification.",
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


async def _add(
    factory: async_sessionmaker[AsyncSession],
    seeded: SeededTenant,
    *,
    role_id: uuid.UUID,
    status: str,
) -> uuid.UUID:
    row = _request(seeded, role_id=role_id, status=status)
    async with tenant_session(factory, seeded.tenant_id) as db:
        db.add(row)
    return row.id


@pytest.mark.parametrize(
    ("status", "open_"),
    [
        ("pending", True),
        ("approved", True),
        ("executed", False),
        ("rejected", False),
        ("failed", False),
    ],
)
def test_fetch_open_request_finds_pending_and_approved_only(
    session_factory: async_sessionmaker[AsyncSession], status: str, open_: bool
) -> None:
    """Catches a lookup whose status filter is wrong in either direction: one
    that drops `approved` (an approved request waiting to be granted could be
    filed twice), or one that also returns `executed`, `rejected` or `failed`
    (a closed request would block a fresh ask for good). Each status gets its
    own tenant with that single row, so no other row can answer for it."""

    async def scenario() -> None:
        seeded = await seed_tenant(session_factory, f"open-{status}")
        request_id = await _add(session_factory, seeded, role_id=seeded.role_id, status=status)
        async with tenant_session(session_factory, seeded.tenant_id) as db:
            found = await fetch_open_request(db, seeded.user_id, seeded.role_id)
        if open_:
            assert found is not None
            assert found.id == request_id
            assert found.status == status
        else:
            assert found is None

    asyncio.run(scenario())


def test_fetch_open_request_ignores_other_roles_and_tenants(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Catches a lookup that matches on the target alone (an open request for
    one role would block every other role), or one that is not scoped to the
    company: tenant B holds an open request for its own user and role, and a
    session bound to tenant A must not see it even when asked for those ids."""

    async def scenario() -> None:
        mine = await seed_tenant(session_factory, "open-mine")
        theirs = await seed_tenant(session_factory, "open-theirs")
        await _add(session_factory, mine, role_id=mine.role_b_id, status="pending")
        await _add(session_factory, theirs, role_id=theirs.role_id, status="pending")
        async with tenant_session(session_factory, mine.tenant_id) as db:
            other_role = await fetch_open_request(db, mine.user_id, mine.role_id)
            foreign = await fetch_open_request(db, theirs.user_id, theirs.role_id)
            same_role = await fetch_open_request(db, mine.user_id, mine.role_b_id)
        assert other_role is None
        assert foreign is None
        assert same_role is not None, "premise: the row is visible to its own tenant"

    asyncio.run(scenario())
