"""An earlier change section 6, first block: `read_sod_rules`, against Postgres.

Surface under test (written before it exists). The implementer must provide
exactly this:

``helpdesk.access.rules`` (new module, re-exported from ``helpdesk.access``)
    ``SodRuleRole`` - one resolved role, carrying exactly ``role_id``,
    ``slug``, ``name``, ``system_slug`` and ``system_name``.
    ``SodRuleDetail`` - one rule, carrying exactly ``rule_id``, ``role_a_id``,
    ``role_a``, ``role_b_id``, ``role_b``, ``rationale`` and ``policy_ref``.
    The two ids are ALWAYS present; each resolved block is ``SodRuleRole`` or
    ``None`` (plan decision B).
    ``async def read_sod_rules(session) -> tuple[SodRuleDetail, ...]`` - the
    bound tenant's rules, ordered ``policy_ref`` ascending then ``rule_id``
    ascending (decision C), in exactly three SELECTs whatever the rule count.

The session is tenant-bound (``helpdesk.core.db.tenant_session``), so the
helper takes no tenant argument at all: the rules are the token's tenant's own
rows and the guard in ``helpdesk/core/db.py`` is the only filter.

``read_sod_rules`` is imported from the PACKAGE and the two models from the
MODULE on purpose: plan section 3 says both paths exist, and importing one of
each pins the re-export without a case that asserts nothing else.

The plan names the two field sets but not the carrier (a frozen dataclass, as
`PendingWithContext` and `ReviewItemWithContext` are, or a Pydantic model, as
`ActiveGrant` is). `_field_names` below reads either, so the exact field set is
still asserted while the choice stays the implementer's - a value the plan left
open, recorded here rather than invented.

a project rule: ONE session-scoped database, its schema created exactly once.
Per-test isolation is a namespace, never a rebuild - every case seeds a tenant
of its OWN with `_seed.seed_tenant`, so no case can read another's rules.
`seed_tenant` plants exactly one `sod_rules` row per tenant (role A against
role B, `policy_ref` "POL-TEST-1"), and every expectation below is written as
that row plus what the case plants.

No pytest-asyncio: each case is a sync function driving one `asyncio.run`.
"""

import asyncio
import dataclasses
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from helpdesk.mcp_server.services.access_requests.rules import (
    SodRuleDetail,
    SodRuleRole,
    read_sod_rules,
)
from helpdesk.shared.core.db import tenant_session
from helpdesk.shared.models import Role, SodRule, System, Tenant
from tests.support.seed import SeededTenant, seed_tenant

DETAIL_FIELDS: tuple[str, ...] = (
    "rule_id",
    "role_a_id",
    "role_a",
    "role_b_id",
    "role_b",
    "rationale",
    "policy_ref",
)
"""Plan criterion 3's seven fields, written out.

A literal rather than a read of the model under test: the row is what 24eb
puts on the wire and 24f renders, so a field added or dropped is a red case
here rather than a page that breaks on the first rename."""

ROLE_FIELDS: tuple[str, ...] = ("role_id", "slug", "name", "system_slug", "system_name")
"""Plan criterion 4's five fields, written out for the same reason."""

SEEDED_POLICY_REF = "POL-TEST-1"
"""The `policy_ref` `_seed.seed_tenant` plants, read off `_seed.py`."""

SEEDED_RATIONALE = "fake rationale for tests"
"""The `rationale` `_seed.seed_tenant` plants, read off `_seed.py`."""

# Two planted refs that sort BEFORE the seeded one on every column of the
# order, and repeat so the tie-break is exercised (plan criterion 5).
REF_ONE = "FAKE-SOD-1"
REF_TWO = "FAKE-SOD-2"

EXPECTED_STATEMENTS = 3
"""Plan criterion 6's number, exactly: the rules, the roles
by id, the systems by id - whatever the rule count."""


def _field_names(value: object) -> tuple[str, ...]:
    """The declared field names of a frozen dataclass or a Pydantic model."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return tuple(field.name for field in dataclasses.fields(value))
    fields = getattr(type(value), "model_fields", None)
    assert fields is not None, f"{type(value)!r} is neither a dataclass nor a Pydantic model"
    return tuple(fields)


async def _seed(factory: async_sessionmaker[AsyncSession], label: str) -> SeededTenant:
    """A tenant of this case's own: the per-test namespace."""
    return await seed_tenant(factory, f"sodrules-{label}-{uuid.uuid4().hex[:10]}")


async def _bare_tenant(factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    """A tenant row and nothing else: no roles, no systems, no rules."""
    async with factory() as session:
        tenant = Tenant(
            slug=f"sodrules-empty-{uuid.uuid4().hex[:10]}",
            name="Fake Tenant (no rules)",
            residency="eu",
            settings={},
        )
        session.add(tenant)
        await session.commit()
        return tenant.id


async def _plant_role(
    factory: async_sessionmaker[AsyncSession],
    tenant: SeededTenant,
    slug: str,
    name: str,
) -> uuid.UUID:
    """One extra role on the tenant's seeded system."""
    async with tenant_session(factory, tenant.tenant_id) as session:
        role = Role(
            tenant_id=tenant.tenant_id,
            system_id=tenant.system_id,
            slug=slug,
            name=name,
            privilege="write",
            default_expiry_days=60,
        )
        session.add(role)
        await session.flush()
        return role.id


async def _plant_rule(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    *,
    rule_id: uuid.UUID,
    role_a_id: uuid.UUID,
    role_b_id: uuid.UUID,
    policy_ref: str,
    rationale: str = "fake planted rationale",
) -> None:
    """One `sod_rules` row with an id this case chose.

    The id is explicit because the tie-break case has to plant the rows in the
    OPPOSITE order to the one it expects back; `Repository.create` refuses
    `id` (it is a protected field), so the model is constructed directly, the
    way `_seed.py` does.
    """
    async with tenant_session(factory, tenant_id) as session:
        session.add(
            SodRule(
                id=rule_id,
                tenant_id=tenant_id,
                role_a_id=role_a_id,
                role_b_id=role_b_id,
                rationale=rationale,
                policy_ref=policy_ref,
            )
        )
        await session.flush()


async def _read(factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID) -> Any:
    async with tenant_session(factory, tenant_id) as session:
        return await read_sod_rules(session)


def _sorted_ids(count: int) -> list[uuid.UUID]:
    """`count` fresh uuids in the order Postgres orders them.

    Postgres compares `uuid` bytewise and Python compares the same 16 bytes,
    so sorting here is the same sequence an `ORDER BY rule_id` produces.
    """
    return sorted(uuid.uuid4() for _ in range(count))


def _resolved(block: SodRuleRole | None) -> SodRuleRole:
    """The block on a side this case expects to resolve, narrowed for mypy."""
    assert block is not None, "this side names a role of the bound tenant and must resolve"
    return block


def _one(details: Sequence[SodRuleDetail], rule_id: uuid.UUID) -> SodRuleDetail:
    matches = [detail for detail in details if detail.rule_id == rule_id]
    assert len(matches) == 1, f"expected exactly one row for {rule_id}, got {len(matches)}"
    return matches[0]


# --- the happy path (criteria 3 and 4) -----------------------------------------


def test_a_rule_comes_back_with_both_sides_resolved_and_the_exact_two_field_sets(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Criteria 3 and 4, as the literals 24eb puts on the wire.

    KILL: a helper that hands back the `SodRule` rows with their ids and
    nothing resolved, so the page shows two uuids where two role names belong;
    a block filled from the ROLE's own columns for the system half (the
    seeded system's slug and name differ from every role's, so `system_slug`
    carrying "role-a-..." fails); and a resolution that reads both sides from
    `role_a_id`, which the seeded pair catches because role A is the reader
    and role B the admin and their slugs, names and ids all differ.

    The field sets are asserted EXACTLY, so a row that grew an eighth field -
    a `created_at`, a second copy of the rationale, an `unresolvable` flag
    decision B deliberately does not carry - is as red as one short of a
    field.
    """

    async def scenario() -> None:
        factory = session_factory
        tenant = await _seed(factory, "happy")

        details = await _read(factory, tenant.tenant_id)

        assert len(details) == 1, details
        detail = details[0]
        assert _field_names(detail) == DETAIL_FIELDS
        assert detail.rule_id == tenant.sod_rule_id
        assert detail.rationale == SEEDED_RATIONALE
        assert detail.policy_ref == SEEDED_POLICY_REF
        assert detail.role_a_id == tenant.role_id
        assert detail.role_b_id == tenant.role_b_id

        async with tenant_session(factory, tenant.tenant_id) as session:
            role_a = await session.get(Role, tenant.role_id)
            role_b = await session.get(Role, tenant.role_b_id)
            system = await session.get(System, tenant.system_id)
        assert role_a is not None and role_b is not None and system is not None

        for side, role in ((detail.role_a, role_a), (detail.role_b, role_b)):
            block = _resolved(side)
            assert _field_names(block) == ROLE_FIELDS
            assert block.role_id == role.id
            assert block.slug == role.slug
            assert block.name == role.name
            assert block.system_slug == system.slug
            assert block.system_name == system.name

        assert detail.role_a is not None and detail.role_b is not None
        assert detail.role_a.slug != detail.role_b.slug
        assert detail.role_a.name != detail.role_b.name

    asyncio.run(scenario())


# --- the one total order, tie-break included (criterion 5) ----------------------


def test_the_rows_are_ordered_by_policy_ref_then_rule_id(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Criterion 5 and decision C, asserted as an exact sequence.

    KILL: an order left to Postgres (no ORDER BY, so insertion order is what
    comes back), an order by `created_at`, or an order on `policy_ref` alone.
    The four rows are planted in the REVERSE of the sequence they must come
    back in, and the two rows sharing `FAKE-SOD-1` are planted higher id
    first, so an implementation that pins only the first column leaves the
    tie in insertion order and fails here. The seeded row's `POL-TEST-1`
    sorts after both planted refs, so a DESCENDING order fails too.

    Four rules over four roles is also the fixture criterion 6 counts
    statements on, which is why they are planted together.
    """

    async def scenario() -> None:
        factory = session_factory
        tenant = await _seed(factory, "order")
        role_c = await _plant_role(factory, tenant, "role-c-fake", "Fake Writer C")
        role_d = await _plant_role(factory, tenant, "role-d-fake", "Fake Writer D")
        tie_low, tie_high = _sorted_ids(2)
        second_ref_id = uuid.uuid4()

        # Planted last-first: the seeded rule already exists, then
        # FAKE-SOD-2, then the tie's HIGH id, then its LOW id.
        await _plant_rule(
            factory,
            tenant.tenant_id,
            rule_id=second_ref_id,
            role_a_id=role_c,
            role_b_id=role_d,
            policy_ref=REF_TWO,
        )
        await _plant_rule(
            factory,
            tenant.tenant_id,
            rule_id=tie_high,
            role_a_id=tenant.role_b_id,
            role_b_id=role_c,
            policy_ref=REF_ONE,
        )
        await _plant_rule(
            factory,
            tenant.tenant_id,
            rule_id=tie_low,
            role_a_id=tenant.role_id,
            role_b_id=role_c,
            policy_ref=REF_ONE,
        )

        details = await _read(factory, tenant.tenant_id)

        assert [detail.rule_id for detail in details] == [
            tie_low,
            tie_high,
            second_ref_id,
            tenant.sod_rule_id,
        ]
        assert [detail.policy_ref for detail in details] == [
            REF_ONE,
            REF_ONE,
            REF_TWO,
            SEEDED_POLICY_REF,
        ]

    asyncio.run(scenario())


# --- the unresolvable side (criterion 8, decision B) ----------------------------


def test_a_rule_naming_no_role_row_answers_that_side_null_and_keeps_its_id(
    session_factory: async_sessionmaker[AsyncSession],
    shared_engine: AsyncEngine,
) -> None:
    """Criterion 8: one bad reference may not blank the board.

    KILL: a resolution written as `roles[rule.role_a_id]`, which raises
    `KeyError` and fails the whole call, and equally a comprehension that
    DROPS a rule it cannot resolve - the admin reading this page is the only
    person who can fix such a row, and both implementations hide it. The id is
    asserted present on the null side, so a helper that nulls the id along
    with the block fails as well: an id-less row is not actionable.

    The other side of the same rule and the tenant's other rule are asserted
    fully resolved in the same call, which is what separates "the read
    survived" from "the read answered".

    `sod_rules.role_a_id` carries a foreign key to `roles.id`, so a dangling
    reference cannot be inserted while the constraint stands. It is dropped
    and restored around the plant, on this file's own throwaway database:
    dropping it permanently would silently weaken every other case here.
    """

    async def scenario() -> None:
        factory = session_factory
        engine = shared_engine
        tenant = await _seed(factory, "dangling")
        role_c = await _plant_role(factory, tenant, "role-c-fake", "Fake Writer C")
        missing_role_id = uuid.uuid4()
        dangling_id = uuid.uuid4()

        constraint = await _drop_role_a_fk(engine)
        try:
            await _plant_rule(
                factory,
                tenant.tenant_id,
                rule_id=dangling_id,
                role_a_id=missing_role_id,
                role_b_id=role_c,
                policy_ref=REF_ONE,
            )

            details = await _read(factory, tenant.tenant_id)

            assert len(details) == 2, details
            dangling = _one(details, dangling_id)
            assert dangling.role_a is None
            assert dangling.role_a_id == missing_role_id
            assert dangling.role_b is not None
            assert dangling.role_b.role_id == role_c
            assert dangling.role_b.slug == "role-c-fake"

            seeded = _one(details, tenant.sod_rule_id)
            assert seeded.role_a is not None
            assert seeded.role_b is not None
            assert seeded.role_a.role_id == tenant.role_id
            assert seeded.role_b.role_id == tenant.role_b_id
        finally:
            await _delete_rule(engine, dangling_id)
            await _restore_constraint(engine, constraint)

    asyncio.run(scenario())


def test_a_rule_naming_another_tenants_role_answers_null_and_never_that_name(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Criterion 7's second half: the resolution obeys the tenant guard too.

    KILL: a resolution that leaves the tenant-scoped repository - a raw
    `select(Role)` on an unbound session, a lookup through a second engine, a
    join written by hand - which answers the OTHER tenant's role name here.
    That is the only genuinely dangerous answer this tool can give, and every
    other case in this file passes with it in place, because every other role
    id they resolve belongs to the reading tenant.

    The rule itself is the reading tenant's own row, so nothing but the ROLE
    lookup can decide this case: the id is asserted intact and the resolved
    block asserted `None`, and the foreign key stands (it points at
    `roles.id`, which is not tenant-scoped).
    """

    async def scenario() -> None:
        factory = session_factory
        reader = await _seed(factory, "reader")
        other = await _seed(factory, "other")
        foreign_id = uuid.uuid4()

        await _plant_rule(
            factory,
            reader.tenant_id,
            rule_id=foreign_id,
            role_a_id=other.role_id,
            role_b_id=reader.role_b_id,
            policy_ref=REF_ONE,
        )

        details = await _read(factory, reader.tenant_id)

        foreign = _one(details, foreign_id)
        assert foreign.role_a is None
        assert foreign.role_a_id == other.role_id
        assert foreign.role_b is not None
        assert foreign.role_b.role_id == reader.role_b_id

    asyncio.run(scenario())


# --- tenant isolation of the rules themselves (criterion 7) ---------------------


def test_another_tenants_rules_are_absent_from_the_answer(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Criterion 7's first half: only the bound tenant's rows are read.

    KILL: a helper that takes a tenant id and filters on it, or
    one that builds its own `select(SodRule)` outside the repository and so
    outside the session guard. Both answer BOTH tenants' rules here. The two
    tenants carry the same `policy_ref` and the same rationale, so the only
    thing separating the two sets is which tenant they belong to and the
    assertion is on ids, not on strings.

    The read is taken from each side in turn: a query that answered empty for
    everyone would pass a one-sided assertion.
    """

    async def scenario() -> None:
        factory = session_factory
        tenant_a = await _seed(factory, "iso-a")
        tenant_b = await _seed(factory, "iso-b")

        details_a = await _read(factory, tenant_a.tenant_id)
        details_b = await _read(factory, tenant_b.tenant_id)

        assert [detail.rule_id for detail in details_a] == [tenant_a.sod_rule_id]
        assert [detail.rule_id for detail in details_b] == [tenant_b.sod_rule_id]

    asyncio.run(scenario())


# --- a tenant with no rules (criterion 10) --------------------------------------


def test_a_tenant_with_no_rules_answers_an_empty_tuple(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Criterion 10: an empty configuration is an answer, never a failure.

    KILL: a helper that raises on the empty read - a `.one()` on the rules
    query, or an unguarded `get_many` over an empty id set (`IN ()` is not
    valid SQL, which is why `Repository.get_many` short-circuits and a
    hand-written lookup does not). Either turns a tenant that has simply not
    configured any SoD rules into a broken admin page, and no other case in
    this file can see it because every other tenant here has at least one
    rule.

    The tenant carries no roles and no systems either, so the two resolution
    statements have nothing to resolve: this is the one case that reaches the
    empty branch of both.
    """

    async def scenario() -> None:
        factory = session_factory
        tenant_id = await _bare_tenant(factory)

        details = await _read(factory, tenant_id)

        assert details == ()

    asyncio.run(scenario())


# --- three statements, whatever the rule count (criterion 6) --------------------


def test_the_whole_read_issues_exactly_three_selects(
    session_factory: async_sessionmaker[AsyncSession],
    shared_engine: AsyncEngine,
) -> None:
    """Criterion 6, counted on the engine rather than read off the source.

    KILL: the obvious resolution - `Repository.get` per side per rule - which
    is nine statements for the four rules here and grows with the tenant. A
    board that is merely slow looks identical from the outside, so nothing
    else in this file goes red for it; this counter fixes the number at three
    for a fixture of four rules spanning four roles, so a later refactor that
    reaches for `get` inside the loop is a failing case rather than a slow
    page.

    The window is measured around the SECOND call, after a warm-up read on
    the same engine: the first statement on a fresh pool carries the driver's
    own connection setup, which is not the helper's work.
    """

    async def scenario() -> None:
        factory = session_factory
        engine = shared_engine
        tenant = await _seed(factory, "count")
        role_c = await _plant_role(factory, tenant, "role-c-fake", "Fake Writer C")
        role_d = await _plant_role(factory, tenant, "role-d-fake", "Fake Writer D")
        for role_a_id, role_b_id, ref in (
            (tenant.role_id, role_c, REF_ONE),
            (tenant.role_b_id, role_c, REF_ONE),
            (role_c, role_d, REF_TWO),
        ):
            await _plant_rule(
                factory,
                tenant.tenant_id,
                rule_id=uuid.uuid4(),
                role_a_id=role_a_id,
                role_b_id=role_b_id,
                policy_ref=ref,
            )

        warm_up = await _read(factory, tenant.tenant_id)
        assert len(warm_up) == 4, warm_up

        statements: list[str] = []
        _watch(engine, statements)
        details = await _read(factory, tenant.tenant_id)

        assert len(details) == 4, details
        assert statements, "the statement counter never fired"
        assert len(statements) == EXPECTED_STATEMENTS, statements
        for statement in statements:
            assert statement.lstrip().upper().startswith("SELECT"), statement

    asyncio.run(scenario())


# --- probes around the helper, never through it ---------------------------------


def _watch(engine: AsyncEngine, statements: list[str]) -> None:
    """Record every statement this engine sends to Postgres.

    The shape `test_audit_search.py` already uses for the same question."""

    def _record(
        conn: Connection,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _record)


_FK_LOOKUP = (
    "SELECT conname, pg_get_constraintdef(oid) AS definition FROM pg_constraint "
    "WHERE conrelid = 'sod_rules'::regclass AND contype = 'f' "
    "AND conkey = ARRAY[(SELECT attnum FROM pg_attribute "
    "WHERE attrelid = 'sod_rules'::regclass AND attname = 'role_a_id')]"
)


async def _drop_role_a_fk(engine: AsyncEngine) -> tuple[str, str]:
    """Take the `role_a_id` foreign key off, returning what it takes to restore."""
    async with engine.begin() as conn:
        row = (await conn.execute(text(_FK_LOOKUP))).one()
        name, definition = str(row.conname), str(row.definition)
        await conn.execute(text(f'ALTER TABLE sod_rules DROP CONSTRAINT "{name}"'))
    return name, definition


async def _restore_constraint(engine: AsyncEngine, constraint: tuple[str, str]) -> None:
    """Put the foreign key back exactly as Postgres described it."""
    name, definition = constraint
    async with engine.begin() as conn:
        await conn.execute(text(f'ALTER TABLE sod_rules ADD CONSTRAINT "{name}" {definition}'))


async def _delete_rule(engine: AsyncEngine, rule_id: uuid.UUID) -> None:
    """Remove one planted rule so the restored constraint can be validated."""
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM sod_rules WHERE id = :rule_id"), {"rule_id": rule_id})
