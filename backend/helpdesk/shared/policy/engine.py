"""`classify`: the whole policy engine in one pass.

Order is fixed: risk class, self-admin bump, expiry cap, approver chain, and
only then the two overrides that block. Blocking last means a refusal still
shows the class and expiry it would have had. Reasons come out in a fixed
order so callers can compare tuples, not sets.
"""

from helpdesk.shared.policy import risk as risk_module
from helpdesk.shared.policy.decision import decide, escalate_self_admin, required_approvers
from helpdesk.shared.policy.expiry import effective_expiry
from helpdesk.shared.policy.sod import find_conflicts
from helpdesk.shared.policy.types import (
    Classification,
    Decision,
    PolicyRefs,
    ReasonCode,
    RequestSnapshot,
    RequiredApprover,
    SodConflict,
    TargetStatus,
    sod_conflict_reason,
)


def _policy_refs(
    refs: PolicyRefs,
    decision: Decision,
    capped: bool,
    conflicts: tuple[SodConflict, ...],
) -> tuple[str, ...]:
    """The clauses this classification actually applied, deduplicated."""
    candidates = [refs.risk_class]
    if decision is Decision.APPROVAL:
        candidates.append(refs.approvals)
    if capped:
        candidates.append(refs.expiry)
    if decision is Decision.BLOCKED:
        candidates.append(refs.blocked)
    candidates.extend(conflict.policy_ref for conflict in conflicts)

    cited: list[str] = []
    for ref in candidates:
        if ref is not None and ref not in cited:
            cited.append(ref)
    return tuple(cited)


def classify(snapshot: RequestSnapshot) -> Classification:
    """Classify one access request. Deterministic, side-effect free, no LLM."""
    risk_class, reasons = risk_module.risk_class(snapshot)

    escalated = escalate_self_admin(snapshot, risk_class)
    reason_codes = list(reasons)
    if escalated is not risk_class:
        reason_codes.append(ReasonCode.SELF_ADMIN_ESCALATED)
    risk_class = escalated

    expiry_days, capped = effective_expiry(snapshot, risk_class)
    if capped:
        reason_codes.append(ReasonCode.EXPIRY_CAPPED)

    decision = decide(risk_class)
    approvers: tuple[RequiredApprover, ...] = required_approvers(snapshot, risk_class)

    if snapshot.target.status is TargetStatus.LEAVER:
        decision = Decision.BLOCKED
        approvers = ()
        reason_codes.append(ReasonCode.TARGET_LEAVER)

    conflicts = find_conflicts(snapshot)
    if conflicts:
        decision = Decision.BLOCKED
        approvers = ()

    return Classification(
        risk_class=risk_class,
        decision=decision,
        reasons=(
            *(code.value for code in reason_codes),
            *(sod_conflict_reason(conflict.rule_id) for conflict in conflicts),
        ),
        required_approvers=approvers,
        effective_expiry_days=expiry_days,
        sod_conflicts=conflicts,
        policy_refs=_policy_refs(snapshot.policy_refs, decision, capped, conflicts),
    )
