"""Segregation-of-duties conflicts.

A rule pairs two roles one person must not hold together. A request
conflicts when the target holds the paired role, or has an approved request
for it not yet run. The same check runs again at grant time, since things can
change while a request waits.
"""

import uuid
from collections.abc import Iterable

from helpdesk.shared.policy.types import (
    RequestSnapshot,
    SodConflict,
    SodConflictSource,
    SodRuleSnapshot,
)


def paired_role_id(rule: SodRuleSnapshot, role_id: uuid.UUID) -> uuid.UUID | None:
    """The other half of the pair, or None if the rule is about other roles."""
    if rule.role_a_id == role_id:
        return rule.role_b_id
    if rule.role_b_id == role_id:
        return rule.role_a_id
    return None


def find_conflicts(snapshot: RequestSnapshot) -> tuple[SodConflict, ...]:
    """Conflicts in snapshot rule order, at most one per rule."""
    conflicts: list[SodConflict] = []
    for rule in snapshot.sod_rules:
        paired = paired_role_id(rule, snapshot.role.role_id)
        if paired is None:
            continue
        if paired in snapshot.target.active_grant_role_ids:
            source = SodConflictSource.ACTIVE_GRANT
        elif paired in snapshot.target.approved_pending_role_ids:
            source = SodConflictSource.APPROVED_REQUEST
        else:
            continue
        conflicts.append(
            SodConflict(
                rule_id=rule.rule_id,
                conflicting_role_id=paired,
                policy_ref=rule.policy_ref,
                source=source,
            )
        )
    return tuple(conflicts)


def held_conflicts(
    rules: Iterable[SodRuleSnapshot], held_role_ids: frozenset[uuid.UUID]
) -> frozenset[uuid.UUID]:
    """Which held roles form a full conflicting pair, for access reviews.

    Both halves come back: a review flags grants, and flagging only one side
    would show the reviewer the wrong grant half the time.
    """
    conflicting: set[uuid.UUID] = set()
    for rule in rules:
        if rule.role_a_id in held_role_ids and rule.role_b_id in held_role_ids:
            conflicting.add(rule.role_a_id)
            conflicting.add(rule.role_b_id)
    return frozenset(conflicting)
