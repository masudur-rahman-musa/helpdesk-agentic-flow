"""Segregation of duties, the design.

For each rule in `snapshot.sod_rules`, the paired role is the other side of the
pair when the requested role is one side; rules mentioning neither side are
ignored. A conflict exists when the paired role is in the target's active
grants (source `active_grant`) or in its approved-not-yet-executed requests
(source `approved_request`); when both hold, the source is `active_grant`.

Any conflict blocks, whatever the risk class, and returns no approvers.
"""

import pytest
from _policy import (
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
from helpdesk.policy.engine import classify
from helpdesk.policy.types import (
    Decision,
    RiskClass,
    Sensitivity,
    SodConflict,
    SodConflictSource,
    sod_conflict_reason,
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
