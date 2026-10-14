"""The separation-of-duties rules read as RULES, for a person to look at.

`snapshot.py` already loads the same rows as `SodRuleSnapshot` — three ids and
a `policy_ref`, the vocabulary an earlier change's engine needs to judge a pair — and
`reviews.py` reads that same shape for its `sod_conflict` flag. Neither
answers the question the design's `/admin/rules` page asks: which two roles
conflict, in words, and why. That is what `read_sod_rules` is for, and it
decides nothing about access.

The read is three statements whatever the rule count:
the rules, then the roles both sides name, then the systems those roles
belong to. Resolving a side with `Repository.get` inside the loop answers
exactly the same rows and costs a round trip per side per rule, so the count
is pinned rather than the shape —
`test_access_sod_rules.py::test_the_whole_read_issues_exactly_three_selects`.

Every read goes through the tenant-scoped repository helpers, so the tenant
filter is the guard in `helpdesk/core/db.py` and this module adds no second
one: a `role_a_id` pointing at a role row in ANOTHER tenant resolves to
`None` rather than to that role's name, which is the only genuinely dangerous
answer this read could give
(`::test_a_rule_naming_another_tenants_role_answers_null_and_never_that_name`).

`helpdesk/policy/` is not imported and gains nothing: these
rows are records here, not inputs to a decision.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.crud import Repository
from helpdesk.shared.models import Role, SodRule, System


@dataclass(frozen=True, slots=True)
class SodRuleRole:
    """One side of a rule, resolved to the words the page shows.

    A frozen dataclass around what the two lookups answered, the shape
    `PendingWithContext` already uses: the wire model is 24eb's, and a second
    Pydantic model here would be two contracts to keep in step. The system's
    slug and name come from the SYSTEM row the role points at, never from the
    role's own columns — pinned by
    `test_access_sod_rules.py::test_a_rule_comes_back_with_both_sides_resolved_and_the_exact_two_field_sets`.
    """

    role_id: uuid.UUID
    slug: str
    name: str
    system_slug: str
    system_name: str


@dataclass(frozen=True, slots=True)
class SodRuleDetail:
    """One rule: both ids, both resolved sides, the rationale and the ref.

    The two ids are ALWAYS present and each resolved block may be `None`
    (plan decision B). A rule whose role row cannot be read is still a row an
    admin has to see and fix, so it is neither dropped nor raised on, and the
    id is what makes it actionable
    (`::test_a_rule_naming_no_role_row_answers_that_side_null_and_keeps_its_id`).
    Nullability IS the marker: there is no second fact for a flag to carry.
    """

    rule_id: uuid.UUID
    role_a_id: uuid.UUID
    role_a: SodRuleRole | None
    role_b_id: uuid.UUID
    role_b: SodRuleRole | None
    rationale: str
    policy_ref: str


async def read_sod_rules(session: AsyncSession) -> tuple[SodRuleDetail, ...]:
    """The bound tenant's SoD rules, resolved, in one total order.

    Ordered `policy_ref` ascending then `rule_id` ascending (decision C).
    Both columns are non-null on every row, so the order is total and
    assertable as an exact sequence, and it survives a side that resolves to
    nothing and has no name to sort by. Applied here rather than in the tool,
    so 24eb's wire and every later caller read the same sequence
    (`::test_the_rows_are_ordered_by_policy_ref_then_rule_id`).

    The session is tenant-bound, so there is no tenant argument to trust: the rules are the token's tenant's own rows.
    """
    rules = await Repository(session, SodRule).list()
    role_ids = {rule.role_a_id for rule in rules} | {rule.role_b_id for rule in rules}
    roles = await Repository(session, Role).get_many(role_ids)
    systems = await Repository(session, System).get_many(
        {role.system_id for role in roles.values()}
    )

    def resolve(role_id: uuid.UUID) -> SodRuleRole | None:
        role = roles.get(role_id)
        if role is None:
            return None
        system = systems.get(role.system_id)
        if system is None:
            return None
        return SodRuleRole(
            role_id=role.id,
            slug=role.slug,
            name=role.name,
            system_slug=system.slug,
            system_name=system.name,
        )

    details = [
        SodRuleDetail(
            rule_id=rule.id,
            role_a_id=rule.role_a_id,
            role_a=resolve(rule.role_a_id),
            role_b_id=rule.role_b_id,
            role_b=resolve(rule.role_b_id),
            rationale=rule.rationale,
            policy_ref=rule.policy_ref,
        )
        for rule in rules
    ]
    return tuple(sorted(details, key=lambda detail: (detail.policy_ref, detail.rule_id)))
