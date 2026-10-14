"""Deterministic policy engine (the design, an earlier change).

Pure by construction: no I/O, no clock, no LLM, and no import of `models`,
`core` or any service. `engine.classify` takes a snapshot the caller has
already loaded and returns a `Classification`.
"""

from helpdesk.shared.policy.engine import classify
from helpdesk.shared.policy.types import (
    ApproverSlot,
    Classification,
    Decision,
    PolicyRefs,
    Privilege,
    ReasonCode,
    RequestSnapshot,
    RequiredApprover,
    RiskClass,
    RoleSnapshot,
    Sensitivity,
    SodConflict,
    SodConflictSource,
    SodRuleSnapshot,
    SystemCategory,
    TargetSnapshot,
    TargetStatus,
    sod_conflict_reason,
)

__all__ = [
    "ApproverSlot",
    "Classification",
    "Decision",
    "PolicyRefs",
    "Privilege",
    "ReasonCode",
    "RequestSnapshot",
    "RequiredApprover",
    "RiskClass",
    "RoleSnapshot",
    "Sensitivity",
    "SodConflict",
    "SodConflictSource",
    "SodRuleSnapshot",
    "SystemCategory",
    "TargetSnapshot",
    "TargetStatus",
    "classify",
    "sod_conflict_reason",
]
