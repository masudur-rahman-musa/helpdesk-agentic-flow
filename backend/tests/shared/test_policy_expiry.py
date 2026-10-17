"""Effective expiry.

effective_expiry_days = min(requested if given, role.default_expiry_days,
class cap) using the FINAL risk class. Caps: low 365, elevated 180,
privileged 90, critical 30. Admin privilege is additionally capped at 30
whatever the class.

EXPIRY_CAPPED is emitted only when a requested value was given and the
effective value came out lower.
"""

import pytest

from helpdesk.shared.policy.engine import classify
from helpdesk.shared.policy.types import Decision, Privilege, RiskClass, Sensitivity
from tests.support.policy import make_role, make_snapshot, make_target

CLASS_CAPS: dict[RiskClass, int] = {
    RiskClass.LOW: 365,
    RiskClass.ELEVATED: 180,
    RiskClass.PRIVILEGED: 90,
    RiskClass.CRITICAL: 30,
}


def _low(**over: object) -> dict[str, object]:
    return {
        "role_in_department_template": True,
        "role": make_role(sensitivity=Sensitivity.LOW, **over),
    }


def _elevated(**over: object) -> dict[str, object]:
    return {
        "role_in_department_template": False,
        "role": make_role(sensitivity=Sensitivity.LOW, **over),
    }


def _privileged(**over: object) -> dict[str, object]:
    return {
        "role_in_department_template": False,
        "role": make_role(sensitivity=Sensitivity.HIGH, **over),
    }


def _critical(**over: object) -> dict[str, object]:
    return {
        "role_in_department_template": True,
        "role": make_role(sensitivity=Sensitivity.CRITICAL, **over),
    }


# id, snapshot kwargs, requested, expected effective, EXPIRY_CAPPED expected
EXPIRY_CASES: list[tuple[str, dict[str, object], int | None, int, bool]] = [
    # --- requested is the smallest bound
    ("requested-wins", _low(default_expiry_days=365), 10, 10, False),
    ("requested-wins-under-class-cap", _critical(default_expiry_days=365), 7, 7, False),
    # --- the role default is the smallest bound
    ("role-default-wins", _low(default_expiry_days=60), 300, 60, True),
    ("role-default-wins-no-request", _low(default_expiry_days=45), None, 45, False),
    # --- the class cap is the smallest bound, once per class
    ("class-cap-low-365", _low(default_expiry_days=3650), 3650, 365, True),
    ("class-cap-elevated-180", _elevated(default_expiry_days=365), 365, 180, True),
    ("class-cap-privileged-90", _privileged(default_expiry_days=365), 365, 90, True),
    ("class-cap-critical-30", _critical(default_expiry_days=365), 365, 30, True),
    # --- the admin 30-day cap beats the privileged class cap of 90
    (
        "admin-cap-beats-class-cap",
        _low(privilege=Privilege.ADMIN, default_expiry_days=365),
        90,
        30,
        True,
    ),
    (
        "admin-cap-with-no-request",
        _low(privilege=Privilege.ADMIN, default_expiry_days=365),
        None,
        30,
        False,
    ),
    # --- boundaries: exactly at the cap is not a cap event
    ("requested-equals-effective", _low(default_expiry_days=365), 30, 30, False),
    ("requested-equals-class-cap", _elevated(default_expiry_days=365), 180, 180, False),
    ("requested-one-over-class-cap", _elevated(default_expiry_days=365), 181, 180, True),
    ("requested-equals-role-default", _low(default_expiry_days=60), 60, 60, False),
    # --- no requested value is never a cap event, however low the result
    ("no-request-critical", _critical(default_expiry_days=365), None, 30, False),
    ("no-request-privileged", _privileged(default_expiry_days=365), None, 90, False),
]


@pytest.mark.parametrize(
    ("kwargs", "requested", "expected_days", "expect_capped"),
    [case[1:] for case in EXPIRY_CASES],
    ids=[case[0] for case in EXPIRY_CASES],
)
def test_effective_expiry(
    kwargs: dict[str, object],
    requested: int | None,
    expected_days: int,
    expect_capped: bool,
) -> None:
    result = classify(make_snapshot(**kwargs, requested_expiry_days=requested))

    assert result.effective_expiry_days == expected_days
    assert ("EXPIRY_CAPPED" in result.reasons) is expect_capped


@pytest.mark.parametrize(
    ("kwargs", "expected_class"),
    [
        (_low(default_expiry_days=3650), RiskClass.LOW),
        (_elevated(default_expiry_days=3650), RiskClass.ELEVATED),
        (_privileged(default_expiry_days=3650), RiskClass.PRIVILEGED),
        (_critical(default_expiry_days=3650), RiskClass.CRITICAL),
    ],
    ids=["low", "elevated", "privileged", "critical"],
)
def test_class_cap_matches_the_final_class(
    kwargs: dict[str, object], expected_class: RiskClass
) -> None:
    result = classify(make_snapshot(**kwargs, requested_expiry_days=3650))

    assert result.risk_class == expected_class
    assert result.effective_expiry_days == CLASS_CAPS[expected_class]


def test_cap_uses_the_class_after_the_new_joiner_bump() -> None:
    """A new joiner is elevated, so the cap is 180 rather than the low cap of 365."""
    result = classify(
        make_snapshot(
            **_low(default_expiry_days=3650),
            target=make_target(tenure_days=1),
            requested_expiry_days=3650,
        )
    )

    assert result.risk_class is RiskClass.ELEVATED
    assert result.effective_expiry_days == 180
    assert "NEW_JOINER" in result.reasons
    assert "EXPIRY_CAPPED" in result.reasons


def test_cap_uses_the_class_after_the_self_admin_escalation() -> None:
    """Escalated to critical; the admin cap of 30 and the critical cap agree."""
    result = classify(
        make_snapshot(
            **_low(privilege=Privilege.ADMIN, default_expiry_days=365),
            requester_user_id=make_target().user_id,
            requested_expiry_days=365,
        )
    )

    assert result.risk_class is RiskClass.CRITICAL
    assert result.effective_expiry_days == 30


def test_blocked_decision_still_computes_an_expiry() -> None:
    result = classify(
        make_snapshot(
            **_low(default_expiry_days=365),
            target=make_target(status="leaver"),
            requested_expiry_days=500,
        )
    )

    assert result.decision is Decision.BLOCKED
    assert result.effective_expiry_days == 365


def test_expiry_capped_precedes_target_leaver_in_the_reason_order() -> None:
    result = classify(
        make_snapshot(
            **_low(default_expiry_days=365),
            target=make_target(status="leaver"),
            requested_expiry_days=500,
        )
    )

    assert result.reasons == ("IN_TEMPLATE", "EXPIRY_CAPPED", "TARGET_LEAVER")


def test_expiry_capped_is_emitted_at_most_once() -> None:
    result = classify(
        make_snapshot(**_critical(default_expiry_days=10), requested_expiry_days=3650)
    )

    assert result.effective_expiry_days == 10
    assert [r for r in result.reasons if r == "EXPIRY_CAPPED"] == ["EXPIRY_CAPPED"]
