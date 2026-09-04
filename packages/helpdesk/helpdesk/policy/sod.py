"""Segregation-of-duties conflicts (the design).

A rule pairs two roles that one person must not hold together. The requested
role conflicts when the target already holds the paired role, or has a
request for it that is approved but not yet executed — state that would
become a grant without any further decision.

This predicate is the same one row 11 re-runs at execution time: state can
change while a request waits for approval, so the check at classification
time is not the last word.
"""

import uuid

from helpdesk.policy.types import (
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
