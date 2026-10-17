"""Risk class and the facts behind it.

Each table row is checked on its own and the highest match wins. First-match
order would make the table order matter and hide the admin and critical rows
behind the template rows.
"""

from helpdesk.shared.policy.types import (
    Privilege,
    ReasonCode,
    RequestSnapshot,
    RiskClass,
    Sensitivity,
    severity,
)

NEW_JOINER_TENURE_DAYS = 30
"""Below this many days of tenure, a `low` request is lifted."""

_LOWER_SENSITIVITY = frozenset({Sensitivity.LOW, Sensitivity.MEDIUM})


def base_risk_class(snapshot: RequestSnapshot) -> RiskClass:
    """The risk table without the tenure bump."""
    in_template = snapshot.role_in_department_template
    sensitivity = snapshot.role.sensitivity
    privilege = snapshot.role.privilege

    candidates = [RiskClass.LOW]
    if in_template and sensitivity is Sensitivity.HIGH and privilege is Privilege.WRITE:
        candidates.append(RiskClass.ELEVATED)
    if not in_template and sensitivity in _LOWER_SENSITIVITY:
        candidates.append(RiskClass.ELEVATED)
    if not in_template and sensitivity is Sensitivity.HIGH:
        candidates.append(RiskClass.PRIVILEGED)
    if privilege is Privilege.ADMIN:
        candidates.append(RiskClass.PRIVILEGED)
    if sensitivity is Sensitivity.CRITICAL:
        candidates.append(RiskClass.CRITICAL)
    return max(candidates, key=severity)


def is_new_joiner(snapshot: RequestSnapshot) -> bool:
    return snapshot.target.tenure_days < NEW_JOINER_TENURE_DAYS


def risk_class(snapshot: RequestSnapshot) -> tuple[RiskClass, tuple[ReasonCode, ...]]:
    """Class plus the reason codes behind it, in fixed order.

    The tenure bump only lifts `low`, so `NEW_JOINER` appears only when
    something was actually lifted.
    """
    resolved = base_risk_class(snapshot)
    reasons: list[ReasonCode] = [
        ReasonCode.IN_TEMPLATE
        if snapshot.role_in_department_template
        else ReasonCode.NOT_IN_TEMPLATE
    ]
    if snapshot.role.sensitivity is Sensitivity.HIGH:
        reasons.append(ReasonCode.SENSITIVITY_HIGH)
    if snapshot.role.sensitivity is Sensitivity.CRITICAL:
        reasons.append(ReasonCode.CRITICAL_SYSTEM)
    if snapshot.role.privilege is Privilege.ADMIN:
        reasons.append(ReasonCode.PRIVILEGE_ADMIN)

    if resolved is RiskClass.LOW and is_new_joiner(snapshot):
        resolved = RiskClass.ELEVATED
        reasons.append(ReasonCode.NEW_JOINER)
    return resolved, tuple(reasons)
