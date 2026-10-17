"""Effective expiry: the smallest of the ask, the role default and the cap
for the final risk class (after any escalation).

An over-ask is not an error. It goes ahead at the cap and records
`EXPIRY_CAPPED`.
"""

from helpdesk.shared.policy.types import Privilege, RequestSnapshot, RiskClass

CLASS_CAP_DAYS: dict[RiskClass, int] = {
    RiskClass.LOW: 365,
    RiskClass.ELEVATED: 180,
    RiskClass.PRIVILEGED: 90,
    RiskClass.CRITICAL: 30,
}

ADMIN_CAP_DAYS = 30
"""Admin privilege is capped here whatever the class."""


def effective_expiry(snapshot: RequestSnapshot, risk_class: RiskClass) -> tuple[int, bool]:
    """Return the effective days and whether the requested value was cut."""
    bounds = [snapshot.role.default_expiry_days, CLASS_CAP_DAYS[risk_class]]
    if snapshot.role.privilege is Privilege.ADMIN:
        bounds.append(ADMIN_CAP_DAYS)
    requested = snapshot.requested_expiry_days
    if requested is not None:
        bounds.append(requested)

    effective = min(bounds)
    capped = requested is not None and requested > effective
    return effective, capped
