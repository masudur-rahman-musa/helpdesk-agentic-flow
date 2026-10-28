"""Input snapshot and output shape for the policy engine.

Every type is frozen. `system_category` is passed in rather than derived from
a system slug, because slugs are tenant data the code must not hard-code.
"""

import uuid
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Sensitivity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Privilege(StrEnum):
    READ = "read"
    WRITE = "write"
    ADMIN = "admin"


class SystemCategory(StrEnum):
    """Which extra approver a `critical` request needs."""

    PROD_INFRA = "prod_infra"
    HRIS = "hris"
    OTHER = "other"


class TargetStatus(StrEnum):
    ACTIVE = "active"
    LEAVER = "leaver"


class RiskClass(StrEnum):
    LOW = "low"
    ELEVATED = "elevated"
    PRIVILEGED = "privileged"
    CRITICAL = "critical"


_SEVERITY: dict[RiskClass, int] = {
    RiskClass.LOW: 0,
    RiskClass.ELEVATED: 1,
    RiskClass.PRIVILEGED: 2,
    RiskClass.CRITICAL: 3,
}


def severity(risk_class: RiskClass) -> int:
    """Rank for comparing classes. Table rows overlap and the highest wins."""
    return _SEVERITY[risk_class]


class Decision(StrEnum):
    AUTO = "auto"
    APPROVAL = "approval"
    BLOCKED = "blocked"


class ReasonCode(StrEnum):
    """Machine reason codes. Reasons are never free text."""

    IN_TEMPLATE = "IN_TEMPLATE"
    NOT_IN_TEMPLATE = "NOT_IN_TEMPLATE"
    SENSITIVITY_HIGH = "SENSITIVITY_HIGH"
    PRIVILEGE_ADMIN = "PRIVILEGE_ADMIN"
    CRITICAL_SYSTEM = "CRITICAL_SYSTEM"
    NEW_JOINER = "NEW_JOINER"
    SOD_CONFLICT = "SOD_CONFLICT"
    EXPIRY_CAPPED = "EXPIRY_CAPPED"
    SELF_ADMIN_ESCALATED = "SELF_ADMIN_ESCALATED"
    TARGET_LEAVER = "TARGET_LEAVER"


def sod_conflict_reason(rule_id: uuid.UUID) -> str:
    """`SOD_CONFLICT:{rule_id}` — the only reason code that carries a value."""
    return f"{ReasonCode.SOD_CONFLICT.value}:{rule_id}"


def is_valid_reason(reason: str) -> bool:
    """True for a bare code, or `SOD_CONFLICT:` plus a dashed lowercase id.

    Other uuid spellings are refused so one reason has only one spelling.
    """
    prefix = f"{ReasonCode.SOD_CONFLICT.value}:"
    if reason.startswith(prefix):
        payload = reason.removeprefix(prefix)
        try:
            return str(uuid.UUID(payload)) == payload
        except ValueError:
            return False
    return reason in set(ReasonCode) and reason != ReasonCode.SOD_CONFLICT


class ApproverSlot(StrEnum):
    """Who must approve. A slot, not a person: the pure engine cannot look up
    "an it_admin", so the caller fills unresolved ids."""

    DEPARTMENT_MANAGER = "department_manager"
    SYSTEM_OWNER = "system_owner"
    IT_ADMIN = "it_admin"
    HR = "hr"


class RequiredApprover(BaseModel):
    model_config = ConfigDict(frozen=True)

    slot: ApproverSlot
    user_id: uuid.UUID | None = None
    """None for group slots and for seats nobody holds."""


class SodConflictSource(StrEnum):
    ACTIVE_GRANT = "active_grant"
    APPROVED_REQUEST = "approved_request"


class SodConflict(BaseModel):
    model_config = ConfigDict(frozen=True)

    rule_id: uuid.UUID
    conflicting_role_id: uuid.UUID
    policy_ref: str
    source: SodConflictSource


class PolicyRefs(BaseModel):
    """Where each clause of the tenant's written policy lives.

    The caller supplies them, since they are tenant document ids. None means
    not cited.
    """

    model_config = ConfigDict(frozen=True)

    risk_class: str | None = None
    approvals: str | None = None
    expiry: str | None = None
    blocked: str | None = None


class SodRuleSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    rule_id: uuid.UUID
    role_a_id: uuid.UUID
    role_b_id: uuid.UUID
    policy_ref: str


class RoleSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    role_id: uuid.UUID
    privilege: Privilege
    default_expiry_days: int = Field(gt=0)
    sensitivity: Sensitivity
    system_category: SystemCategory
    system_owner_user_id: uuid.UUID | None = None


class TargetSnapshot(BaseModel):
    """The person the access is for."""

    model_config = ConfigDict(frozen=True)

    user_id: uuid.UUID
    department_manager_user_id: uuid.UUID | None = None
    status: TargetStatus
    tenure_days: int = Field(ge=0)
    active_grant_role_ids: frozenset[uuid.UUID] = frozenset()
    approved_pending_role_ids: frozenset[uuid.UUID] = frozenset()
    """Roles approved but not yet granted. These count for conflicts too."""


class RequestSnapshot(BaseModel):
    """Everything `classify` is allowed to see."""

    model_config = ConfigDict(frozen=True)

    requester_user_id: uuid.UUID
    target: TargetSnapshot
    role: RoleSnapshot
    role_in_department_template: bool
    requested_expiry_days: int | None = Field(default=None, gt=0)
    sod_rules: tuple[SodRuleSnapshot, ...] = ()
    policy_refs: PolicyRefs = PolicyRefs()

    @property
    def is_self_request(self) -> bool:
        return self.requester_user_id == self.target.user_id


class Classification(BaseModel):
    """The engine's whole output. Every field is filled even when blocked."""

    model_config = ConfigDict(frozen=True)

    risk_class: RiskClass
    decision: Decision
    reasons: tuple[str, ...]
    required_approvers: tuple[RequiredApprover, ...]
    effective_expiry_days: int
    sod_conflicts: tuple[SodConflict, ...]
    policy_refs: tuple[str, ...]

    @field_validator("reasons")
    @classmethod
    def _reasons_are_machine_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        bad = [reason for reason in value if not is_valid_reason(reason)]
        if bad:
            raise ValueError(f"not valid reason codes: {bad}")
        return value
