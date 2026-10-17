"""Decision, approver chain, self-admin escalation, leaver, policy refs.

Approver chain by FINAL risk class:
    elevated   -> [department_manager]
    privileged -> [department_manager, system_owner]
    critical   -> [department_manager, system_owner] plus it_admin for
                  `prod_infra`, hr for `hris`, nothing extra for `other`.

Substitution ("a manager cannot approve their own request", read as
the general rule it states in its second sentence): any slot resolving to the
requester or the target is replaced in place by the next usable slot in
[system_owner, it_admin], where usable means not already in the chain, not
resolving to the requester or the target, and actually filled. A slot with no
person in it is not a conflict of interest and stands, but is never chosen as
a replacement. If nothing is usable the slot is dropped.
"""

import uuid

import pytest
from pydantic import ValidationError

from helpdesk.shared.policy.engine import classify
from helpdesk.shared.policy.types import (
    ApproverSlot,
    Classification,
    Decision,
    PolicyRefs,
    Privilege,
    RequiredApprover,
    RiskClass,
    Sensitivity,
    SystemCategory,
    is_valid_reason,
    sod_conflict_reason,
)
from tests.support.policy import (
    ALL_REFS,
    MANAGER_ID,
    REF_APPROVALS,
    REF_BLOCKED,
    REF_EXPIRY,
    REF_RISK,
    REQUESTER_ID,
    RULE_REF,
    SYSTEM_OWNER_ID,
    TARGET_ID,
    make_role,
    make_rule,
    make_snapshot,
    make_target,
)

DM = ApproverSlot.DEPARTMENT_MANAGER
SO = ApproverSlot.SYSTEM_OWNER
IT = ApproverSlot.IT_ADMIN
HR = ApproverSlot.HR


def _low() -> dict[str, object]:
    return {"role_in_department_template": True, "role": make_role(sensitivity=Sensitivity.LOW)}


def _elevated() -> dict[str, object]:
    return {"role_in_department_template": False, "role": make_role(sensitivity=Sensitivity.LOW)}


def _privileged() -> dict[str, object]:
    return {"role_in_department_template": False, "role": make_role(sensitivity=Sensitivity.HIGH)}


def _critical(category: SystemCategory = SystemCategory.OTHER) -> dict[str, object]:
    return {
        "role_in_department_template": True,
        "role": make_role(sensitivity=Sensitivity.CRITICAL, system_category=category),
    }


# --------------------------------------------------------------------------
# decision + approver chain per class
# --------------------------------------------------------------------------

CHAIN_CASES: list[
    tuple[str, dict[str, object], RiskClass, Decision, tuple[RequiredApprover, ...]]
] = [
    ("low-auto-no-approvers", _low(), RiskClass.LOW, Decision.AUTO, ()),
    (
        "elevated-manager-only",
        _elevated(),
        RiskClass.ELEVATED,
        Decision.APPROVAL,
        (RequiredApprover(slot=DM, user_id=MANAGER_ID),),
    ),
    (
        "privileged-manager-and-owner",
        _privileged(),
        RiskClass.PRIVILEGED,
        Decision.APPROVAL,
        (
            RequiredApprover(slot=DM, user_id=MANAGER_ID),
            RequiredApprover(slot=SO, user_id=SYSTEM_OWNER_ID),
        ),
    ),
    (
        "critical-prod_infra-adds-it_admin",
        _critical(SystemCategory.PROD_INFRA),
        RiskClass.CRITICAL,
        Decision.APPROVAL,
        (
            RequiredApprover(slot=DM, user_id=MANAGER_ID),
            RequiredApprover(slot=SO, user_id=SYSTEM_OWNER_ID),
            RequiredApprover(slot=IT, user_id=None),
        ),
    ),
    (
        "critical-hris-adds-hr",
        _critical(SystemCategory.HRIS),
        RiskClass.CRITICAL,
        Decision.APPROVAL,
        (
            RequiredApprover(slot=DM, user_id=MANAGER_ID),
            RequiredApprover(slot=SO, user_id=SYSTEM_OWNER_ID),
            RequiredApprover(slot=HR, user_id=None),
        ),
    ),
    (
        "critical-other-adds-nothing",
        _critical(SystemCategory.OTHER),
        RiskClass.CRITICAL,
        Decision.APPROVAL,
        (
            RequiredApprover(slot=DM, user_id=MANAGER_ID),
            RequiredApprover(slot=SO, user_id=SYSTEM_OWNER_ID),
        ),
    ),
]


@pytest.mark.parametrize(
    ("kwargs", "expected_class", "expected_decision", "expected_approvers"),
    [case[1:] for case in CHAIN_CASES],
    ids=[case[0] for case in CHAIN_CASES],
)
def test_decision_and_approver_chain(
    kwargs: dict[str, object],
    expected_class: RiskClass,
    expected_decision: Decision,
    expected_approvers: tuple[RequiredApprover, ...],
) -> None:
    result = classify(make_snapshot(**kwargs))

    assert result.risk_class == expected_class
    assert result.decision == expected_decision
    assert result.required_approvers == expected_approvers
    assert result.sod_conflicts == ()


def test_low_is_auto_and_carries_no_approvers() -> None:
    result = classify(make_snapshot(**_low()))

    assert result.decision is Decision.AUTO
    assert result.required_approvers == ()


# --------------------------------------------------------------------------
# manager substitution (a manager cannot approve their own request)
# --------------------------------------------------------------------------


def test_manager_is_requester_on_elevated_falls_back_to_system_owner() -> None:
    result = classify(
        make_snapshot(
            **_elevated(),
            target=make_target(department_manager_user_id=REQUESTER_ID),
        )
    )

    assert result.risk_class is RiskClass.ELEVATED
    assert result.required_approvers == (RequiredApprover(slot=SO, user_id=SYSTEM_OWNER_ID),)


def test_manager_is_requester_on_privileged_falls_back_to_it_admin() -> None:
    """The system owner is already in the chain, so the next eligible slot is it_admin."""
    result = classify(
        make_snapshot(
            **_privileged(),
            target=make_target(department_manager_user_id=REQUESTER_ID),
        )
    )

    assert result.required_approvers == (
        RequiredApprover(slot=IT, user_id=None),
        RequiredApprover(slot=SO, user_id=SYSTEM_OWNER_ID),
    )


def test_manager_and_owner_both_requester_falls_back_to_it_admin() -> None:
    result = classify(
        make_snapshot(
            role_in_department_template=False,
            role=make_role(sensitivity=Sensitivity.LOW, system_owner_user_id=REQUESTER_ID),
            target=make_target(department_manager_user_id=REQUESTER_ID),
        )
    )

    assert result.risk_class is RiskClass.ELEVATED
    assert result.required_approvers == (RequiredApprover(slot=IT, user_id=None),)


def test_manager_is_requester_on_critical_prod_infra_drops_the_manager_slot() -> None:
    """No fallback is left: the chain already holds system_owner and it_admin.

    Duplicating it_admin would collapse three approvers into two people, so
    the manager slot goes instead. Two independent approvers remain and
    neither of them is the requester.
    """
    result = classify(
        make_snapshot(
            role=make_role(
                sensitivity=Sensitivity.CRITICAL,
                system_category=SystemCategory.PROD_INFRA,
            ),
            target=make_target(department_manager_user_id=REQUESTER_ID),
        )
    )

    assert result.risk_class is RiskClass.CRITICAL
    assert result.required_approvers == (
        RequiredApprover(slot=SO, user_id=SYSTEM_OWNER_ID),
        RequiredApprover(slot=IT, user_id=None),
    )


def test_manager_is_target_is_also_substituted() -> None:
    result = classify(
        make_snapshot(
            **_elevated(),
            target=make_target(department_manager_user_id=TARGET_ID),
        )
    )

    assert result.required_approvers == (RequiredApprover(slot=SO, user_id=SYSTEM_OWNER_ID),)


def test_unresolved_manager_is_not_substituted() -> None:
    """No manager in the directory is not a conflict of interest: the slot stays."""
    result = classify(
        make_snapshot(
            **_elevated(),
            target=make_target(department_manager_user_id=None),
        )
    )

    assert result.required_approvers == (RequiredApprover(slot=DM, user_id=None),)


def test_system_owner_who_is_the_requester_is_substituted() -> None:
    """The owner of the system is no more allowed to self-approve than the manager."""
    result = classify(
        make_snapshot(
            role_in_department_template=False,
            role=make_role(sensitivity=Sensitivity.HIGH, system_owner_user_id=REQUESTER_ID),
        )
    )

    assert result.required_approvers == (
        RequiredApprover(slot=DM, user_id=MANAGER_ID),
        RequiredApprover(slot=IT, user_id=None),
    )


def test_system_owner_who_is_the_target_is_substituted_on_critical() -> None:
    """it_admin is already in the chain, so the owner slot is dropped, not doubled."""
    result = classify(
        make_snapshot(
            role=make_role(
                sensitivity=Sensitivity.CRITICAL,
                system_category=SystemCategory.PROD_INFRA,
                system_owner_user_id=TARGET_ID,
            ),
        )
    )

    assert result.required_approvers == (
        RequiredApprover(slot=DM, user_id=MANAGER_ID),
        RequiredApprover(slot=IT, user_id=None),
    )


def test_fallback_skips_an_unfilled_system_owner_seat() -> None:
    """An ownerless system would leave a request nobody can approve, so it_admin wins."""
    result = classify(
        make_snapshot(
            role_in_department_template=False,
            role=make_role(sensitivity=Sensitivity.LOW, system_owner_user_id=None),
            target=make_target(department_manager_user_id=REQUESTER_ID),
        )
    )

    assert result.required_approvers == (RequiredApprover(slot=IT, user_id=None),)


@pytest.mark.parametrize("manager_id", [MANAGER_ID, REQUESTER_ID, TARGET_ID, None])
@pytest.mark.parametrize("owner_id", [SYSTEM_OWNER_ID, REQUESTER_ID, TARGET_ID, None])
@pytest.mark.parametrize("category", list(SystemCategory))
@pytest.mark.parametrize("sensitivity", list(Sensitivity))
@pytest.mark.parametrize("privilege", list(Privilege))
def test_no_approver_is_ever_a_party_to_the_request(
    manager_id: uuid.UUID | None,
    owner_id: uuid.UUID | None,
    category: SystemCategory,
    sensitivity: Sensitivity,
    privilege: Privilege,
) -> None:
    """The whole point of substitution, as a property over every chain shape.

    A slot with no person in it is exempt: it names a seat, not an approver.
    """
    result = classify(
        make_snapshot(
            role_in_department_template=False,
            role=make_role(
                sensitivity=sensitivity,
                privilege=privilege,
                system_category=category,
                system_owner_user_id=owner_id,
            ),
            target=make_target(department_manager_user_id=manager_id),
        )
    )

    parties = {REQUESTER_ID, TARGET_ID}
    assert not [approver for approver in result.required_approvers if approver.user_id in parties]
    slots = [approver.slot for approver in result.required_approvers]
    assert len(slots) == len(set(slots)), "a slot must not appear twice"
    if result.risk_class is not RiskClass.LOW:
        assert slots, "a request needing approval must name someone to approve it"


def test_a_colliding_critical_request_bottoms_out_at_one_approver() -> None:
    """The floor, stated so a later change cannot lower it unnoticed.

    The requester manages the target and owns the system, so both named seats
    are filled by a party to the request. Only `it_admin` is left. The policy
    asks for three; the engine is pure and has nobody else to name, so one is
    the honest answer and resolving a delegate is left to the caller.
    """
    result = classify(
        make_snapshot(
            role=make_role(
                sensitivity=Sensitivity.CRITICAL,
                system_category=SystemCategory.PROD_INFRA,
                system_owner_user_id=REQUESTER_ID,
            ),
            target=make_target(department_manager_user_id=REQUESTER_ID),
        )
    )

    assert result.risk_class is RiskClass.CRITICAL
    assert result.decision is Decision.APPROVAL
    assert result.required_approvers == (RequiredApprover(slot=IT, user_id=None),)


# --------------------------------------------------------------------------
# self-requested admin (last row of the approver table)
# --------------------------------------------------------------------------


def test_self_requested_admin_escalates_privileged_to_critical() -> None:
    result = classify(
        make_snapshot(
            requester_user_id=TARGET_ID,
            role_in_department_template=True,
            role=make_role(privilege=Privilege.ADMIN, sensitivity=Sensitivity.LOW),
        )
    )

    assert result.risk_class is RiskClass.CRITICAL
    assert result.decision is Decision.APPROVAL
    assert result.reasons == ("IN_TEMPLATE", "PRIVILEGE_ADMIN", "SELF_ADMIN_ESCALATED")
    assert result.required_approvers == (
        RequiredApprover(slot=DM, user_id=MANAGER_ID),
        RequiredApprover(slot=SO, user_id=SYSTEM_OWNER_ID),
    )


def test_self_requested_admin_on_critical_system_stays_critical_without_reason() -> None:
    """Nothing above critical, so the class did not change and no reason is emitted."""
    result = classify(
        make_snapshot(
            requester_user_id=TARGET_ID,
            role_in_department_template=True,
            role=make_role(privilege=Privilege.ADMIN, sensitivity=Sensitivity.CRITICAL),
        )
    )

    assert result.risk_class is RiskClass.CRITICAL
    assert result.reasons == ("IN_TEMPLATE", "CRITICAL_SYSTEM", "PRIVILEGE_ADMIN")


def test_self_request_without_admin_is_not_escalated() -> None:
    result = classify(
        make_snapshot(
            requester_user_id=TARGET_ID,
            role_in_department_template=False,
            role=make_role(privilege=Privilege.WRITE, sensitivity=Sensitivity.HIGH),
        )
    )

    assert result.risk_class is RiskClass.PRIVILEGED
    assert "SELF_ADMIN_ESCALATED" not in result.reasons


def test_admin_for_someone_else_is_not_escalated() -> None:
    result = classify(
        make_snapshot(
            role_in_department_template=True,
            role=make_role(privilege=Privilege.ADMIN, sensitivity=Sensitivity.LOW),
        )
    )

    assert result.risk_class is RiskClass.PRIVILEGED
    assert result.reasons == ("IN_TEMPLATE", "PRIVILEGE_ADMIN")


def test_self_requested_admin_never_auto() -> None:
    result = classify(
        make_snapshot(
            requester_user_id=TARGET_ID,
            role_in_department_template=True,
            role=make_role(privilege=Privilege.ADMIN, sensitivity=Sensitivity.MEDIUM),
        )
    )

    assert result.decision is not Decision.AUTO


# --------------------------------------------------------------------------
# leaver
# --------------------------------------------------------------------------

LEAVER_CASES: list[tuple[str, dict[str, object], RiskClass]] = [
    ("leaver-would-be-low", _low(), RiskClass.LOW),
    ("leaver-would-be-elevated", _elevated(), RiskClass.ELEVATED),
    ("leaver-would-be-privileged", _privileged(), RiskClass.PRIVILEGED),
    ("leaver-would-be-critical", _critical(SystemCategory.PROD_INFRA), RiskClass.CRITICAL),
]


@pytest.mark.parametrize(
    ("kwargs", "expected_class"),
    [case[1:] for case in LEAVER_CASES],
    ids=[case[0] for case in LEAVER_CASES],
)
def test_leaver_is_blocked_whatever_the_class(
    kwargs: dict[str, object], expected_class: RiskClass
) -> None:
    result = classify(make_snapshot(**kwargs, target=make_target(status="leaver")))

    assert result.risk_class == expected_class
    assert result.decision is Decision.BLOCKED
    assert result.required_approvers == ()
    assert "TARGET_LEAVER" in result.reasons


def test_leaver_reason_order() -> None:
    result = classify(
        make_snapshot(
            role_in_department_template=True,
            role=make_role(sensitivity=Sensitivity.CRITICAL),
            target=make_target(status="leaver"),
        )
    )

    assert result.reasons == ("IN_TEMPLATE", "CRITICAL_SYSTEM", "TARGET_LEAVER")


def test_leaver_with_sod_conflict_carries_both_reasons_and_both_effects() -> None:
    rule = make_rule()
    result = classify(
        make_snapshot(
            **_elevated(),
            target=make_target(status="leaver", active_grant_role_ids=frozenset({rule.role_b_id})),
            sod_rules=(rule,),
        )
    )

    assert result.decision is Decision.BLOCKED
    assert result.required_approvers == ()
    assert result.reasons == (
        "NOT_IN_TEMPLATE",
        "TARGET_LEAVER",
        sod_conflict_reason(rule.rule_id),
    )
    assert len(result.sod_conflicts) == 1


# --------------------------------------------------------------------------
# policy_refs output
# --------------------------------------------------------------------------


def test_policy_refs_for_an_auto_decision_cite_the_risk_clause_only() -> None:
    result = classify(make_snapshot(**_low(), policy_refs=ALL_REFS))

    assert result.policy_refs == (REF_RISK,)


def test_policy_refs_for_an_approval_add_the_approvals_clause() -> None:
    result = classify(make_snapshot(**_elevated(), policy_refs=ALL_REFS))

    assert result.policy_refs == (REF_RISK, REF_APPROVALS)


def test_policy_refs_add_the_expiry_clause_only_when_capped() -> None:
    capped = classify(make_snapshot(**_elevated(), requested_expiry_days=365, policy_refs=ALL_REFS))
    uncapped = classify(
        make_snapshot(**_elevated(), requested_expiry_days=30, policy_refs=ALL_REFS)
    )

    assert capped.policy_refs == (REF_RISK, REF_APPROVALS, REF_EXPIRY)
    assert uncapped.policy_refs == (REF_RISK, REF_APPROVALS)


def test_policy_refs_for_a_blocked_decision() -> None:
    rule = make_rule()
    result = classify(
        make_snapshot(
            **_low(),
            target=make_target(active_grant_role_ids=frozenset({rule.role_b_id})),
            sod_rules=(rule,),
            policy_refs=ALL_REFS,
        )
    )

    assert result.policy_refs == (REF_RISK, REF_BLOCKED, RULE_REF)


def test_policy_refs_omit_clauses_the_snapshot_left_unset() -> None:
    result = classify(make_snapshot(**_elevated(), policy_refs=PolicyRefs(approvals=REF_APPROVALS)))

    assert result.policy_refs == (REF_APPROVALS,)


def test_policy_refs_are_deduplicated_in_order() -> None:
    shared = PolicyRefs(risk_class=REF_RISK, approvals=REF_RISK, expiry=REF_RISK)
    result = classify(make_snapshot(**_elevated(), requested_expiry_days=365, policy_refs=shared))

    assert result.policy_refs == (REF_RISK,)


def test_empty_policy_refs_snapshot_yields_no_refs() -> None:
    assert classify(make_snapshot(**_elevated())).policy_refs == ()


# --------------------------------------------------------------------------
# reasons are machine codes only
# --------------------------------------------------------------------------


def _classification(reasons: tuple[str, ...]) -> Classification:
    return Classification(
        risk_class=RiskClass.LOW,
        decision=Decision.AUTO,
        reasons=reasons,
        required_approvers=(),
        effective_expiry_days=30,
        sod_conflicts=(),
        policy_refs=(),
    )


def test_classification_rejects_free_text_reasons() -> None:
    with pytest.raises(ValidationError):
        _classification(("because I said so",))


def test_classification_rejects_bare_sod_conflict_code() -> None:
    with pytest.raises(ValidationError):
        _classification(("SOD_CONFLICT",))


def test_classification_rejects_a_sod_conflict_reason_with_a_malformed_id() -> None:
    """The prefix alone is not enough: the payload must be a real rule id."""
    with pytest.raises(ValidationError):
        _classification(("SOD_CONFLICT:not-a-uuid",))


def test_classification_rejects_a_non_canonical_rule_id() -> None:
    """Undashed hex parses as a uuid but is a second spelling of one reason."""
    with pytest.raises(ValidationError):
        _classification((f"SOD_CONFLICT:{uuid.uuid4().hex}",))


def test_classification_accepts_a_sod_conflict_reason_with_an_id() -> None:
    rule_id = uuid.uuid4()
    reason = sod_conflict_reason(rule_id)

    assert _classification((reason,)).reasons == (f"SOD_CONFLICT:{rule_id}",)


def test_engine_only_ever_emits_valid_reason_codes() -> None:
    for kwargs in (_low(), _elevated(), _privileged(), _critical(SystemCategory.HRIS)):
        result = classify(make_snapshot(**kwargs, requested_expiry_days=999))
        assert result.reasons
        assert all(is_valid_reason(reason) for reason in result.reasons)
