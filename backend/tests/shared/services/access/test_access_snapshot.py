"""An earlier change, section 6: `build_request_snapshot` over one seeded database.

Surface under test (written before it exists):

``helpdesk.access.directory``
    ``DirectoryProvider`` — the read half of the directory port (an earlier change
    decision A): ``lookup_user(user_id)``, ``list_membership_role_ids(user_id)``
    (role ids of the user's department template; empty frozenset for a user
    with no department) and ``list_account_role_ids(user_id, now)`` (role ids
    of the user's active, unrevoked, unexpired grants).
    ``SimulatedDirectory(session)`` — its only v1 implementation (the design),
    over a tenant-scoped session.

``helpdesk.access.snapshot``
    ``async def build_request_snapshot(session, directory, *,
    requester_user_id, target_user_id, role_id, requested_expiry_days=None,
    now) -> RequestSnapshot`` — an earlier change's snapshot filled from repository
    reads. ``now`` is aware and keyword-only; ``tenure_days`` counts days
    from ``users.start_date`` to ``now``'s date, clamped at 0. Unknown role,
    unknown target and a cross-tenant target (via the tenant guard) all raise
    ``helpdesk.errors.NotFoundError``. ``policy_refs`` stays the empty
    default until row 15 wires citations.

a project rule: one throwaway database, created and seeded once for this
module (argon2 makes seeding the slow part); per-test isolation is distinct
seeded users and unique justification strings, never a reseed. Expectations
are derived from ``scripts.seed``'s deterministic generator
(``generate_tenant``) and from reads around the builder — no hardcoded UUIDs.

No pytest-asyncio: each case is a sync function driving one ``asyncio.run``.
"""

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select

from helpdesk.shared.core.db import create_engine, create_session_factory, tenant_session
from helpdesk.shared.core.errors import NotFoundError
from helpdesk.shared.crud import Repository
from helpdesk.shared.models import AccessRequest, Base, RequestStatus, SodRule, System, User
from helpdesk.shared.policy.types import (
    PolicyRefs,
    Privilege,
    RequestSnapshot,
    Sensitivity,
    SystemCategory,
    TargetStatus,
)
from helpdesk.shared.services.access.directory import SimulatedDirectory
from helpdesk.shared.services.access.snapshot import build_request_snapshot
from scripts.seed import CUAN, NORDLICHT, generate_tenant
from tests.conftest import TempDatabaseFactory

# One fixed, aware clock for every build: a snapshot whose expected tenure or
# grant visibility depends on when the suite runs is a flaky test.
NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
# Far before every seeded start_date (the seed hires from 2018 onward): the
# clamp case must be reachable, not hypothetical.
BEFORE_ANY_HIRE = datetime(2015, 1, 1, 12, 0, tzinfo=UTC)

# The deterministic seed rows, recomputed rather than hardcoded: uuid5 ids
# from natural keys, so these are exactly what `seed_all` wrote.
ROWS = generate_tenant(NORDLICHT)
OTHER_TENANT_ROWS = generate_tenant(CUAN)

# Seeded users picked by department and index. `TEMPLATE_TARGET` has neither
# planted anomaly (its grants are exactly its department template);
# `STALE_TARGET`'s first template grant is the seeded stale one (active
# status, past expiry), which is what makes the expiry filter observable.
TEMPLATE_TARGET = ROWS.users_by_department["Production"][2]
STALE_TARGET = ROWS.users_by_department["Production"][1]
PENDING_TARGET = ROWS.users_by_department["Sales"][1]
REQUESTER = ROWS.users_by_department["Production"][3]
PRODUCTION_MANAGER = ROWS.users_by_department["Production"][0]

IN_TEMPLATE_ROLE = ROWS.role_ids[("mes", "operator")]
OUT_OF_TEMPLATE_ROLE = ROWS.role_ids[("payments", "approver")]
PROD_INFRA_ROLE = ROWS.role_ids[("prod-k8s", "auditor")]
HRIS_ROLE = ROWS.role_ids[("hris", "viewer")]
APPROVED_PENDING_ROLE = ROWS.role_ids[("payments", "initiator")]
NON_APPROVED_ROLE = ROWS.role_ids[("crm", "editor")]


@pytest.fixture(scope="session")
def access_db_url(pg: TempDatabaseFactory) -> Iterator[str]:
    """One database, created, migrated (metadata) and seeded exactly once."""
    url = pg.create()

    async def prepare() -> None:
        engine = create_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
        finally:
            await engine.dispose()

    asyncio.run(prepare())

    from scripts.seed import seed_all

    asyncio.run(seed_all(url))
    yield url
    pg.drop(url)


# --- helpers ------------------------------------------------------------------


def _build(
    url: str,
    *,
    requester_user_id: uuid.UUID,
    target_user_id: uuid.UUID,
    role_id: uuid.UUID,
    requested_expiry_days: int | None = None,
    now: datetime = NOW,
) -> RequestSnapshot:
    async def scenario() -> RequestSnapshot:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, ROWS.tenant_id) as session:
                directory = SimulatedDirectory(session)
                snapshot: RequestSnapshot = await build_request_snapshot(
                    session,
                    directory,
                    requester_user_id=requester_user_id,
                    target_user_id=target_user_id,
                    role_id=role_id,
                    requested_expiry_days=requested_expiry_days,
                    now=now,
                )
                return snapshot
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _start_date(url: str, user_id: uuid.UUID) -> date:
    async def scenario() -> date:
        engine = create_engine(url)
        try:
            async with engine.connect() as conn:
                row = (await conn.execute(select(User.start_date).where(User.id == user_id))).one()
                value = row[0]
                assert isinstance(value, date)
                return value
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _expected_active_role_ids(user_id: uuid.UUID, now: datetime) -> frozenset[uuid.UUID]:
    """The seed's own answer: every grant row for the user whose expiry has
    not passed (the seed writes all of them active and unrevoked)."""
    return frozenset(
        ROWS.role_ids[grant.role]
        for grant in ROWS.grants
        if grant.user_id == user_id and (grant.expires_at is None or grant.expires_at > now)
    )


def _insert_request(
    url: str,
    *,
    target_user_id: uuid.UUID,
    role_id: uuid.UUID,
    status: RequestStatus,
    justification: str,
) -> None:
    async def scenario() -> None:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, ROWS.tenant_id) as session:
                await Repository(session, AccessRequest).create(
                    requester_user_id=REQUESTER.id,
                    target_user_id=target_user_id,
                    role_id=role_id,
                    justification=justification,
                    requested_expiry_days=None,
                    risk_class="low",
                    decision="auto",
                    decision_reasons=[],
                    citations=[],
                    status=status.value,
                    approver_user_ids=[],
                    approvals=[],
                    session_id=None,
                    trace_id=None,
                    idempotency_key=None,
                )
        finally:
            await engine.dispose()

    asyncio.run(scenario())


# --- the filled snapshot -------------------------------------------------------


def test_an_in_template_role_fills_the_whole_snapshot(access_db_url: str) -> None:
    """Catches a builder that mis-joins any of the snapshot's sources: wrong
    template membership, a manager or owner read off the wrong row, role
    facts (privilege, sensitivity, expiry) not taken from the requested role,
    a dropped `requested_expiry_days`, or a `policy_refs` filled before row
    15 wires citations (decision B)."""
    snapshot = _build(
        access_db_url,
        requester_user_id=REQUESTER.id,
        target_user_id=TEMPLATE_TARGET.id,
        role_id=IN_TEMPLATE_ROLE,
        requested_expiry_days=30,
    )

    assert snapshot.requester_user_id == REQUESTER.id
    assert snapshot.role_in_department_template is True
    assert snapshot.requested_expiry_days == 30
    assert snapshot.policy_refs == PolicyRefs()

    assert snapshot.target.user_id == TEMPLATE_TARGET.id
    assert snapshot.target.status is TargetStatus.ACTIVE
    assert snapshot.target.department_manager_user_id == PRODUCTION_MANAGER.id

    # `mes` is seeded high/other with a write role expiring in 180 days.
    assert snapshot.role.role_id == IN_TEMPLATE_ROLE
    assert snapshot.role.privilege is Privilege.WRITE
    assert snapshot.role.sensitivity is Sensitivity.HIGH
    assert snapshot.role.system_category is SystemCategory.OTHER
    assert snapshot.role.default_expiry_days == 180
    assert snapshot.role.system_owner_user_id == ROWS.system_owners["mes"].id


def test_a_role_outside_the_department_template_reads_false(access_db_url: str) -> None:
    """Catches a membership check that answers a constant True or checks the
    tenant's templates rather than the target's own department: Production's
    template has no `payments.approver`."""
    snapshot = _build(
        access_db_url,
        requester_user_id=REQUESTER.id,
        target_user_id=TEMPLATE_TARGET.id,
        role_id=OUT_OF_TEMPLATE_ROLE,
    )

    assert snapshot.role_in_department_template is False


def test_tenure_counts_days_from_start_date_and_clamps_at_zero(access_db_url: str) -> None:
    """Catches a tenure computed from the wrong column, off the wrong clock,
    or allowed to go negative for a start date after `now` (the snapshot
    type refuses ge=0 violations, so an unclamped builder raises instead of
    returning)."""
    start = _start_date(access_db_url, TEMPLATE_TARGET.id)
    expected = (NOW.date() - start).days
    assert expected > 0, "fixture premise: the seeded start date is before NOW"

    snapshot = _build(
        access_db_url,
        requester_user_id=REQUESTER.id,
        target_user_id=TEMPLATE_TARGET.id,
        role_id=IN_TEMPLATE_ROLE,
    )
    assert snapshot.target.tenure_days == expected

    clamped = _build(
        access_db_url,
        requester_user_id=REQUESTER.id,
        target_user_id=TEMPLATE_TARGET.id,
        role_id=IN_TEMPLATE_ROLE,
        now=BEFORE_ANY_HIRE,
    )
    assert clamped.target.tenure_days == 0


def test_active_grant_role_ids_exclude_the_seeded_stale_grant(access_db_url: str) -> None:
    """Catches a grant read that ignores `expires_at`: STALE_TARGET's first
    template grant is seeded active-but-expired, so an implementation
    selecting on status alone reports one role id too many."""
    stale_roles = {
        ROWS.role_ids[grant.role]
        for grant in ROWS.grants
        if grant.user_id == STALE_TARGET.id
        and grant.expires_at is not None
        and grant.expires_at < NOW
    }
    assert stale_roles, "fixture premise: STALE_TARGET must hold a stale grant"

    snapshot = _build(
        access_db_url,
        requester_user_id=REQUESTER.id,
        target_user_id=STALE_TARGET.id,
        role_id=IN_TEMPLATE_ROLE,
    )

    assert snapshot.target.active_grant_role_ids == _expected_active_role_ids(STALE_TARGET.id, NOW)
    assert stale_roles.isdisjoint(snapshot.target.active_grant_role_ids)


def test_approved_pending_role_ids_hold_approved_requests_only(access_db_url: str) -> None:
    """Catches a pending set built from the wrong statuses: a request row
    with status `approved` must appear, a `pending` one for the same target
    must not (the design counts only approved-not-yet-executed state)."""
    marker = uuid.uuid4().hex
    _insert_request(
        access_db_url,
        target_user_id=PENDING_TARGET.id,
        role_id=APPROVED_PENDING_ROLE,
        status=RequestStatus.APPROVED,
        justification=f"fake approved request {marker}",
    )
    _insert_request(
        access_db_url,
        target_user_id=PENDING_TARGET.id,
        role_id=NON_APPROVED_ROLE,
        status=RequestStatus.PENDING,
        justification=f"fake pending request {marker}",
    )

    snapshot = _build(
        access_db_url,
        requester_user_id=REQUESTER.id,
        target_user_id=PENDING_TARGET.id,
        role_id=IN_TEMPLATE_ROLE,
    )

    assert snapshot.target.approved_pending_role_ids == frozenset({APPROVED_PENDING_ROLE})


def test_the_snapshot_carries_the_tenants_seeded_sod_rules(access_db_url: str) -> None:
    """Catches a builder that drops the SoD rules, invents rule ids, or loses
    `policy_ref` on the way through: the snapshot's rules must be exactly the
    tenant's `sod_rules` rows, id for id."""

    async def db_rules() -> set[tuple[uuid.UUID, uuid.UUID, uuid.UUID, str]]:
        engine = create_engine(access_db_url)
        try:
            async with engine.connect() as conn:
                rows = (
                    await conn.execute(
                        select(
                            SodRule.id, SodRule.role_a_id, SodRule.role_b_id, SodRule.policy_ref
                        ).where(SodRule.tenant_id == ROWS.tenant_id)
                    )
                ).all()
                return {(row[0], row[1], row[2], row[3]) for row in rows}
        finally:
            await engine.dispose()

    expected = asyncio.run(db_rules())
    assert expected, "fixture premise: the seed writes SoD rules for this tenant"

    snapshot = _build(
        access_db_url,
        requester_user_id=REQUESTER.id,
        target_user_id=TEMPLATE_TARGET.id,
        role_id=IN_TEMPLATE_ROLE,
    )

    assert {
        (rule.rule_id, rule.role_a_id, rule.role_b_id, rule.policy_ref)
        for rule in snapshot.sod_rules
    } == expected


@pytest.mark.parametrize(
    ("role_key", "expected_category"),
    [
        pytest.param(("prod-k8s", "auditor"), SystemCategory.PROD_INFRA, id="prod-k8s"),
        pytest.param(("hris", "viewer"), SystemCategory.HRIS, id="hris"),
        pytest.param(("wiki", "viewer"), SystemCategory.OTHER, id="other"),
    ],
)
def test_the_system_category_maps_through_from_the_seeded_column(
    access_db_url: str,
    role_key: tuple[str, str],
    expected_category: SystemCategory,
) -> None:
    """Catches a builder that defaults every system to OTHER (a migrated but
    unread category column, an earlier change risk 2) or maps the string onto the
    wrong enum member: the seeded `prod-k8s` must come out PROD_INFRA and
    `hris` must come out HRIS."""
    snapshot = _build(
        access_db_url,
        requester_user_id=REQUESTER.id,
        target_user_id=TEMPLATE_TARGET.id,
        role_id=ROWS.role_ids[role_key],
    )

    assert snapshot.role.system_category is expected_category
    assert snapshot.role.system_owner_user_id == ROWS.system_owners[role_key[0]].id


# --- the directory port's empty-department contract ----------------------------


def test_a_user_with_no_department_has_no_membership_roles(access_db_url: str) -> None:
    """Catches a membership read that raises on a NULL department or falls
    back to some other department's template: the contract is an empty
    frozenset."""

    async def scenario() -> frozenset[uuid.UUID]:
        engine = create_engine(access_db_url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, ROWS.tenant_id) as session:
                orphan = await Repository(session, User).create(
                    department_id=None,
                    email=f"no.department.{uuid.uuid4().hex}@fake.invalid",
                    display_name="Fake Orphan",
                    password_hash="not-a-real-hash-this-user-never-logs-in",
                    principals=["employee"],
                    status="active",
                    start_date=date(2020, 1, 1),
                    end_date=None,
                )
                membership: frozenset[uuid.UUID] = await SimulatedDirectory(
                    session
                ).list_membership_role_ids(orphan.id)
                return membership
        finally:
            await engine.dispose()

    assert asyncio.run(scenario()) == frozenset()


def test_a_system_created_without_a_category_reads_other(access_db_url: str) -> None:
    """Pins `System.category`'s server_default exactly: a
    row inserted without a category — the shape every pre-11a row has after
    the migration — must read `other`, the value the snapshot builder maps
    to `SystemCategory.OTHER`. Catches the default being dropped or renamed
    on the model side, which would silently diverge from the migration."""

    async def scenario() -> str:
        engine = create_engine(access_db_url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, ROWS.tenant_id) as session:
                system = await Repository(session, System).create(
                    slug=f"fake-defaulted-{uuid.uuid4().hex[:8]}",
                    name="Fake Defaulted System",
                    sensitivity="low",
                    owner_user_id=None,
                )
                return system.category
        finally:
            await engine.dispose()

    assert asyncio.run(scenario()) == "other"


# --- NOT_FOUND ----------------------------------------------------------------


def test_an_unknown_role_is_not_found(access_db_url: str) -> None:
    """Catches a builder that lets a missing role surface as an attribute
    error or a half-empty snapshot instead of the typed NOT_FOUND."""
    with pytest.raises(NotFoundError):
        _build(
            access_db_url,
            requester_user_id=REQUESTER.id,
            target_user_id=TEMPLATE_TARGET.id,
            role_id=uuid.uuid4(),
        )


def test_an_unknown_target_is_not_found(access_db_url: str) -> None:
    """Catches a builder that lets a missing target surface as anything but
    the typed NOT_FOUND."""
    with pytest.raises(NotFoundError):
        _build(
            access_db_url,
            requester_user_id=REQUESTER.id,
            target_user_id=uuid.uuid4(),
            role_id=IN_TEMPLATE_ROLE,
        )


def test_a_real_user_of_another_tenant_is_not_found(access_db_url: str) -> None:
    """Catches a target lookup that escapes the tenant guard: the id names a
    REAL seeded user, but in the other tenant, so a builder reading around
    the guard would find the row instead of raising."""
    foreign_user = OTHER_TENANT_ROWS.users[0]
    assert OTHER_TENANT_ROWS.tenant_id != ROWS.tenant_id

    async def exists() -> bool:
        engine = create_engine(access_db_url)
        try:
            async with engine.connect() as conn:
                row = (
                    await conn.execute(select(User.tenant_id).where(User.id == foreign_user.id))
                ).one_or_none()
                return row is not None and row[0] == OTHER_TENANT_ROWS.tenant_id
        finally:
            await engine.dispose()

    assert asyncio.run(exists()), "fixture premise: the foreign user really is seeded"

    with pytest.raises(NotFoundError):
        _build(
            access_db_url,
            requester_user_id=REQUESTER.id,
            target_user_id=foreign_user.id,
            role_id=IN_TEMPLATE_ROLE,
        )
