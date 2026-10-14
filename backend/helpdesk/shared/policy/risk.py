"""Risk class and the facts behind it (the design).

Every §9.2 row is an independent predicate and the highest matching class
wins. Ordered first-match would make table order
load-bearing and would hide the `admin` and `critical` rows behind the
template rows above them.

`low` is always a candidate, so the maximum is never taken over an empty
set — the function is total without an unreachable fallback branch.
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
"""Below this many days of tenure, a `low` request is lifted (the design)."""

_LOWER_SENSITIVITY = frozenset({Sensitivity.LOW, Sensitivity.MEDIUM})


def base_risk_class(snapshot: RequestSnapshot) -> RiskClass:
    """The §9.2 table without the tenure bump."""
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
    """Class plus the §9.6 facts that produced it, in canonical order.

    The tenure bump is applied last and only lifts `low`; a new joiner whose
    request was already `elevated` or higher gets no `NEW_JOINER` reason,
    because nothing was lifted.
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
