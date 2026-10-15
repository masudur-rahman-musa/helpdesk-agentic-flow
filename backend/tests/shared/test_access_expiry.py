"""An earlier change §6: `expire_grants`, called directly with a fixed `now`.

Written before the implementation exists. Expected surface (the implementer
must provide exactly this):

``helpdesk.access.expiry``
    ``GRANT_EXPIRED_EVENT_TYPE = "grant.expired"`` (the design).
    ``async def expire_grants(session, *, now) -> ...`` — over the BOUND
    tenant only, it locks the lapsed set (`status = 'active'`,
    `revoked_at IS NULL`, `expires_at IS NOT NULL AND expires_at <= now`,
    `SELECT … FOR UPDATE`), writes `status = 'expired'` leaving `revoked_at`
    and `revoke_reason` null, appends ONE `grant.expired` audit event per row
    (`actor_kind` `system`, `subject_type` `grant`, `subject_id` the grant id,
    payload keys exactly `user_id`, `role_id`, `expires_at`), notifies the
    holder and the holder's department manager, and increments
    `helpdesk_expiries_total` by the number of rows expired. All in the
    caller's transaction; `now` is the caller's instant, never `now()` read
    inside.

``helpdesk.obs.metrics``
    a counter exposed as ``helpdesk_expiries_total``, registered by
    ``build_metrics`` on the registry it is handed (the plan-20b factory
    shape) and read at CALL time through the module-level pair, so a test can
    install its own registry and assert an ABSOLUTE value.

The return value is deliberately never asserted: plan §6 fixes no return
shape, and every claim below is readable from the database, the audit chain,
the notifications or the registry.

Decision D is the boundary this file exists for: `expires_at <= now`, the
exact complement of `core.repository.is_live_grant`'s `expires_at > now`, so
a grant is never both not-live and not-expired.

a project rule: the engine and session factory are the shared ones from
`conftest.py` -- one engine for the whole run, schema created once. Per-test
isolation is a namespace, not a rebuild -- `expire_grants` sweeps a whole
tenant, so every case seeds a tenant of its OWN with `_seed.seed_tenant` and
no case can see another's rows.

No pytest-asyncio: each case is a sync function driving one `asyncio.run`
(the race case drives two, one per thread).
"""

import asyncio
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from prometheus_client import CollectorRegistry
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from helpdesk.shared.access.expiry import GRANT_EXPIRED_EVENT_TYPE, expire_grants
from helpdesk.shared.core.db import tenant_session
from helpdesk.shared.core.observability import metrics
from helpdesk.shared.crud import Repository
from helpdesk.shared.crud.grants import ACTIVE_GRANT_STATUS, REVOKED_GRANT_STATUS
from helpdesk.shared.models import Department, Grant, Notification, User
from tests.support.seed import SeededTenant, seed_tenant

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
"""The one instant every case expires at. Passed in, never read from a clock."""

GRANTED_AT = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
LAPSED_AT = NOW - timedelta(days=1)
"""Comfortably past `NOW`: the ordinary lapsed grant."""
JUST_AHEAD = NOW + timedelta(microseconds=1)
"""The narrowest value that must survive decision D's `<=`."""

EXPIRED_GRANT_STATUS = "expired"
"""What `grants.status` reads once the sweep has been through (plan §1).

A literal rather than an import: expiry is not revocation, and the only thing
that makes this string right is that §1 fixes it."""

EXPECTED_PAYLOAD_KEYS = {"user_id", "role_id", "expires_at"}
EXPIRIES_TOTAL = "helpdesk_expiries_total"


@dataclass(frozen=True)
class GrantFacts:
    """What one `grants` row says, copied out before its session closes."""

    grant_id: uuid.UUID
    status: str
    revoked_at: datetime | None
    revoke_reason: str | None
    expires_at: datetime | None


@dataclass(frozen=True)
class NotificationFacts:
    """What one `notifications` row says, copied out before its session closes."""

    user_id: uuid.UUID
    kind: str
    subject_type: str | None
    subject_id: str | None
    read_at: Any


@pytest.fixture
def fresh_registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[CollectorRegistry]:
    """A registry of this test's own, installed over the module-level pair.

    An earlier change decision C: the call sites read `helpdesk.obs.metrics.METRICS` at
    call time, so replacing both names here is what makes an ABSOLUTE
    assertion possible. Against the process-wide registry the same case could
    only assert a delta, which passes just as happily when the wrong site did
    the incrementing.
    """
    registry = CollectorRegistry()
    monkeypatch.setattr(metrics, "REGISTRY", registry)
    monkeypatch.setattr(metrics, "METRICS", metrics.build_metrics(registry))
    yield registry


def _tenant(factory: async_sessionmaker[AsyncSession]) -> SeededTenant:
    """A tenant of this case's own: the per-test namespace.

    `seed_tenant` plants one grant with a NULL `expires_at`, which every case
    below leaves in place: a sweep that expired it would fail the untouched
    case and would show up as an extra event or an extra counter tick here.
    """

    async def prepare() -> SeededTenant:
        return await seed_tenant(factory, f"expiry-{uuid.uuid4().hex[:12]}")

    return asyncio.run(prepare())


def _plant_grant(
    factory: async_sessionmaker[AsyncSession],
    tenant: SeededTenant,
    *,
    user_id: uuid.UUID | None = None,
    role_id: uuid.UUID | None = None,
    expires_at: datetime | None = LAPSED_AT,
    status: str = ACTIVE_GRANT_STATUS,
    revoked_at: datetime | None = None,
    revoke_reason: str | None = None,
) -> uuid.UUID:
    """One committed `grants` row, written around the sweep."""

    async def scenario() -> uuid.UUID:
        async with tenant_session(factory, tenant.tenant_id) as session:
            row = await Repository(session, Grant).create(
                user_id=tenant.user_id if user_id is None else user_id,
                role_id=tenant.role_b_id if role_id is None else role_id,
                request_id=None,
                granted_by_user_id=None,
                granted_at=GRANTED_AT,
                expires_at=expires_at,
                revoked_at=revoked_at,
                revoke_reason=revoke_reason,
                status=status,
            )
            return row.id

    return asyncio.run(scenario())


def _plant_user(factory: async_sessionmaker[AsyncSession], tenant: SeededTenant) -> uuid.UUID:
    """A second, obviously fake person in the seeded department."""
    suffix = uuid.uuid4().hex[:12]

    async def scenario() -> uuid.UUID:
        async with tenant_session(factory, tenant.tenant_id) as session:
            row = await Repository(session, User).create(
                department_id=tenant.department_id,
                email=f"fake.manager.{suffix}@example.test",
                display_name=f"Fake Manager {suffix}",
                password_hash="argon2id$obviously-fake-test-hash",
                principals=["employee", "manager"],
                status="active",
                start_date=date(2026, 1, 5),
                end_date=None,
                password_reset_requested_at=None,
                account_unlocked_at=None,
            )
            return row.id

    return asyncio.run(scenario())


def _set_manager(
    factory: async_sessionmaker[AsyncSession],
    tenant: SeededTenant,
    manager_user_id: uuid.UUID | None,
) -> None:
    async def scenario() -> None:
        async with tenant_session(factory, tenant.tenant_id) as session:
            await Repository(session, Department).update(
                tenant.department_id, manager_user_id=manager_user_id
            )

    asyncio.run(scenario())


def _expire(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, *, now: datetime = NOW
) -> None:
    """One `expire_grants` in one committed transaction."""

    async def scenario() -> None:
        async with tenant_session(factory, tenant_id) as session:
            await expire_grants(session, now=now)

    asyncio.run(scenario())


def _grant(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, grant_id: uuid.UUID
) -> GrantFacts:
    async def scenario() -> GrantFacts:
        async with tenant_session(factory, tenant_id) as session:
            row = await Repository(session, Grant).get(grant_id)
            assert row is not None, f"planted grant {grant_id} is gone"
            return GrantFacts(
                grant_id=row.id,
                status=row.status,
                revoked_at=row.revoked_at,
                revoke_reason=row.revoke_reason,
                expires_at=row.expires_at,
            )

    return asyncio.run(scenario())


def _expired_events(
    shared_engine: AsyncEngine, tenant_id: uuid.UUID
) -> list[tuple[str, str, str, dict[str, Any]]]:
    """Every `grant.expired` event of one tenant, in `seq` order."""

    async def scenario() -> list[tuple[str, str, str, dict[str, Any]]]:
        async with shared_engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT actor_kind, subject_type, subject_id, payload "
                    "FROM audit_events WHERE tenant_id = :tenant "
                    "AND event_type = :event_type ORDER BY seq"
                ),
                {"tenant": tenant_id, "event_type": GRANT_EXPIRED_EVENT_TYPE},
            )
            return [(str(row[0]), str(row[1]), str(row[2]), dict(row[3])) for row in result.all()]

    return asyncio.run(scenario())


def _notifications(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> list[NotificationFacts]:
    async def scenario() -> list[NotificationFacts]:
        async with tenant_session(factory, tenant_id) as session:
            rows = await Repository(session, Notification).list()
            return [
                NotificationFacts(
                    user_id=row.user_id,
                    kind=str(row.kind),
                    subject_type=None if row.subject_type is None else str(row.subject_type),
                    subject_id=None if row.subject_id is None else str(row.subject_id),
                    read_at=row.read_at,
                )
                for row in rows
            ]

    return asyncio.run(scenario())


# --- the status write and its boundary (criteria 5 and 6, decision D) ---------


def test_a_lapsed_grant_becomes_expired_without_being_revoked(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Criterion 5. Catches a sweep that reuses the revocation path: expiry
    is not revocation, so a row stamped `revoked` — or `expired` with a
    `revoked_at` and a reason — records a human decision that nobody took,
    and the audit trail then shows a revocation with no revoker. Both fields
    are asserted, because a status written correctly beside a stamped
    `revoked_at` is exactly what going through `revoke_grant_row` produces.
    """
    tenant = _tenant(session_factory)
    grant_id = _plant_grant(session_factory, tenant, expires_at=LAPSED_AT)

    _expire(session_factory, tenant.tenant_id)

    row = _grant(session_factory, tenant.tenant_id, grant_id)
    assert row.status == EXPIRED_GRANT_STATUS
    assert row.revoked_at is None
    assert row.revoke_reason is None


def test_a_grant_expiring_at_exactly_now_is_expired(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Decision D's inclusive boundary. Catches the design's literal
    `expires_at < now()`, which leaves a grant whose expiry is exactly the
    sweep's instant neither live (`is_live_grant` compares `expires_at > now`
    and says no) nor expired — a row in a state no query in the codebase
    accounts for, that only a LATER tick happens to clean up."""
    tenant = _tenant(session_factory)
    grant_id = _plant_grant(session_factory, tenant, expires_at=NOW)

    _expire(session_factory, tenant.tenant_id)

    assert _grant(session_factory, tenant.tenant_id, grant_id).status == EXPIRED_GRANT_STATUS


def test_a_grant_one_microsecond_ahead_of_now_is_untouched(
    session_factory: async_sessionmaker[AsyncSession],
    shared_engine: AsyncEngine,
) -> None:
    """The exclusive half of the same boundary: catches a `<=` widened to a
    range or a comparison done in the wrong direction, which would expire
    access that is still legitimately held — and, because expiry writes no
    reason, would leave the holder with no record of why it went."""
    tenant = _tenant(session_factory)
    grant_id = _plant_grant(session_factory, tenant, expires_at=JUST_AHEAD)

    _expire(session_factory, tenant.tenant_id)

    row = _grant(session_factory, tenant.tenant_id, grant_id)
    assert row.status == ACTIVE_GRANT_STATUS
    assert _expired_events(shared_engine, tenant.tenant_id) == []


def test_null_expiry_revoked_and_already_expired_grants_are_untouched(
    session_factory: async_sessionmaker[AsyncSession],
    shared_engine: AsyncEngine,
) -> None:
    """Criterion 6, one planted row per excluded state.

    Each catches a dropped conjunct of the lapsed-set filter: without
    `expires_at IS NOT NULL` a permanent grant is swept away (Postgres
    compares NULL to `now` as unknown, so this only bites an implementation
    that coalesces); without `revoked_at IS NULL` a revoked row is rewritten
    `expired` and loses the revocation; without `status = 'active'` an
    already-expired row is swept again every five minutes for ever, writing a
    duplicate audit event and a duplicate notification each time.
    """
    tenant = _tenant(session_factory)
    never = _plant_grant(session_factory, tenant, expires_at=None)
    revoked = _plant_grant(
        session_factory,
        tenant,
        expires_at=LAPSED_AT,
        status=REVOKED_GRANT_STATUS,
        revoked_at=GRANTED_AT,
        revoke_reason="fake historical revocation, must not be rewritten",
    )
    already = _plant_grant(
        session_factory, tenant, expires_at=LAPSED_AT, status=EXPIRED_GRANT_STATUS
    )

    _expire(session_factory, tenant.tenant_id)

    assert _grant(session_factory, tenant.tenant_id, never).status == ACTIVE_GRANT_STATUS
    revoked_row = _grant(session_factory, tenant.tenant_id, revoked)
    assert revoked_row.status == REVOKED_GRANT_STATUS
    assert revoked_row.revoked_at == GRANTED_AT
    assert _grant(session_factory, tenant.tenant_id, already).status == EXPIRED_GRANT_STATUS
    assert _expired_events(shared_engine, tenant.tenant_id) == []
    assert _notifications(session_factory, tenant.tenant_id) == []


def test_another_tenants_lapsed_grant_is_untouched(
    session_factory: async_sessionmaker[AsyncSession],
    shared_engine: AsyncEngine,
) -> None:
    """Catches a sweep that builds its own statement over `grants` instead of
    going through the tenant-bound session: the runner calls this once per
    tenant, so a query that ignores the binding expires every tenant's grants
    on the first tenant's tick and writes each tenant's events onto the wrong
    chain. The neighbour's grant is lapsed by the same instant, so only the
    scoping can save it."""
    tenant = _tenant(session_factory)
    neighbour = _tenant(session_factory)
    mine = _plant_grant(session_factory, tenant, expires_at=LAPSED_AT)
    theirs = _plant_grant(session_factory, neighbour, expires_at=LAPSED_AT)

    _expire(session_factory, tenant.tenant_id)

    assert _grant(session_factory, tenant.tenant_id, mine).status == EXPIRED_GRANT_STATUS
    assert _grant(session_factory, neighbour.tenant_id, theirs).status == ACTIVE_GRANT_STATUS
    assert _expired_events(shared_engine, neighbour.tenant_id) == []
    assert _notifications(session_factory, neighbour.tenant_id) == []


# --- the audit record (criterion 7, a project rule) -------------------------


def test_one_grant_expired_event_per_row_names_the_grant_and_its_expiry(
    session_factory: async_sessionmaker[AsyncSession],
    shared_engine: AsyncEngine,
) -> None:
    """a project rule: what the event CARRIES, not that one was written.

    Catches an event whose subject is the user, the sweep or the tenant —
    none of which answers the question an auditor asks of this row ("which
    grant went, and when was it due") — one filed under an `actor_kind` that
    makes an automatic sweep look like a person's decision, and a payload
    carrying extra fields (a reason, a revoked_count) that would describe the
    expiry as a revocation. Two grants are planted so a single event covering
    a batch fails as well.
    """
    tenant = _tenant(session_factory)
    manager = _plant_user(session_factory, tenant)
    first = _plant_grant(session_factory, tenant, expires_at=LAPSED_AT, role_id=tenant.role_id)
    second = _plant_grant(
        session_factory, tenant, user_id=manager, expires_at=NOW, role_id=tenant.role_b_id
    )

    _expire(session_factory, tenant.tenant_id)

    events = _expired_events(shared_engine, tenant.tenant_id)
    assert len(events) == 2
    by_subject = {
        subject_id: (actor, subject_type, payload)
        for actor, subject_type, subject_id, payload in events
    }
    assert set(by_subject) == {str(first), str(second)}
    for actor_kind, subject_type, _payload in by_subject.values():
        assert actor_kind == "system"
        assert subject_type == "grant"
    for subject_id, (_actor, _subject_type, payload) in by_subject.items():
        assert set(payload) == EXPECTED_PAYLOAD_KEYS, subject_id
    first_payload = by_subject[str(first)][2]
    assert first_payload["user_id"] == str(tenant.user_id)
    assert first_payload["role_id"] == str(tenant.role_id)
    assert datetime.fromisoformat(first_payload["expires_at"]) == LAPSED_AT
    second_payload = by_subject[str(second)][2]
    assert second_payload["user_id"] == str(manager)
    assert second_payload["role_id"] == str(tenant.role_b_id)
    assert datetime.fromisoformat(second_payload["expires_at"]) == NOW


# --- the notifications (criterion 8, decision E) ------------------------------


def test_an_expiry_notifies_the_holder_and_the_department_manager(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Decision E. Catches a sweep that tells only the holder — the manager
    is the person who can re-request the access, and the design names both —
    and one that writes a single row carrying a list of recipients, which no
    per-user unread query can find. Each row is checked to name the grant, so
    two rows for two different subjects would not satisfy it either."""
    tenant = _tenant(session_factory)
    manager = _plant_user(session_factory, tenant)
    _set_manager(session_factory, tenant, manager)
    grant_id = _plant_grant(session_factory, tenant, expires_at=LAPSED_AT)

    _expire(session_factory, tenant.tenant_id)

    rows = _notifications(session_factory, tenant.tenant_id)
    assert sorted(row.user_id for row in rows) == sorted([tenant.user_id, manager])
    for row in rows:
        assert row.kind == "grant_expired"
        assert row.subject_type == "grant"
        assert row.subject_id == str(grant_id)
        assert row.read_at is None


def test_a_department_with_no_manager_notifies_the_holder_only(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Decision E's first skip. `seed_tenant` leaves `manager_user_id` null,
    so this is the ordinary case. Catches a sweep that notifies the manager
    unconditionally: with no manager to resolve, that is either a crash in
    the middle of a batch or a notification row pointing at nobody."""
    tenant = _tenant(session_factory)
    grant_id = _plant_grant(session_factory, tenant, expires_at=LAPSED_AT)

    _expire(session_factory, tenant.tenant_id)

    rows = _notifications(session_factory, tenant.tenant_id)
    assert [row.user_id for row in rows] == [tenant.user_id]
    assert rows[0].subject_id == str(grant_id)


def test_a_holder_who_is_their_own_manager_is_notified_once(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Decision E's second skip, and the one a "notify the manager if there
    is one" implementation still fails: the department HAS a manager here, so
    only the holder-equals-manager test can stop the second row. Catches a
    department head being told twice about their own grant."""
    tenant = _tenant(session_factory)
    _set_manager(session_factory, tenant, tenant.user_id)
    grant_id = _plant_grant(session_factory, tenant, expires_at=LAPSED_AT)

    _expire(session_factory, tenant.tenant_id)

    rows = _notifications(session_factory, tenant.tenant_id)
    assert [row.user_id for row in rows] == [tenant.user_id]
    assert rows[0].subject_id == str(grant_id)


# --- the counter (criterion 9) ------------------------------------------------


def test_the_expiry_counter_rises_by_exactly_the_number_of_grants_expired(
    session_factory: async_sessionmaker[AsyncSession],
    fresh_registry: CollectorRegistry,
) -> None:
    """Criterion 9, as an absolute value off a registry this test built.

    Three lapsed rows and one sweep, so the case separates the three
    plausible wrong sites: a counter incremented once per CALL reads 1, one
    incremented per candidate row before the filters reads more than 3
    (a null-expiry grant and an ahead-of-now grant are both planted), and one
    never incremented reads 0. A delta against the process-wide registry
    would pass for any of them if another test had moved it.
    """
    tenant = _tenant(session_factory)
    manager = _plant_user(session_factory, tenant)
    for role_id in (tenant.role_id, tenant.role_b_id):
        _plant_grant(session_factory, tenant, expires_at=LAPSED_AT, role_id=role_id)
    _plant_grant(session_factory, tenant, user_id=manager, expires_at=NOW)
    _plant_grant(session_factory, tenant, user_id=manager, expires_at=JUST_AHEAD)

    assert fresh_registry.get_sample_value(EXPIRIES_TOTAL) == 0.0

    _expire(session_factory, tenant.tenant_id)

    assert fresh_registry.get_sample_value(EXPIRIES_TOTAL) == 3.0


# --- two sweeps over one grant ------------------------------------------------


def test_two_callers_racing_one_overdue_grant_expire_it_once(
    session_factory: async_sessionmaker[AsyncSession],
    shared_engine: AsyncEngine,
) -> None:
    """The OUTCOME two overlapping runners must see, not a lock's proof — no
    kill claim.

    Two `expire_grants` calls handed to a thread pool each open their own
    engine and connection first, so their transactions often do not overlap
    at all and this case passes with the `FOR UPDATE` removed. It still earns
    its place: a second sweep over an already-expired row must be a no-op, so
    the duplicate audit event and the duplicate notification a re-sweep would
    write are what the assertions below refuse. The `status = 'active'` test
    is what makes that true, and it is pinned separately by
    `test_null_expiry_revoked_and_already_expired_grants_are_untouched`.
    """
    tenant = _tenant(session_factory)
    grant_id = _plant_grant(session_factory, tenant, expires_at=LAPSED_AT)

    def attempt() -> None:
        _expire(session_factory, tenant.tenant_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        for future in [pool.submit(attempt), pool.submit(attempt)]:
            future.result()

    assert _grant(session_factory, tenant.tenant_id, grant_id).status == EXPIRED_GRANT_STATUS
    events = _expired_events(shared_engine, tenant.tenant_id)
    assert [subject_id for _actor, _subject_type, subject_id, _payload in events] == [str(grant_id)]
    assert len(_notifications(session_factory, tenant.tenant_id)) == 1
