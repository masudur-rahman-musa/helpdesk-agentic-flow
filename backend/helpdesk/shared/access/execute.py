"""`execute_grant`: the one place access is actually given.

Not a tool anyone can call: the request flow calls it on the `auto` path,
and approval calls it once every seat is approved.

The policy check runs twice. What the user was shown describes a moment
that has passed: another grant may have landed, or the target may have
left. So this rebuilds the snapshot from current data and runs the whole
engine again, not just the duties check, so the two answers cannot drift.
A refusal leaves no grant. The grant and its event are flushed in the
caller's transaction, so they commit or roll back together.

Nothing here makes a grant unique per (user, role); re-granting is how a
renewal works. Revoke handles it by ending every live row for that pair.
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
"""The audit event for "access was actually given"."""

_ACTOR_KIND = "system"
"""Only the system runs grants; the person who caused it goes in `actor_ref`."""

_SUBJECT_TYPE = "grant"


class SodConflictAtExecutionError(ToolError):
    """A separation-of-duties conflict exists now, after the user was told the request was clean."""

    code = ErrorCode.SOD_CONFLICT


class PolicyBlockedAtExecutionError(ToolError):
    """The fresh check blocks for another reason, such as the target having left.

    Its own error code, so "you may not have this" reads differently from
    "these two roles cannot be held together".
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
    """Re-check, refuse or grant, and record it, all in the caller's transaction.

    `now` comes from the caller so the snapshot, the check and the grant
    times all answer for one instant.
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
    """A whole fresh classification, or a refusal.

    Conflicts are tested before the decision, so the caller sees
    `SOD_CONFLICT` and not `POLICY_BLOCKED`. Onboarding also calls this to
    learn which template roles will be refused, before any grant is written.
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
