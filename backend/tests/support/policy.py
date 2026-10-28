"""Snapshot builders for the policy-engine tests.

Fixed uuids, keyword overrides, and one default snapshot that classifies
`low` / `auto` so each case only states the fields it is about.
"""

import uuid
from typing import Any

from helpdesk.shared.policy.types import (
    PolicyRefs,
    Privilege,
    RequestSnapshot,
    RoleSnapshot,
    Sensitivity,
    SodRuleSnapshot,
    SystemCategory,
    TargetSnapshot,
    TargetStatus,
)

REQUESTER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
TARGET_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a2")
MANAGER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a3")
SYSTEM_OWNER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a4")

ROLE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b1")
PAIRED_ROLE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b2")
OTHER_ROLE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b3")
UNRELATED_ROLE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b4")

RULE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c1")
RULE_2_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c2")

RULE_REF = "NL-POL-009 section 2"
RULE_2_REF = "NL-POL-009 section 3"

REF_RISK = "NL-POL-001 section 1"
REF_APPROVALS = "NL-POL-002 section 2"
REF_EXPIRY = "NL-POL-003 section 3"
REF_BLOCKED = "NL-POL-004 section 4"

ALL_REFS = PolicyRefs(
    risk_class=REF_RISK,
    approvals=REF_APPROVALS,
    expiry=REF_EXPIRY,
    blocked=REF_BLOCKED,
)


def make_role(**overrides: Any) -> RoleSnapshot:
    defaults: dict[str, Any] = {
        "role_id": ROLE_ID,
        "privilege": Privilege.READ,
        "default_expiry_days": 365,
        "sensitivity": Sensitivity.LOW,
        "system_category": SystemCategory.OTHER,
        "system_owner_user_id": SYSTEM_OWNER_ID,
    }
    return RoleSnapshot(**{**defaults, **overrides})


def make_target(**overrides: Any) -> TargetSnapshot:
    defaults: dict[str, Any] = {
        "user_id": TARGET_ID,
        "department_manager_user_id": MANAGER_ID,
        "status": TargetStatus.ACTIVE,
        "tenure_days": 400,
    }
    return TargetSnapshot(**{**defaults, **overrides})


def make_rule(
    rule_id: uuid.UUID = RULE_ID,
    role_a_id: uuid.UUID = ROLE_ID,
    role_b_id: uuid.UUID = PAIRED_ROLE_ID,
    policy_ref: str = RULE_REF,
) -> SodRuleSnapshot:
    return SodRuleSnapshot(
        rule_id=rule_id,
        role_a_id=role_a_id,
        role_b_id=role_b_id,
        policy_ref=policy_ref,
    )


def make_snapshot(**overrides: Any) -> RequestSnapshot:
    """Default: in-template, low-sensitivity read for a tenured active target.

    Everything, `role` and `target` included, is a keyword override, so a case
    can splat a shared dict of them (`**_privileged()`) without mypy losing
    the argument types.
    """
    defaults: dict[str, Any] = {
        "requester_user_id": REQUESTER_ID,
        "target": make_target(),
        "role": make_role(),
        "role_in_department_template": True,
        "requested_expiry_days": None,
        "sod_rules": (),
        "policy_refs": PolicyRefs(),
    }
    return RequestSnapshot(**{**defaults, **overrides})
