"""The random id a user is known by at the model gateway.

What is under test (does NOT exist yet; every case is red until it lands):

``helpdesk.shared.crud.users``
    ``async gateway_user_id_for(session, user_id) -> str``: the user's stored
    ``User.gateway_user_id``, made once on first use (``u_`` + uuid4 hex) by a
    single ``UPDATE ... WHERE gateway_user_id IS NULL`` and read back.
``helpdesk.shared.models.directory.User.gateway_user_id``
    Nullable, ``VARCHAR(34)``, unique, with a database check that it matches
    ``^u_[0-9a-f]{32}$``.

Cost: the `db` marker. The update-once race, the unique index and the check are
what only Postgres shows. Engine and factory come from the shared session
fixtures in ``conftest.py``; each test seeds its own tenant.
"""

import asyncio
import re
import uuid
from datetime import date

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from helpdesk.shared.core.db import tenant_session
from helpdesk.shared.crud.users import gateway_user_id_for
from helpdesk.shared.models import User
from tests.support.seed import seed_tenant

pytestmark = pytest.mark.db

WELL_FORMED = re.compile(r"u_[0-9a-f]{32}")


async def _stored(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, user_id: uuid.UUID
) -> str | None:
    """The column as a brand-new session reads it."""
    async with tenant_session(factory, tenant_id) as db:
        user = await db.get(User, user_id)
        assert user is not None
        return user.gateway_user_id


def test_a_user_gets_a_well_formed_id_on_first_use(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Catches a helper that returns a value without storing it, builds it from
    the internal user id (`u_` + `user.id.hex`), or makes one in the wrong shape.

    The column starts empty, so a stored well-formed value can only come from
    the helper; the internal-id form is the one a lazy implementation picks.
    """

    async def scenario() -> None:
        seeded = await seed_tenant(session_factory, "gw-user-first")
        assert await _stored(session_factory, seeded.tenant_id, seeded.user_id) is None
        async with tenant_session(session_factory, seeded.tenant_id) as db:
            value = await gateway_user_id_for(db, seeded.user_id)
        assert WELL_FORMED.fullmatch(value)
        assert value != "u_" + seeded.user_id.hex
        assert await _stored(session_factory, seeded.tenant_id, seeded.user_id) == value

    asyncio.run(scenario())


def test_the_same_user_always_gets_the_same_id(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Catches a helper that makes a fresh random id on every call instead of
    keeping the first, so the gateway would see one person as many users.

    Three calls, the last across a new session, must all return one value.
    """

    async def scenario() -> None:
        seeded = await seed_tenant(session_factory, "gw-user-same")
        async with tenant_session(session_factory, seeded.tenant_id) as db:
            first = await gateway_user_id_for(db, seeded.user_id)
            second = await gateway_user_id_for(db, seeded.user_id)
        async with tenant_session(session_factory, seeded.tenant_id) as db:
            third = await gateway_user_id_for(db, seeded.user_id)
        assert first == second == third

    asyncio.run(scenario())


def test_two_users_get_different_ids(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Catches a helper that hands every user in a tenant the same id (a
    constant, or one id kept per tenant), which would merge their cost."""

    async def scenario() -> None:
        seeded = await seed_tenant(session_factory, "gw-user-two")
        async with tenant_session(session_factory, seeded.tenant_id) as db:
            other = User(
                tenant_id=seeded.tenant_id,
                department_id=None,
                email="second.person@gw-user-two.example.test",
                display_name="Fake Second Person",
                password_hash="argon2id$obviously-fake-test-hash",
                principals=["employee"],
                status="active",
                start_date=date(2026, 1, 5),
                end_date=None,
            )
            db.add(other)
            await db.flush()
            other_id = other.id
        async with tenant_session(session_factory, seeded.tenant_id) as db:
            one = await gateway_user_id_for(db, seeded.user_id)
            two = await gateway_user_id_for(db, other_id)
        assert one != two

    asyncio.run(scenario())


def test_two_first_uses_at_once_end_on_one_id(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Catches a read-then-write helper (select, see empty, then write) that lets
    two turns at once each store their own id, so one of them sends an id that
    is no longer the stored one.

    Two sessions ask together for one empty user; both must return the value
    that ended up stored.
    """

    async def ask(tenant_id: uuid.UUID, user_id: uuid.UUID) -> str:
        async with tenant_session(session_factory, tenant_id) as db:
            return await gateway_user_id_for(db, user_id)

    async def scenario() -> None:
        seeded = await seed_tenant(session_factory, "gw-user-race")
        a, b = await asyncio.gather(
            ask(seeded.tenant_id, seeded.user_id), ask(seeded.tenant_id, seeded.user_id)
        )
        assert a == b
        assert await _stored(session_factory, seeded.tenant_id, seeded.user_id) == a

    asyncio.run(scenario())


_GOOD = "a" * 32


@pytest.mark.parametrize(
    "bad",
    [
        "u_" + "a" * 31,
        "u_" + "A" * 32,
        "a" * 34,
    ],
    ids=["31-hex", "uppercase-hex", "no-prefix"],
)
def test_the_database_refuses_a_bad_id(
    session_factory: async_sessionmaker[AsyncSession], bad: str
) -> None:
    """Catches a schema with no check on the column, so a bad value written by
    any path other than the helper is stored and later refused only at the
    client.

    Every row is 33 or 34 characters, so it fits VARCHAR(34) and only the
    check can refuse it.
    """

    async def scenario() -> None:
        seeded = await seed_tenant(session_factory, "gw-user-check")
        with pytest.raises(IntegrityError):
            async with tenant_session(session_factory, seeded.tenant_id) as db:
                user = await db.get(User, seeded.user_id)
                assert user is not None
                user.gateway_user_id = bad
                await db.flush()

    asyncio.run(scenario())
