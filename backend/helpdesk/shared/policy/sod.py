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
    """Which of these HELD roles are half of a pair the holder already completes.

    `find_conflicts` above answers the request question — may this person be
    given this role — and needs a snapshot to do it. This answers the review
    question: given everything somebody holds right
    now, which of those roles should never have been held together. No
    request, no target, no clock.

    Both halves of a completed pair come back, not one: a review flags
    GRANTS, and an item for only one side of a conflict would put the wrong
    grant in front of the reviewer half the time. A rule about roles the
    holder does not have, and a pair only half held, contribute nothing —
    those are the two branches
    `test_policy_sod.py::test_held_conflicts_over_the_rule_table` walks from
    both sides of each rule, which is also what proves the answer does not
    depend on which column of the rule a role sits in.

    Duplicate rules over the same pair are harmless by construction: the
    answer is a set, so the second rule adds nothing rather than double
    counting. Nothing here reads `policy_ref` or `rule_id` — a review item
    carries one flag word (the design), and WHICH rule was broken is a question
    for the reviewer's detail view, not for this predicate.
    """
    conflicting: set[uuid.UUID] = set()
    for rule in rules:
        if rule.role_a_id in held_role_ids and rule.role_b_id in held_role_ids:
            conflicting.add(rule.role_a_id)
            conflicting.add(rule.role_b_id)
    return frozenset(conflicting)
