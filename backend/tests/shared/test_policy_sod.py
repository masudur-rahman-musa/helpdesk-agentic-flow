"""Segregation of duties.

For each rule in `snapshot.sod_rules`, the paired role is the other side of the
pair when the requested role is one side; rules mentioning neither side are
ignored. A conflict exists when the paired role is in the target's active
grants (source `active_grant`) or in its approved-not-yet-executed requests
(source `approved_request`); when both hold, the source is `active_grant`.

Any conflict blocks, whatever the risk class, and returns no approvers.
"""

import uuid

import pytest

from helpdesk.shared.policy.engine import classify
from helpdesk.shared.policy.sod import held_conflicts
from helpdesk.shared.policy.types import (
    Decision,
    RiskClass,
    Sensitivity,
    SodConflict,
    SodConflictSource,
    SodRuleSnapshot,
    sod_conflict_reason,
)
from tests.support.policy import (
    ALL_REFS,
    OTHER_ROLE_ID,
    PAIRED_ROLE_ID,
    REF_BLOCKED,
    REF_RISK,
    ROLE_ID,
    RULE_2_ID,
    RULE_2_REF,
    RULE_ID,
    RULE_REF,
    UNRELATED_ROLE_ID,
    make_role,
    make_rule,
    make_snapshot,
    make_target,
)


def test_conflict_via_active_grant() -> None:
    rule = make_rule()
    result = classify(
        make_snapshot(
            target=make_target(active_grant_role_ids=frozenset({PAIRED_ROLE_ID})),
            sod_rules=(rule,),
        )
    )

    assert result.decision is Decision.BLOCKED
    assert result.required_approvers == ()
    assert result.sod_conflicts == (
        SodConflict(
            rule_id=RULE_ID,
            conflicting_role_id=PAIRED_ROLE_ID,
            policy_ref=RULE_REF,
            source=SodConflictSource.ACTIVE_GRANT,
        ),
    )
    assert result.reasons == ("IN_TEMPLATE", sod_conflict_reason(RULE_ID))


def test_conflict_via_approved_not_yet_executed_request() -> None:
    result = classify(
        make_snapshot(
            target=make_target(approved_pending_role_ids=frozenset({PAIRED_ROLE_ID})),
            sod_rules=(make_rule(),),
        )
    )

    assert result.decision is Decision.BLOCKED
    assert result.sod_conflicts == (
        SodConflict(
            rule_id=RULE_ID,
            conflicting_role_id=PAIRED_ROLE_ID,
            policy_ref=RULE_REF,
            source=SodConflictSource.APPROVED_REQUEST,
        ),
    )


def test_active_grant_wins_when_both_sources_hold() -> None:
    result = classify(
        make_snapshot(
            target=make_target(
                active_grant_role_ids=frozenset({PAIRED_ROLE_ID}),
                approved_pending_role_ids=frozenset({PAIRED_ROLE_ID}),
            ),
            sod_rules=(make_rule(),),
        )
    )

    assert len(result.sod_conflicts) == 1
    assert result.sod_conflicts[0].source is SodConflictSource.ACTIVE_GRANT


def test_pair_is_matched_in_either_direction() -> None:
    """The requested role sitting in `role_b_id` conflicts with `role_a_id`."""
    rule = make_rule(role_a_id=PAIRED_ROLE_ID, role_b_id=ROLE_ID)
    result = classify(
        make_snapshot(
            target=make_target(active_grant_role_ids=frozenset({PAIRED_ROLE_ID})),
            sod_rules=(rule,),
        )
    )

    assert result.decision is Decision.BLOCKED
    assert result.sod_conflicts[0].conflicting_role_id == PAIRED_ROLE_ID


def test_no_conflict_when_the_target_does_not_hold_the_paired_role() -> None:
    result = classify(
        make_snapshot(
            target=make_target(active_grant_role_ids=frozenset({UNRELATED_ROLE_ID})),
            sod_rules=(make_rule(),),
        )
    )

    assert result.decision is Decision.AUTO
    assert result.sod_conflicts == ()
    assert result.reasons == ("IN_TEMPLATE",)


def test_rule_that_mentions_neither_side_is_ignored() -> None:
    """The target holds both roles of the rule, but the request is elsewhere."""
    rule = make_rule(role_a_id=OTHER_ROLE_ID, role_b_id=UNRELATED_ROLE_ID)
    result = classify(
        make_snapshot(
            target=make_target(active_grant_role_ids=frozenset({OTHER_ROLE_ID, UNRELATED_ROLE_ID})),
            sod_rules=(rule,),
        )
    )

    assert result.decision is Decision.AUTO
    assert result.sod_conflicts == ()


def test_holding_the_requested_role_itself_is_not_a_conflict() -> None:
    result = classify(
        make_snapshot(
            target=make_target(active_grant_role_ids=frozenset({ROLE_ID})),
            sod_rules=(make_rule(),),
        )
    )

    assert result.sod_conflicts == ()


def test_several_conflicts_are_reported_in_snapshot_rule_order() -> None:
    rule_one = make_rule()
    rule_two = make_rule(
        rule_id=RULE_2_ID,
        role_a_id=OTHER_ROLE_ID,
        role_b_id=ROLE_ID,
        policy_ref=RULE_2_REF,
    )
    result = classify(
        make_snapshot(
            target=make_target(
                active_grant_role_ids=frozenset({PAIRED_ROLE_ID}),
                approved_pending_role_ids=frozenset({OTHER_ROLE_ID}),
            ),
            sod_rules=(rule_one, rule_two),
            policy_refs=ALL_REFS,
        )
    )

    assert result.decision is Decision.BLOCKED
    assert result.sod_conflicts == (
        SodConflict(
            rule_id=RULE_ID,
            conflicting_role_id=PAIRED_ROLE_ID,
            policy_ref=RULE_REF,
            source=SodConflictSource.ACTIVE_GRANT,
        ),
        SodConflict(
            rule_id=RULE_2_ID,
            conflicting_role_id=OTHER_ROLE_ID,
            policy_ref=RULE_2_REF,
            source=SodConflictSource.APPROVED_REQUEST,
        ),
    )
    assert result.reasons == (
        "IN_TEMPLATE",
        sod_conflict_reason(RULE_ID),
        sod_conflict_reason(RULE_2_ID),
    )
    assert result.policy_refs == (REF_RISK, REF_BLOCKED, RULE_REF, RULE_2_REF)


CLASS_CASES: list[tuple[str, bool, Sensitivity, RiskClass]] = [
    ("would-be-low-auto", True, Sensitivity.LOW, RiskClass.LOW),
    ("would-be-elevated", False, Sensitivity.LOW, RiskClass.ELEVATED),
    ("would-be-privileged", False, Sensitivity.HIGH, RiskClass.PRIVILEGED),
    ("would-be-critical", True, Sensitivity.CRITICAL, RiskClass.CRITICAL),
]


@pytest.mark.parametrize(
    ("in_template", "sensitivity", "expected_class"),
    [case[1:] for case in CLASS_CASES],
    ids=[case[0] for case in CLASS_CASES],
)
def test_a_conflict_beats_every_risk_class(
    in_template: bool, sensitivity: Sensitivity, expected_class: RiskClass
) -> None:
    result = classify(
        make_snapshot(
            role_in_department_template=in_template,
            role=make_role(sensitivity=sensitivity),
            target=make_target(active_grant_role_ids=frozenset({PAIRED_ROLE_ID})),
            sod_rules=(make_rule(),),
        )
    )

    assert result.risk_class == expected_class
    assert result.decision is Decision.BLOCKED
    assert result.required_approvers == ()
    assert sod_conflict_reason(RULE_ID) in result.reasons


def test_conflict_policy_ref_is_cited_even_without_snapshot_refs() -> None:
    result = classify(
        make_snapshot(
            target=make_target(active_grant_role_ids=frozenset({PAIRED_ROLE_ID})),
            sod_rules=(make_rule(),),
        )
    )

    assert result.policy_refs == (RULE_REF,)


def test_no_rules_means_no_conflicts() -> None:
    result = classify(
        make_snapshot(
            target=make_target(
                active_grant_role_ids=frozenset({PAIRED_ROLE_ID, OTHER_ROLE_ID}),
            )
        )
    )

    assert result.sod_conflicts == ()
    assert result.decision is Decision.AUTO


# --- `held_conflicts` over a set somebody already holds ----
#
# A different question from `find_conflicts` above, which asks whether ONE
# requested role may be added. The review asks it of a set nobody is requesting:
# which of the roles this person ALREADY holds conflict with another role they
# hold. The table below drives it to 100% line and branch, which is the floor
# `make test-policy` holds `helpdesk.policy` to.

HeldConflictCase = tuple[str, tuple[SodRuleSnapshot, ...], frozenset[uuid.UUID], set[uuid.UUID]]

HELD_CONFLICT_CASES: list[HeldConflictCase] = [
    (
        "no rules at all",
        (),
        frozenset({ROLE_ID, PAIRED_ROLE_ID}),
        set(),
    ),
    (
        "a rule about two other roles",
        (make_rule(role_a_id=OTHER_ROLE_ID, role_b_id=UNRELATED_ROLE_ID),),
        frozenset({ROLE_ID, PAIRED_ROLE_ID}),
        set(),
    ),
    (
        "both halves held",
        (make_rule(),),
        frozenset({ROLE_ID, PAIRED_ROLE_ID}),
        {ROLE_ID, PAIRED_ROLE_ID},
    ),
    (
        "only role_a held",
        (make_rule(),),
        frozenset({ROLE_ID, UNRELATED_ROLE_ID}),
        set(),
    ),
    (
        "only role_b held",
        (make_rule(),),
        frozenset({PAIRED_ROLE_ID, UNRELATED_ROLE_ID}),
        set(),
    ),
    (
        "the same pair declared the other way round",
        (make_rule(role_a_id=PAIRED_ROLE_ID, role_b_id=ROLE_ID),),
        frozenset({ROLE_ID, PAIRED_ROLE_ID}),
        {ROLE_ID, PAIRED_ROLE_ID},
    ),
    (
        "the same pair declared twice",
        (make_rule(), make_rule(rule_id=RULE_2_ID, policy_ref=RULE_2_REF)),
        frozenset({ROLE_ID, PAIRED_ROLE_ID}),
        {ROLE_ID, PAIRED_ROLE_ID},
    ),
    (
        "one conflicting pair beside an innocent held role",
        (make_rule(), make_rule(rule_id=RULE_2_ID, role_a_id=OTHER_ROLE_ID)),
        frozenset({ROLE_ID, PAIRED_ROLE_ID, UNRELATED_ROLE_ID}),
        {ROLE_ID, PAIRED_ROLE_ID},
    ),
    (
        "holding nothing",
        (make_rule(),),
        frozenset(),
        set(),
    ),
]


@pytest.mark.parametrize(
    ("rules", "held", "expected"),
    [case[1:] for case in HELD_CONFLICT_CASES],
    ids=[case[0] for case in HELD_CONFLICT_CASES],
)
def test_held_conflicts_returns_every_held_role_paired_with_another_held_role(
    rules: tuple[SodRuleSnapshot, ...],
    held: frozenset[uuid.UUID],
    expected: set[uuid.UUID],
) -> None:
    """Catches the three ways this predicate is normally got wrong.

    "Only role_a held" and "only role_b held" catch a rule read as "either
    half is enough", which would flag half the tenant as conflicted; they are
    two rows rather than one because the two sides are separate branches of
    the `and`. "The same pair declared the other way round" catches a check
    written against `role_a_id` alone, which silently depends on the order a
    rule happened to be written in. "Both halves held" is asserted to contain
    BOTH ids, not just one, because the review flags each conflicting grant
    and a set holding only the first half leaves its partner reading `ok`.
    "The same pair declared twice" catches a list return, whose duplicate
    would double-count the same conflict in `flag_counts`.
    """
    assert held_conflicts(rules, held) == frozenset(expected)


def test_held_conflicts_answers_a_frozenset_and_touches_neither_argument() -> None:
    """Catches a `set` return, and an implementation that mutates the set it
    was handed.

    `held_role_ids` is the caller's own view of what one person holds and is
    read again after this call to decide each item's flag; a function that
    added the conflicts into it (or removed the innocent roles from it) would
    change the answer for every grant reviewed afterwards, which no assertion
    on the return value alone can see. Pinning `frozenset` matters because the
    result is used as a lookup while items are built: a mutable answer invites
    exactly the same defect one layer up.
    """
    rules = (make_rule(),)
    held = frozenset({ROLE_ID, PAIRED_ROLE_ID, UNRELATED_ROLE_ID})

    result = held_conflicts(rules, held)

    assert isinstance(result, frozenset)
    assert held == frozenset({ROLE_ID, PAIRED_ROLE_ID, UNRELATED_ROLE_ID})
    assert rules == (make_rule(),)
