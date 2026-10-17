"""Separation-of-duties rules in words, for the admin rules page.

The engine reads these rows as ids. This read resolves each side to role and
system names so a person can see which two roles conflict and why. It decides
nothing about access. A role in another tenant resolves to `None`, never to
its name.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.crud import Repository
from helpdesk.shared.models import Role, SodRule, System


@dataclass(frozen=True, slots=True)
class SodRuleRole:
    """One side of a rule, resolved to the words the page shows.

    The system's slug and name come from the system row the role points at.
    """

    role_id: uuid.UUID
    slug: str
    name: str
    system_slug: str
    system_name: str


@dataclass(frozen=True, slots=True)
class SodRuleDetail:
    """One rule: both ids, both resolved sides, the rationale and the ref.

    A side may be `None` when its role row cannot be read. The rule is still
    shown with its id, because an admin has to see it to fix it.
    """

    rule_id: uuid.UUID
    role_a_id: uuid.UUID
    role_a: SodRuleRole | None
    role_b_id: uuid.UUID
    role_b: SodRuleRole | None
    rationale: str
    policy_ref: str


async def read_sod_rules(session: AsyncSession) -> tuple[SodRuleDetail, ...]:
    """The bound tenant's SoD rules, resolved, sorted by `policy_ref` then id.

    Three queries whatever the rule count. Both sort columns are never null,
    so the order holds even when a side resolves to nothing.
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
