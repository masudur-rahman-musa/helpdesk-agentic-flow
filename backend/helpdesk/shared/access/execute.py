"""`execute_grant`: the one place access is actually given (the design, §9.5).

Never a wire tool. the design makes it system-only and
the design says it is invoked internally: `create_access_request` calls it on
the `auto` branch, and `mcp_server.tools.approvals.approve_request` (plan
11c) calls it once every seat of a chain is approved. Registering it would
put a transport, a kill switch and an RBAC
check in front of something no caller may ever reach; it stays in the matrix
as an empty entry so an unknown-tool error cannot mask it.

The whole point of this module is that the decision is taken TWICE. SPEC
§9.5 requires the segregation-of-duties check to run again at execution
time, because the classification the user was shown describes a moment that
has passed: between it and this call another grant may have committed, or
the target may have become a leaver. So this rebuilds an earlier change's snapshot
from current state and re-runs the whole engine (decision G) rather than
`policy/sod.py` alone — one code path cannot drift from the classification
the user was shown, and the expiry the grant gets is then the expiry the
engine computed at the instant it was written.

Two refusals, both typed (the design) and both leaving NO grant row:
`SodConflictAtExecutionError` for a conflict, `PolicyBlockedAtExecutionError`
for any other fresh block. Their codes are pinned by
`test_access_execute.py::test_the_execution_errors_carry_their_spec_error_codes`
— the only pin they get, because `test_errors.py`'s `TYPED_ERRORS` table is
outside an earlier change's §8.

Everything here runs in the caller's transaction. The grant and its
`grant.executed` event are flushed, never committed, so a caller that fails
afterwards takes both with it (pinned by
`test_access_execute.py::test_the_grant_and_its_event_roll_back_together`).

RESOLVED by an earlier change, and still true of this module: nothing here makes a
grant unique per (user, role): two requests for the same role under two
different idempotency keys both classify, both execute, and leave two live
`grants` rows for one role. The engine deliberately does not block it — it
sees the target's `active_grant_role_ids` and chooses not to, because
re-granting is how a renewal has to work — so refusing here would contradict
the classification the user was shown. The consequence was answered on the
revocation side rather than by a constraint here: `helpdesk.access.revoke`
takes the LIVE SET for a (user, role) as its unit and revokes every row in it
(decision A), so access cannot survive a revocation the audit log records as
successful.
"""

import uuid
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.core.db import bound_tenant_id
from helpdesk.shared.core.errors import ErrorCode, ToolError
from helpdesk.shared.models import Grant
from helpdesk.shared.policy.engine import classify
from helpdesk.shared.policy.types import Classification, Decision
from helpdesk.shared.access.directory import DirectoryProvider, DirectoryReader
from helpdesk.shared.access.snapshot import build_request_snapshot
from helpdesk.shared.audit import AuditEventInput, append_event

GRANT_EXECUTED_EVENT_TYPE = "grant.executed"
"""the design's own name for "access was actually given"."""

_ACTOR_KIND = "system"
"""`execute_grant` is never a user's call: the design makes it system-only, so
the event records the system as the actor and carries the human who caused
it in `actor_ref` and the payload."""

_SUBJECT_TYPE = "grant"


class SodConflictAtExecutionError(ToolError):
    """A segregation-of-duties conflict exists NOW (the design, requirement #8).

    Distinct from a classification-time block: this one fired against state
    that changed after the user was told the request was clean. No grant is
    written.
    """

    code = ErrorCode.SOD_CONFLICT


class PolicyBlockedAtExecutionError(ToolError):
    """The fresh classification blocks for a reason other than SoD.

    A target who became a `leaver` is the case this exists for. Separate
    from `SodConflictAtExecutionError` because the design gives the two
    different codes, and the caller and the audit trail must be able to tell
    "you may not have this" from "these two roles cannot be held together".
    """

    code = ErrorCode.POLICY_BLOCKED


async def execute_grant(
    session: AsyncSession,
    directory: DirectoryProvider,
    *,
    request_id: uuid.UUID | None,
    requester_user_id: uuid.UUID,
    target_user_id: uuid.UUID,
    role_id: uuid.UUID,
    requested_expiry_days: int | None = None,
    granted_by_user_id: uuid.UUID | None = None,
    actor_ref: str | None = None,
    session_id: uuid.UUID | None = None,
    now: datetime,
) -> Grant:
    """Re-classify, refuse or grant, and record it. All in the caller's transaction.

    `now` is keyword-only and supplied by the caller so that the snapshot,
    the classification, `granted_at` and `expires_at` all answer for one
    instant; reading a clock here instead would let the grant start before
    the state it was checked against.
    """
    classification = await reclassify(
        session,
        directory,
        requester_user_id=requester_user_id,
        target_user_id=target_user_id,
        role_id=role_id,
        requested_expiry_days=requested_expiry_days,
        now=now,
    )

    grant = await directory.apply_grant(
        user_id=target_user_id,
        role_id=role_id,
        request_id=request_id,
        granted_by_user_id=granted_by_user_id,
        granted_at=now,
        expires_at=now + timedelta(days=classification.effective_expiry_days),
    )
    await append_event(
        session,
        bound_tenant_id(session),
        AuditEventInput(
            occurred_at=now,
            actor_kind=_ACTOR_KIND,
            actor_ref=actor_ref,
            session_id=session_id,
            trace_id=None,
            event_type=GRANT_EXECUTED_EVENT_TYPE,
            subject_type=_SUBJECT_TYPE,
            subject_id=str(grant.id),
            payload={
                "request_id": None if request_id is None else str(request_id),
                "target_user_id": str(target_user_id),
                "role_id": str(role_id),
                "risk_class": classification.risk_class.value,
                "effective_expiry_days": classification.effective_expiry_days,
                "expires_at": grant.expires_at.isoformat() if grant.expires_at else None,
            },
        ),
    )
    return grant


async def reclassify(
    session: AsyncSession,
    directory: DirectoryReader,
    *,
    requester_user_id: uuid.UUID,
    target_user_id: uuid.UUID,
    role_id: uuid.UUID,
    requested_expiry_days: int | None,
    now: datetime,
) -> Classification:
    """The the design re-check: a whole fresh classification, or a refusal.

    Conflicts are tested before the decision so a conflicting request is
    reported as `SOD_CONFLICT` rather than the `POLICY_BLOCKED` its
    `blocked` decision would otherwise produce — the engine blocks on a
    conflict, so the order here is what decides which of the two the design
    codes the caller sees.

    Public because an earlier change needs the same answer WITHOUT the grant:
    `helpdesk.access.lifecycle` has to name the roles a joiner's template
    will and will not give them before either grant is written, and a second
    copy of the order above would be free to drift from the one that
    actually writes them. It stays a re-check and never a decision of its
    own — the only caller that may act on it is one that then goes through
    `execute_grant`.
    """
    snapshot = await build_request_snapshot(
        session,
        directory,
        requester_user_id=requester_user_id,
        target_user_id=target_user_id,
        role_id=role_id,
        requested_expiry_days=requested_expiry_days,
        now=now,
    )
    classification = classify(snapshot)
    if classification.sod_conflicts:
        raise SodConflictAtExecutionError(
            "segregation of duties conflict at execution time: "
            f"{[str(conflict.rule_id) for conflict in classification.sod_conflicts]}"
        )
    if classification.decision is Decision.BLOCKED:
        raise PolicyBlockedAtExecutionError(
            f"policy blocks this request at execution time: {list(classification.reasons)}"
        )
    return classification


__all__ = [
    "GRANT_EXECUTED_EVENT_TYPE",
    "PolicyBlockedAtExecutionError",
    "SodConflictAtExecutionError",
    "execute_grant",
    "reclassify",
]
