"""Risk class table, the design.

Expected implementation surface (the implementer must provide exactly this):

``helpdesk.policy.engine.classify(snapshot: RequestSnapshot) -> Classification``
also re-exported as ``helpdesk.policy.classify``.

Every §9.2 row is an independent predicate and the highest matching severity
wins (plan decision A), so the overlap rows are pinned explicitly here: an
in-template ``admin`` role on a low-sensitivity system is ``privileged``, an
in-template ``read`` role on a ``critical`` system is ``critical``.

The tenure bump is applied last and only lifts ``low``; a new joiner whose
class is already ``elevated`` or higher gets no ``NEW_JOINER`` reason.

Reason order (the design, canonical): IN_TEMPLATE | NOT_IN_TEMPLATE,
SENSITIVITY_HIGH, CRITICAL_SYSTEM, PRIVILEGE_ADMIN, NEW_JOINER,
SELF_ADMIN_ESCALATED, EXPIRY_CAPPED, TARGET_LEAVER, SOD_CONFLICT:{rule_id}.
"""

import pytest
from _policy import make_role, make_snapshot, make_target

from helpdesk.shared.policy.engine import classify
from helpdesk.shared.policy.types import Privilege, RiskClass, Sensitivity

# id, in_template, sensitivity, privilege, tenure_days, expected class, expected reasons
RISK_ROWS: list[tuple[str, bool, Sensitivity, Privilege, int, RiskClass, tuple[str, ...]]] = [
    # --- §9.2 row 1: in template, sensitivity low/medium -> low
    (
        "in_template-low-read",
        True,
        Sensitivity.LOW,
        Privilege.READ,
        400,
        RiskClass.LOW,
        ("IN_TEMPLATE",),
    ),
    (
        "in_template-medium-write",
        True,
        Sensitivity.MEDIUM,
        Privilege.WRITE,
        400,
        RiskClass.LOW,
        ("IN_TEMPLATE",),
    ),
    # --- §9.2 row 2: in template, high, read -> low
    (
        "in_template-high-read",
        True,
        Sensitivity.HIGH,
        Privilege.READ,
        400,
        RiskClass.LOW,
        ("IN_TEMPLATE", "SENSITIVITY_HIGH"),
    ),
    # --- §9.2 row 3: in template, high, write -> elevated
    (
        "in_template-high-write",
        True,
        Sensitivity.HIGH,
        Privilege.WRITE,
        400,
        RiskClass.ELEVATED,
        ("IN_TEMPLATE", "SENSITIVITY_HIGH"),
    ),
    # --- §9.2 row 4: not in template, sensitivity low/medium -> elevated
    (
        "not_in_template-low-read",
        False,
        Sensitivity.LOW,
        Privilege.READ,
        400,
        RiskClass.ELEVATED,
        ("NOT_IN_TEMPLATE",),
    ),
    (
        "not_in_template-medium-write",
        False,
        Sensitivity.MEDIUM,
        Privilege.WRITE,
        400,
        RiskClass.ELEVATED,
        ("NOT_IN_TEMPLATE",),
    ),
    # --- §9.2 row 5: not in template, high -> privileged
    (
        "not_in_template-high-read",
        False,
        Sensitivity.HIGH,
        Privilege.READ,
        400,
        RiskClass.PRIVILEGED,
        ("NOT_IN_TEMPLATE", "SENSITIVITY_HIGH"),
    ),
    # --- §9.2 row 6: privilege admin -> at least privileged (overlap cases)
    (
        "overlap-in_template-low-admin",
        True,
        Sensitivity.LOW,
        Privilege.ADMIN,
        400,
        RiskClass.PRIVILEGED,
        ("IN_TEMPLATE", "PRIVILEGE_ADMIN"),
    ),
    (
        "overlap-not_in_template-high-admin",
        False,
        Sensitivity.HIGH,
        Privilege.ADMIN,
        400,
        RiskClass.PRIVILEGED,
        ("NOT_IN_TEMPLATE", "SENSITIVITY_HIGH", "PRIVILEGE_ADMIN"),
    ),
    # --- §9.2 row 7: critical system -> critical (overlap cases)
    (
        "overlap-in_template-critical-read",
        True,
        Sensitivity.CRITICAL,
        Privilege.READ,
        400,
        RiskClass.CRITICAL,
        ("IN_TEMPLATE", "CRITICAL_SYSTEM"),
    ),
    (
        "overlap-not_in_template-critical-admin",
        False,
        Sensitivity.CRITICAL,
        Privilege.ADMIN,
        400,
        RiskClass.CRITICAL,
        ("NOT_IN_TEMPLATE", "CRITICAL_SYSTEM", "PRIVILEGE_ADMIN"),
    ),
    # --- §9.2 row 8: tenure < 30 lifts low to elevated
    (
        "new_joiner-day-0",
        True,
        Sensitivity.LOW,
        Privilege.READ,
        0,
        RiskClass.ELEVATED,
        ("IN_TEMPLATE", "NEW_JOINER"),
    ),
    (
        "new_joiner-boundary-29",
        True,
        Sensitivity.LOW,
        Privilege.READ,
        29,
        RiskClass.ELEVATED,
        ("IN_TEMPLATE", "NEW_JOINER"),
    ),
    (
        "tenured-boundary-30",
        True,
        Sensitivity.LOW,
        Privilege.READ,
        30,
        RiskClass.LOW,
        ("IN_TEMPLATE",),
    ),
    (
        "new_joiner-lifts-in_template-high-read",
        True,
        Sensitivity.HIGH,
        Privilege.READ,
        5,
        RiskClass.ELEVATED,
        ("IN_TEMPLATE", "SENSITIVITY_HIGH", "NEW_JOINER"),
    ),
    # --- the bump must NOT lift anything already above low, and must not
    #     emit NEW_JOINER when it did not apply.
    (
        "new_joiner-already-elevated",
        True,
        Sensitivity.HIGH,
        Privilege.WRITE,
        5,
        RiskClass.ELEVATED,
        ("IN_TEMPLATE", "SENSITIVITY_HIGH"),
    ),
    (
        "new_joiner-already-elevated-not_in_template",
        False,
        Sensitivity.LOW,
        Privilege.READ,
        5,
        RiskClass.ELEVATED,
        ("NOT_IN_TEMPLATE",),
    ),
    (
        "new_joiner-already-privileged",
        False,
        Sensitivity.HIGH,
        Privilege.READ,
        5,
        RiskClass.PRIVILEGED,
        ("NOT_IN_TEMPLATE", "SENSITIVITY_HIGH"),
    ),
    (
        "new_joiner-already-critical",
        True,
        Sensitivity.CRITICAL,
        Privilege.READ,
        5,
        RiskClass.CRITICAL,
        ("IN_TEMPLATE", "CRITICAL_SYSTEM"),
    ),
]


@pytest.mark.parametrize(
    (
        "in_template",
        "sensitivity",
        "privilege",
        "tenure_days",
        "expected_class",
        "expected_reasons",
    ),
    [row[1:] for row in RISK_ROWS],
    ids=[row[0] for row in RISK_ROWS],
)
def test_risk_class_table(
    in_template: bool,
    sensitivity: Sensitivity,
    privilege: Privilege,
    tenure_days: int,
    expected_class: RiskClass,
    expected_reasons: tuple[str, ...],
) -> None:
    snapshot = make_snapshot(
        role=make_role(privilege=privilege, sensitivity=sensitivity),
        target=make_target(tenure_days=tenure_days),
        role_in_department_template=in_template,
    )

    result = classify(snapshot)

    assert result.risk_class == expected_class
    assert result.reasons == expected_reasons


@pytest.mark.parametrize("in_template", [True, False])
@pytest.mark.parametrize(
    "sensitivity",
    [Sensitivity.LOW, Sensitivity.MEDIUM, Sensitivity.HIGH, Sensitivity.CRITICAL],
)
def test_admin_privilege_is_never_below_privileged(
    in_template: bool, sensitivity: Sensitivity
) -> None:
    """§9.2 'any role with privilege = admin' holds across the whole table."""
    snapshot = make_snapshot(
        role=make_role(privilege=Privilege.ADMIN, sensitivity=sensitivity),
        role_in_department_template=in_template,
    )

    result = classify(snapshot)

    assert result.risk_class in {RiskClass.PRIVILEGED, RiskClass.CRITICAL}
    assert "PRIVILEGE_ADMIN" in result.reasons


@pytest.mark.parametrize("in_template", [True, False])
@pytest.mark.parametrize("privilege", [Privilege.READ, Privilege.WRITE, Privilege.ADMIN])
def test_critical_sensitivity_is_always_critical(in_template: bool, privilege: Privilege) -> None:
    """§9.2 'any role on a critical system' beats every other row."""
    snapshot = make_snapshot(
        role=make_role(privilege=privilege, sensitivity=Sensitivity.CRITICAL),
        role_in_department_template=in_template,
    )

    result = classify(snapshot)

    assert result.risk_class == RiskClass.CRITICAL
    assert "CRITICAL_SYSTEM" in result.reasons


def test_exactly_one_template_reason_is_always_emitted() -> None:
    for in_template in (True, False):
        result = classify(make_snapshot(role_in_department_template=in_template))
        emitted = [r for r in result.reasons if r in {"IN_TEMPLATE", "NOT_IN_TEMPLATE"}]
        assert emitted == ["IN_TEMPLATE" if in_template else "NOT_IN_TEMPLATE"]
        assert result.reasons[0] == emitted[0]


def test_sensitivity_high_reason_only_for_high() -> None:
    for sensitivity in Sensitivity:
        result = classify(make_snapshot(role=make_role(sensitivity=sensitivity)))
        assert ("SENSITIVITY_HIGH" in result.reasons) is (sensitivity is Sensitivity.HIGH)
        assert ("CRITICAL_SYSTEM" in result.reasons) is (sensitivity is Sensitivity.CRITICAL)


def test_classification_has_no_side_effects_and_is_repeatable() -> None:
    snapshot = make_snapshot(
        role=make_role(privilege=Privilege.WRITE, sensitivity=Sensitivity.HIGH),
        target=make_target(tenure_days=10),
    )

    assert classify(snapshot) == classify(snapshot)
