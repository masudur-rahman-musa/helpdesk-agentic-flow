"""Deterministic policy engine.

Pure: no I/O, no clock, no LLM, no service imports. The caller loads a
snapshot and `classify` returns a `Classification`, so the rules are easy to
test and replay.
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
