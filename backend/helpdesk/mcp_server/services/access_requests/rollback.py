"""`rollback_request`: undo a wrong decision within 24 hours of the grant.

Each live grant is ended through the shared revoke writer and gets a
`grant.rolled_back` event. Writes join the caller's transaction.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.access.directory import DirectoryProvider
from helpdesk.shared.access.requests import set_request_status
from helpdesk.shared.access.revoke import GrantNotActiveError, revoke_grant_row
from helpdesk.shared.audit import AuditEventInput, append_event
from helpdesk.shared.core.db import bound_tenant_id
from helpdesk.shared.core.errors import ErrorCode, ToolError
from helpdesk.shared.crud.grants import lock_live_grants_for_request
from helpdesk.shared.models import AccessRequest, Grant, RequestStatus

WINDOW_HOURS = 24
"""The window in hours, as one number every message is built from."""

ROLLBACK_WINDOW = timedelta(hours=WINDOW_HOURS)
"""Measured from the grant. Exactly 24 hours after is still allowed."""

ROLLBACK_REVOKE_REASON = "rollback"
"""The fixed revoke reason, so reports can match it. The caller's text goes in the event."""

GRANT_ROLLED_BACK_EVENT_TYPE = "grant.rolled_back"
"""One event per ended grant, with the grant as its subject."""

ROLLBACK_STATUS = RequestStatus.EXECUTED
"""The only status a rollback can undo; nothing else granted anything still live."""

_ACTOR_KIND = "user"
"""A rollback is always a person's call; no scheduled job does it."""

_SUBJECT_TYPE = "grant"


class RequestNotExecutedError(ToolError):
    """Nothing executed to undo. `POLICY_BLOCKED`, since the request does exist."""

    code = ErrorCode.POLICY_BLOCKED


class RollbackWindowExpiredError(ToolError):
    """The 24 hours are up. Older access is ended with `revoke_access` instead."""

    code = ErrorCode.POLICY_BLOCKED


@dataclass(frozen=True)
class RollbackResult:
    """The request as it now stands, and every row the undo ended."""

    request: AccessRequest
    revoked_grants: tuple[Grant, ...]


def within_rollback_window(granted_at: datetime, now: datetime) -> bool:
    """Is `now` within 24 hours of the grant? The edge counts as inside."""
    return now <= granted_at + ROLLBACK_WINDOW


async def rollback_request(
    session: AsyncSession,
    directory: DirectoryProvider,
    *,
    request: AccessRequest,
    reason: str,
    rolled_back_by_user_id: uuid.UUID,
    actor_ref: str | None = None,
    session_id: uuid.UUID | None = None,
    now: datetime,
) -> RollbackResult:
    """Undo one executed request: every live grant it made, then its status.

    Refuses before any write if the request is not executed, has no live grant, or
    its earliest grant is past the window. The status goes last, so a failure never
    marks a request undone while its access is still live.
    """
    if request.status != ROLLBACK_STATUS.value:
        raise RequestNotExecutedError(
            f"access request {request.id} is {request.status!r}, "
            f"and only an {ROLLBACK_STATUS.value} request can be rolled back"
        )

    grants = await lock_live_grants_for_request(session, request.id, now)
    if not grants:
        raise GrantNotActiveError(
            f"request {request.id} has no live grant left to roll back: "
            "it was already revoked, or it expired"
        )
    # `grants[0]` is the earliest. Measuring from the newest would allow an
    # undo past the window whenever a request left several rows.
    if not within_rollback_window(grants[0].granted_at, now):
        raise RollbackWindowExpiredError(
            f"access request {request.id} was granted at {grants[0].granted_at.isoformat()}, "
            f"which is more than {WINDOW_HOURS} hours ago"
        )

    revoked: list[Grant] = []
    for grant in grants:
        # Read before revoking: the event records what the grant was.
        granted_at, expires_at = grant.granted_at, grant.expires_at
        row = await revoke_grant_row(
            session,
            directory,
            grant=grant,
            reason=ROLLBACK_REVOKE_REASON,
            revoked_by_user_id=rolled_back_by_user_id,
            actor_ref=actor_ref,
            session_id=session_id,
            revoked_count=len(grants),
            now=now,
        )
        await _append_rolled_back(
            session,
            grant=row,
            request_id=request.id,
            granted_at=granted_at,
            expires_at=expires_at,
            reason=reason,
            rolled_back_count=len(grants),
            actor_ref=actor_ref if actor_ref is not None else str(rolled_back_by_user_id),
            session_id=session_id,
            now=now,
        )
        revoked.append(row)

    updated = await set_request_status(session, request.id, status=RequestStatus.ROLLED_BACK)
    return RollbackResult(request=updated, revoked_grants=tuple(revoked))


async def _append_rolled_back(
    session: AsyncSession,
    *,
    grant: Grant,
    request_id: uuid.UUID,
    granted_at: datetime,
    expires_at: datetime | None,
    reason: str,
    rolled_back_count: int,
    actor_ref: str,
    session_id: uuid.UUID | None,
    now: datetime,
) -> None:
    """One `grant.rolled_back` event: the original lifetime, the reason and the batch size."""
    await append_event(
        session,
        bound_tenant_id(session),
        AuditEventInput(
            occurred_at=now,
            actor_kind=_ACTOR_KIND,
            actor_ref=actor_ref,
            session_id=session_id,
            trace_id=None,
            event_type=GRANT_ROLLED_BACK_EVENT_TYPE,
            subject_type=_SUBJECT_TYPE,
            subject_id=str(grant.id),
            payload={
                "request_id": str(request_id),
                "user_id": str(grant.user_id),
                "role_id": str(grant.role_id),
                "granted_at": granted_at.isoformat(),
                "expires_at": None if expires_at is None else expires_at.isoformat(),
                "reason": reason,
                "rolled_back_count": rolled_back_count,
            },
        ),
    )


__all__ = [
    "GRANT_ROLLED_BACK_EVENT_TYPE",
    "ROLLBACK_REVOKE_REASON",
    "ROLLBACK_STATUS",
    "ROLLBACK_WINDOW",
    "WINDOW_HOURS",
    "RequestNotExecutedError",
    "RollbackResult",
    "RollbackWindowExpiredError",
    "rollback_request",
    "within_rollback_window",
]
