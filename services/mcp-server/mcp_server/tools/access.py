"""The access read tools an earlier change registers: `classify_access_request` and
`list_my_requests`.

`classify_access_request` is pure over DB state (the design): it builds the
snapshot, runs the policy engine, and returns the whole `Classification` —
no `access_requests` row is written, and its one audit event is
`request.classified`, both pinned by
`test_mcp_access_read_tools.py::test_classify_in_template_low_role_is_auto_with_one_event_and_no_row`.
The requester is the TOKEN's user: the input names a target
and a role, never a requester.

`list_my_requests` is requester-scoped the same way: no user argument
exists, and an extra one is dropped, pinned by
`test_mcp_access_read_tools.py::test_list_my_requests_returns_only_the_token_users_rows`
with the caller and the named colleague in the same tenant.

Neither function opens a session or touches the transport: the pipeline
binds the session to the token's tenant before either is called.
"""

import uuid
from datetime import UTC, datetime

from helpdesk.access import (
    SimulatedDirectory,
    build_request_snapshot,
    list_requests_for_requester,
)
from helpdesk.models import RequestStatus
from helpdesk.policy.engine import classify
from helpdesk.policy.types import (
    Decision,
    RequiredApprover,
    RiskClass,
    SodConflict,
)
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_server.identity import Identity

CLASSIFIED_EVENT_TYPE = "request.classified"
"""Classify's own success event: the
audit trail records that a classification was asked for, not a generic
tool success."""

EXPIRY_DAYS_MAX = 3650
"""Inclusive cap on `requested_expiry_days`: the
value lands in an int4 column via row 11b, so an unbounded int would be a
post-effect database error. Pinned exactly by
`test_mcp_access_read_tools.py::test_the_requested_expiry_boundary_is_inclusive_at_3650`."""

_IGNORE_EXTRAS = ConfigDict(extra="ignore")
"""Extra arguments are dropped, never refused — same contract as
`tools/read.py`: identity arrives on the token, so an argument naming a
user is data to discard, not a reason to fail the call."""


class ClassifyAccessRequestInput(BaseModel):
    model_config = _IGNORE_EXTRAS

    target_user_id: uuid.UUID
    role_id: uuid.UUID
    requested_expiry_days: int | None = Field(default=None, gt=0, le=EXPIRY_DAYS_MAX)


class ClassifyAccessRequestOutput(BaseModel):
    """The engine's `Classification`, field for field.

    The approver and conflict rows reuse the policy types themselves: one
    shape, declared once, so the tool cannot drift from the engine.
    """

    risk_class: RiskClass
    decision: Decision
    reasons: list[str]
    required_approvers: list[RequiredApprover]
    effective_expiry_days: int
    sod_conflicts: list[SodConflict]
    policy_refs: list[str]


class ListMyRequestsInput(BaseModel):
    model_config = _IGNORE_EXTRAS

    status: RequestStatus | None = None
    """Exact the design enum membership or VALIDATION: pydantic's StrEnum
    lookup does no stripping, so `"pending "` and `"pending\\n"` are
    refused (a project rule's shape), pinned by
    `test_mcp_access_read_tools.py::test_a_status_outside_the_enum_is_refused_validation`."""


class AccessRequestSummary(BaseModel):
    request_id: uuid.UUID
    target_user_id: uuid.UUID
    role_id: uuid.UUID
    justification: str
    requested_expiry_days: int | None
    risk_class: str
    decision: str
    status: str
    created_at: datetime


class ListMyRequestsOutput(BaseModel):
    requests: list[AccessRequestSummary]


async def classify_access_request(
    session: AsyncSession, identity: Identity, args: ClassifyAccessRequestInput
) -> ClassifyAccessRequestOutput:
    """Classify one hypothetical request. Read-only; the engine decides.

    "Now" is read here, not passed in: tenure, grant liveness and the
    classification all answer for the moment of the call.
    """
    snapshot = await build_request_snapshot(
        session,
        SimulatedDirectory(session),
        requester_user_id=identity.user_id,
        target_user_id=args.target_user_id,
        role_id=args.role_id,
        requested_expiry_days=args.requested_expiry_days,
        now=datetime.now(UTC),
    )
    classification = classify(snapshot)
    return ClassifyAccessRequestOutput(
        risk_class=classification.risk_class,
        decision=classification.decision,
        reasons=list(classification.reasons),
        required_approvers=list(classification.required_approvers),
        effective_expiry_days=classification.effective_expiry_days,
        sod_conflicts=list(classification.sod_conflicts),
        policy_refs=list(classification.policy_refs),
    )


async def list_my_requests(
    session: AsyncSession, identity: Identity, args: ListMyRequestsInput
) -> ListMyRequestsOutput:
    """The requests the token user has filed, optionally by status."""
    rows = await list_requests_for_requester(session, identity.user_id, args.status)
    return ListMyRequestsOutput(
        requests=[
            AccessRequestSummary(
                request_id=row.id,
                target_user_id=row.target_user_id,
                role_id=row.role_id,
                justification=row.justification,
                requested_expiry_days=row.requested_expiry_days,
                risk_class=row.risk_class,
                decision=row.decision,
                status=row.status,
                created_at=row.created_at,
            )
            for row in rows
        ]
    )
