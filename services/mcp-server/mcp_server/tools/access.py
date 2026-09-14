"""The access tools: two reads from an earlier change, two mutations from an earlier change.

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

`create_access_request` is the first mutation here: it classifies
the same way, persists exactly one `access_requests` row carrying the
engine's verdict, and then branches — `auto` executes the grant in the same
transaction, `approval` queues the row with the engine's chain, `blocked`
files it as rejected and grants nothing. It is the one tool in this module
whose target may be someone other than the token's user, and that is
authorized here rather than in the pipeline (decision B): `target_user_id`
is only known after input validation, which by design runs AFTER the RBAC
stage so a caller who may not call a tool learns nothing about its argument
shape. The second check is therefore inside the handler, before any write,
and the pipeline records the `FORBIDDEN` it raises as `tool.denied`.

`revoke_access` is the other direction, and the only tool here
that takes no policy decision at all: there is nothing to classify when
access is being removed. It resolves the named grant, runs the design's scope
through an earlier change's `request_in_scope` — one scope authority for approvals and
revocation — and then hands the (user, role) pair to
`helpdesk.access.revoke_role_access`, which revokes the whole LIVE SET
(decision A) rather than the row the caller named.

No function here opens a session or touches the transport: the pipeline
binds the session to the token's tenant before any of them is called.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from helpdesk.access import (
    DirectoryProvider,
    GrantNotActiveError,
    SimulatedDirectory,
    approval_slots,
    build_request_snapshot,
    execute_grant,
    request_in_scope,
    resolved_approver_ids,
    revoke_role_access,
)
from helpdesk.access import (
    create_access_request as persist_access_request,
)
from helpdesk.audit import AuditEventInput, append_event
from helpdesk.auth import rbac
from helpdesk.core.repository import (
    Repository,
    is_live_grant,
    list_requests_with_context_for_requester,
)
from helpdesk.errors import NotFoundError
from helpdesk.models import Grant, RequestStatus, Role, System
from helpdesk.policy.engine import classify
from helpdesk.policy.types import (
    Classification,
    Decision,
    RequiredApprover,
    RiskClass,
    SodConflict,
)
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_server.identity import Identity
from mcp_server.tools.base import MutatingToolInput

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


REQUESTS_LIMIT_MIN = 1
REQUESTS_LIMIT_DEFAULT = 50
REQUESTS_LIMIT_MAX = 200
"""How many requests one `list_my_requests` call may return.

The same trio `tools/read.py` gives `search_audit`, and here for the same
reason: `access_requests` only grows, so an unbounded read is one a long-lived
tenant eventually cannot render. All three are pinned exactly by
`test_mcp_access_read_tools.py::test_the_request_limit_constants_are_exactly_their_planned_values`,
the default again through the model by
`::test_the_default_request_limit_is_read_off_the_input_model`, and the
inclusive ends by `::test_the_request_limit_boundary_is_inclusive_at_1_and_200`,
whose accepted rows also prove the validated bound reaches the query."""


class ListMyRequestsInput(BaseModel):
    model_config = _IGNORE_EXTRAS

    status: RequestStatus | None = None
    """Exact the design enum membership or VALIDATION: pydantic's StrEnum
    lookup does no stripping, so `"pending "` and `"pending\\n"` are
    refused (a project rule's shape), pinned by
    `test_mcp_access_read_tools.py::test_a_status_outside_the_enum_is_refused_validation`."""

    limit: int = Field(default=REQUESTS_LIMIT_DEFAULT, ge=REQUESTS_LIMIT_MIN, le=REQUESTS_LIMIT_MAX)
    """Refused by the INPUT MODEL, so `0` and `201` answer `VALIDATION`
    before any DB access at all."""


class AccessRequestSummary(BaseModel):
    """One request as a reader sees it: nineteen fields, ids and names both.

    Widened by an earlier change from the nine stored columns it began as (decision B):
    the browser has no directory lookup and must never gain one, so the role,
    the system and the target are NAMED here rather than left as uuids for a
    second read. The names are resolved from the rows the ids point at, never
    echoed from the request row, pinned by
    `test_mcp_access_read_tools.py::test_the_names_come_from_the_directory_and_not_from_the_request_row`.

    `approvals` is the stored `SlotState` chain rendered BY SLOT (decision D):
    no seat's user id is resolved to a display name, because that is a second
    directory read per row for a label the slot already carries.

    `grant_id` and `expires_at` come off the grant the request produced, not
    from `requested_expiry_days` (decision E), which the engine may have
    capped. Both are `None` when nothing was granted. Pinned by
    `::test_the_grant_join_is_null_when_pending_and_carries_the_grant_when_executed`,
    and the exact field set by
    `::test_the_widened_row_carries_exactly_the_nineteen_planned_fields`.
    """

    request_id: uuid.UUID
    target_user_id: uuid.UUID
    role_id: uuid.UUID
    justification: str
    requested_expiry_days: int | None
    risk_class: str
    decision: str
    status: str
    created_at: datetime
    role_slug: str
    role_name: str
    system_slug: str
    system_name: str
    target_display_name: str
    decision_reasons: list[Any]
    citations: list[Any]
    approvals: list[Any]
    grant_id: uuid.UUID | None
    expires_at: datetime | None


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


REQUEST_CREATED_EVENT_TYPE = "request.created"
"""The pipeline's declared event for `create_access_request` (the design).

Written by the pipeline AFTER the handler returns, which is why it lands
LAST in the audit chain even though it reads first.
The sequences are pinned by the three branch cases in
`test_mcp_create_access_request.py`."""

REQUEST_QUEUED_EVENT_TYPE = "request.queued"
"""Appended by this module on the `approval` branch."""

REQUEST_AUTO_EXECUTED_EVENT_TYPE = "request.auto_executed"
"""Appended by this module on the `auto` branch, before `grant.executed`.

Distinct facts, not one: the design names both, and
"the policy engine decided this needed no approval" is a different record
from "access was given"."""

JUSTIFICATION_MAX_LENGTH = 2000
"""Cap on `create_access_request.justification`.

`access_requests.justification` is unbounded `Text`, so this Pydantic bound
is the only backstop. Pinned inclusively by
`test_mcp_create_access_request.py::test_the_justification_boundary_is_inclusive_at_2000`."""

CITATIONS_MAX = 20
"""Cap on `citations[]` entries. `citations` is a JSONB
column with no length of its own, so this bound is the only backstop; pinned
by `test_the_citations_boundary_is_inclusive_at_20`."""

CITATION_FIELD_MAX_LENGTH = 512
"""Cap on every `Citation` string field.

The fields land inside the same unbounded JSONB column, so without one bound
per field the cap on the LIST would still admit twenty arbitrarily long
documents. Pinned inclusively, and on every one of the
five fields rather than one of them, by
`test_mcp_create_access_request.py::test_the_citation_field_boundary_is_inclusive_at_512`."""

_ACTOR_KIND = "user"
_SUBJECT_TYPE = "access_request"

# The engine's `auto` verdict means executed here and nowhere else: the row's
# lifecycle status is this module's, the decision is the engine's.
_STATUS_FOR_DECISION: dict[Decision, RequestStatus] = {
    Decision.AUTO: RequestStatus.EXECUTED,
    Decision.APPROVAL: RequestStatus.PENDING,
    Decision.BLOCKED: RequestStatus.REJECTED,
}


class Citation(BaseModel):
    """One the design citation: `{document_slug, title, version, heading_path, chunk_id}`.

    Typed now, before RAG exists: the tool's the design
    signature already carries citations, and a typed empty list today is the
    same shape row 15 will fill. Extras are forbidden so a caller cannot
    smuggle unbounded free text into the JSONB column under a new key.
    """

    model_config = ConfigDict(extra="forbid")

    document_slug: str = Field(max_length=CITATION_FIELD_MAX_LENGTH)
    title: str = Field(max_length=CITATION_FIELD_MAX_LENGTH)
    version: str = Field(max_length=CITATION_FIELD_MAX_LENGTH)
    heading_path: str = Field(max_length=CITATION_FIELD_MAX_LENGTH)
    chunk_id: str = Field(max_length=CITATION_FIELD_MAX_LENGTH)


class CreateAccessRequestInput(MutatingToolInput):
    """the design's signature. `extra="forbid"` comes from `MutatingToolInput`."""

    target_user_id: uuid.UUID
    role_id: uuid.UUID
    justification: str = Field(min_length=1, max_length=JUSTIFICATION_MAX_LENGTH)
    requested_expiry_days: int | None = Field(default=None, gt=0, le=EXPIRY_DAYS_MAX)
    citations: list[Citation] = Field(default_factory=list, max_length=CITATIONS_MAX)


class CreateAccessRequestOutput(BaseModel):
    """The filed request: the engine's verdict plus what the branch did with it."""

    request_id: uuid.UUID
    status: RequestStatus
    risk_class: RiskClass
    decision: Decision
    reasons: list[str]
    required_approvers: list[RequiredApprover]
    approver_user_ids: list[uuid.UUID]
    """Only the slots the engine could resolve, so this can be shorter than
    `required_approvers` — a group seat (`it_admin`, `hr`) names nobody."""
    effective_expiry_days: int
    sod_conflicts: list[SodConflict]
    policy_refs: list[str]
    grant_id: uuid.UUID | None
    expires_at: datetime | None


async def _authorize_target(
    directory: DirectoryProvider, identity: Identity, target_user_id: uuid.UUID
) -> None:
    """May this caller ask on behalf of this target? (the design, decision B.)

    A self-request needs nothing beyond the `create_access_request` (self)
    row every principal holds. Naming anyone else is the spec's SECOND row,
    checked here rather than in the pipeline because the target is only
    known after input validation, which runs after the RBAC stage by design.
    Raises before any write, so a refusal leaves no request row and no grant.

    Two guards in sequence, and each has a case the other cannot decide.
    Deleting the `authorize` call is caught by
    `test_an_employee_may_not_name_a_colleague_even_in_their_own_department`
    (a plain employee whose target passes the department comparison);
    deleting the `any` branch is caught by
    `test_an_hr_officer_may_name_a_target_in_any_department`; deleting the
    department comparison is caught by
    `test_a_manager_naming_a_target_in_another_department_is_denied`.

    The caller's department is read from the directory, not from the token's
    `department_id` claim: a manager transferred after their token was issued
    would otherwise keep reaching into the department they left. Pinned by
    `test_the_callers_department_is_read_from_the_directory_not_the_token`,
    which moves the caller's row and reuses the token minted before the move.
    The user id itself still comes only from the token.

    Scope narrowing is not a risk for this tool. `authorize` collapses a
    multi-principal caller to the WIDEST allowing scope, and the two scopes
    on this row are `own_department` and `any`, so a manager who is also HR
    correctly gets `any`. The incomparable-scope problem an earlier change recorded
    (owed to row 11c) needs two same-width scopes, which this row has not.
    """
    if target_user_id == identity.user_id:
        return

    scope = rbac.authorize(identity.claims, rbac.Tool.CREATE_ACCESS_REQUEST_ON_BEHALF).scope
    if scope is rbac.Scope.ANY:
        return
    if scope is not rbac.Scope.OWN_DEPARTMENT:
        # No other scope appears on this row today. Refusing rather than
        # allowing keeps a future matrix edit from widening this tool by
        # accident.
        raise _on_behalf_denied()

    caller = await directory.lookup_user(identity.user_id)
    if caller is None or caller.department_id is None:
        raise _on_behalf_denied()
    target = await directory.lookup_user(target_user_id)
    # One answer for "no such user" and "a user in another department": an
    # own_department caller must not be able to use this seam to learn which
    # uuids exist in their tenant, the same reasoning `NotFoundError` carries
    # for cross-tenant reads. A caller whose scope is `any` returned above
    # and still meets the honest NOT_FOUND from the snapshot builder.
    if target is None or target.department_id != caller.department_id:
        raise _on_behalf_denied()


def _on_behalf_denied() -> rbac.RbacDenied:
    """The one refusal every on-behalf branch raises, so none can drift."""
    return rbac.RbacDenied(rbac.Tool.CREATE_ACCESS_REQUEST_ON_BEHALF.value)


async def _append(
    session: AsyncSession,
    identity: Identity,
    event_type: str,
    request_id: uuid.UUID,
    payload: dict[str, Any],
    now: datetime,
) -> None:
    """One domain event, in the tool's own transaction (decision E)."""
    await append_event(
        session,
        identity.tenant_id,
        AuditEventInput(
            occurred_at=now,
            actor_kind=_ACTOR_KIND,
            actor_ref=str(identity.user_id),
            session_id=identity.session_id,
            trace_id=None,
            event_type=event_type,
            subject_type=_SUBJECT_TYPE,
            subject_id=str(request_id),
            payload=payload,
        ),
    )


async def create_access_request(
    session: AsyncSession, identity: Identity, args: CreateAccessRequestInput
) -> CreateAccessRequestOutput:
    """File one access request, then do what the engine's decision says.

    The requester is always the token's user; only the target may be someone
    else, and only with the on-behalf permission checked above. Everything
    below runs in one transaction the pipeline opened, so a refusal from
    `execute_grant` takes the request row with it (decision F): on the auto
    path the classification is microseconds old, so that fires only on a
    concurrent commit, and there is nothing yet for the user to point at.
    """
    directory = SimulatedDirectory(session)
    await _authorize_target(directory, identity, args.target_user_id)

    now = datetime.now(UTC)
    snapshot = await build_request_snapshot(
        session,
        directory,
        requester_user_id=identity.user_id,
        target_user_id=args.target_user_id,
        role_id=args.role_id,
        requested_expiry_days=args.requested_expiry_days,
        now=now,
    )
    classification = classify(snapshot)
    status = _STATUS_FOR_DECISION[classification.decision]

    request = await persist_access_request(
        session,
        requester_user_id=identity.user_id,
        target_user_id=args.target_user_id,
        role_id=args.role_id,
        justification=args.justification,
        requested_expiry_days=args.requested_expiry_days,
        classification=classification,
        status=status,
        citations=[citation.model_dump(mode="json") for citation in args.citations],
        idempotency_key=args.idempotency_key,
        session_id=identity.session_id,
    )

    grant = await _run_branch(session, identity, args, classification, request.id, now)
    return _output(classification, request.id, status, grant)


async def _run_branch(
    session: AsyncSession,
    identity: Identity,
    args: CreateAccessRequestInput,
    classification: Classification,
    request_id: uuid.UUID,
    now: datetime,
) -> Grant | None:
    """The one branch this decision earns, with its domain events.

    `blocked` earns no domain event at all: the pipeline's `request.created`
    already records that a rejected request was filed, and inventing a
    second event for it would put a name in the log that the design does not
    have.
    """
    if classification.decision is Decision.AUTO:
        await _append(
            session,
            identity,
            REQUEST_AUTO_EXECUTED_EVENT_TYPE,
            request_id,
            {
                "role_id": str(args.role_id),
                "target_user_id": str(args.target_user_id),
                "risk_class": classification.risk_class.value,
            },
            now,
        )
        return await execute_grant(
            session,
            SimulatedDirectory(session),
            request_id=request_id,
            requester_user_id=identity.user_id,
            target_user_id=args.target_user_id,
            role_id=args.role_id,
            requested_expiry_days=args.requested_expiry_days,
            granted_by_user_id=identity.user_id,
            actor_ref=str(identity.user_id),
            session_id=identity.session_id,
            now=now,
        )
    if classification.decision is Decision.APPROVAL:
        await _append(
            session,
            identity,
            REQUEST_QUEUED_EVENT_TYPE,
            request_id,
            {
                "role_id": str(args.role_id),
                "target_user_id": str(args.target_user_id),
                "risk_class": classification.risk_class.value,
                "approvals": approval_slots(classification.required_approvers),
            },
            now,
        )
    return None


def _output(
    classification: Classification,
    request_id: uuid.UUID,
    status: RequestStatus,
    grant: Grant | None,
) -> CreateAccessRequestOutput:
    return CreateAccessRequestOutput(
        request_id=request_id,
        status=status,
        risk_class=classification.risk_class,
        decision=classification.decision,
        reasons=list(classification.reasons),
        required_approvers=list(classification.required_approvers),
        approver_user_ids=resolved_approver_ids(classification.required_approvers),
        effective_expiry_days=classification.effective_expiry_days,
        sod_conflicts=list(classification.sod_conflicts),
        policy_refs=list(classification.policy_refs),
        grant_id=None if grant is None else grant.id,
        expires_at=None if grant is None else grant.expires_at,
    )


REASON_MAX_LENGTH = 2000
"""Cap on `revoke_access.reason`.

`grants.revoke_reason` is unbounded `Text`, so this Pydantic bound is the only
backstop there is. The lower bound is 1, not 0: an access removal with an
empty reason is one nobody can audit. Both ends pinned inclusively by
`test_mcp_revoke_access.py::test_the_reason_boundary_is_inclusive_at_1_and_2000`."""


class RevokeAccessInput(MutatingToolInput):
    """the design's signature. `extra="forbid"` comes from `MutatingToolInput`."""

    grant_id: uuid.UUID
    reason: str = Field(min_length=1, max_length=REASON_MAX_LENGTH)


class RevokeAccessOutput(BaseModel):
    """Every grant this call ended, not just the one it was handed."""

    revoked_grant_ids: list[uuid.UUID]
    """EVERY live row for the named grant's `(user, role)` (decision A), the
    named one included. Shorter output would misreport the scope of what
    happened: an earlier change leaves two live rows for one role reachable."""

    revoked_count: int


async def _grant_in_scope(session: AsyncSession, identity: Identity, grant: Grant) -> bool:
    """Does the design's `revoke_access` row reach this grant?

    The same `request_in_scope` the approval tools use (decision B), asked with
    grant-derived arguments: its parameters are already neutral — an actor, the
    actor's department, the subject's department, the system's owner — so
    approvals and revocation cannot answer the scope question differently.

    The caller's department and the holder's are read from the DIRECTORY, never
    from the token's `department_id` claim: a manager transferred after their
    token was issued must not revoke in the department they left. Pinned in
    both directions by
    `test_mcp_revoke_access.py::test_the_managers_department_comes_from_the_directory_not_the_token`,
    which moves the caller's row and reuses the token minted before the move.
    The user id itself still comes only from the token.

    This runs BEFORE the liveness test (decision C), so a caller whose scope
    does not reach a grant learns `FORBIDDEN` whatever state it is in, and the
    id cannot be probed for that state. Pinned by
    `::test_an_already_revoked_grant_is_refused_policy_blocked`, whose second
    half sends the SAME not-live state out of scope.
    """
    directory = SimulatedDirectory(session)
    caller = await directory.lookup_user(identity.user_id)
    holder = await directory.lookup_user(grant.user_id)
    role = await Repository(session, Role).get(grant.role_id)
    system = None if role is None else await Repository(session, System).get(role.system_id)
    return request_in_scope(
        rbac.authorize(identity.claims, rbac.Tool.REVOKE_ACCESS).scopes,
        approver_user_id=identity.user_id,
        approver_department_id=None if caller is None else caller.department_id,
        target_department_id=None if holder is None else holder.department_id,
        system_owner_user_id=None if system is None else system.owner_user_id,
    )


async def revoke_access(
    session: AsyncSession, identity: Identity, args: RevokeAccessInput
) -> RevokeAccessOutput:
    """Take away the access this grant represents, and every live twin of it.

    Three refusals in this order, each a different the design code and each with
    a case no other guard can decide:

    `NOT_FOUND` — no such grant, or one in another tenant. `Repository.get`
    runs through the tenant-bound session, so the two are indistinguishable and
    a grant id is not an existence oracle
    (`test_another_tenants_grant_and_a_missing_one_answer_the_same_way`).

    `FORBIDDEN` — the design's scope does not reach the grant. Raised as
    `RbacDenied`, which the pipeline records as `tool.denied` rather than
    `tool.failed` (a permission decision filed as an outage would be a false
    record). This runs before the liveness test on purpose. Its own kill is
    `test_a_manager_may_not_revoke_outside_their_department`, whose caller the
    matrix admits, so only the department comparison can refuse it.

    The pipeline's matrix stage has NO independent kill on this tool, and that
    is a property of the code rather than a gap in the tests: `_grant_in_scope`
    re-runs `rbac.authorize`, which raises for a principal holding no cell on
    this the design row, so a caller the matrix refuses is refused here too and
    deleting the pipeline stage changes no answer.
    `test_a_denied_call_writes_tool_denied_and_revokes_nothing` pins the
    refusal and its `tool.denied` record, not that stage.

    `POLICY_BLOCKED` — the grant is not live (decision C), from
    `GrantNotActiveError`, raised here for the named row and again inside
    `revoke_role_access` when the whole locked live set is empty. The second is
    what a caller who lost a race sees
    (`test_two_callers_racing_the_same_revocation_write_one_event_per_row`).

    `now` is read once and passed on, so the liveness test, the locked read and
    every `revoked_at` answer for one instant. Everything runs in the
    transaction the pipeline opened, so the revoked rows and their
    `grant.revoked` events commit or roll back together.
    """
    grant = await Repository(session, Grant).get(args.grant_id)
    if grant is None:
        raise NotFoundError("no such grant in this tenant")
    if not await _grant_in_scope(session, identity, grant):
        raise rbac.RbacDenied(rbac.Tool.REVOKE_ACCESS.value)

    now = datetime.now(UTC)
    # The named grant is tested for liveness through the same predicate the
    # locked read uses, so a caller pointing at a row that is already gone is
    # told so, rather than silently having a sibling row revoked under an
    # output that does not contain the id they sent.
    if not is_live_grant(grant, now):
        raise GrantNotActiveError(f"grant {grant.id} is not live: it was revoked, or it expired")

    revoked = await revoke_role_access(
        session,
        SimulatedDirectory(session),
        user_id=grant.user_id,
        role_id=grant.role_id,
        reason=args.reason,
        revoked_by_user_id=identity.user_id,
        actor_ref=str(identity.user_id),
        session_id=identity.session_id,
        now=now,
    )
    return RevokeAccessOutput(
        revoked_grant_ids=[row.id for row in revoked], revoked_count=len(revoked)
    )


async def list_my_requests(
    session: AsyncSession, identity: Identity, args: ListMyRequestsInput
) -> ListMyRequestsOutput:
    """The requests the token user has filed, optionally by status.

    Requester-scoped off the token, never off an argument: the
    input declares no user field and drops an extra one. The join behind it
    resolves every name in one statement rather than one read per row; see
    `list_requests_with_context_for_requester`.
    """
    rows = await list_requests_with_context_for_requester(
        session,
        identity.user_id,
        None if args.status is None else args.status.value,
        args.limit,
    )
    return ListMyRequestsOutput(
        requests=[
            AccessRequestSummary(
                request_id=row.request.id,
                target_user_id=row.request.target_user_id,
                role_id=row.request.role_id,
                justification=row.request.justification,
                requested_expiry_days=row.request.requested_expiry_days,
                risk_class=row.request.risk_class,
                decision=row.request.decision,
                status=row.request.status,
                created_at=row.request.created_at,
                role_slug=row.role_slug,
                role_name=row.role_name,
                system_slug=row.system_slug,
                system_name=row.system_name,
                target_display_name=row.target_display_name,
                decision_reasons=list(row.request.decision_reasons),
                citations=list(row.request.citations),
                approvals=list(row.request.approvals),
                grant_id=row.grant_id,
                expires_at=row.expires_at,
            )
            for row in rows
        ]
    )
