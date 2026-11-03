"""Revoking access: the one place access is actually taken away.

Nothing makes a grant unique per (user, role), since renewals add a second
row. So revoking only the row a caller named could log "access ended" while
the user still held the role. `revoke_role_access` therefore ends every live
row for the pair. `revoke_grant_row` ends one row, for a review item, with no
liveness filter, so a past-expiry but still `active` grant can be ended. Both
share one writer, so the event and reason cannot drift.

No policy check here: there is nothing to check when access is removed.
Work is flushed in the caller's transaction, so revokes and their events
commit together.
"""

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.access.directory import DirectoryProvider
from helpdesk.shared.audit import AuditEventInput, append_event
from helpdesk.shared.core.db import bound_tenant_id
from helpdesk.shared.core.errors import ErrorCode, ToolError
from helpdesk.shared.crud.grants import REVOKED_GRANT_STATUS, lock_live_grants_for_role
from helpdesk.shared.models import Grant
from helpdesk.shared.notifications.request_events import notify_revoked

GRANT_REVOKED_EVENT_TYPE = "grant.revoked"
"""The audit event for "access was taken away", one per revoked row, so each
event names the grant that ended."""

USER_ACTOR_KIND = "user"
"""A revoke a person decided. The default, so human revokes are never logged
as automatic."""

SYSTEM_ACTOR_KIND = "system"
"""A revoke nobody decided: only the nightly leaver sweep uses it."""

_SUBJECT_TYPE = "grant"


class MissingActorError(ToolError):
    """No actor id and no `actor_ref`, which would log events by someone called "None"."""

    code = ErrorCode.VALIDATION


def resolve_actor_ref(actor_ref: str | None, actor_id: uuid.UUID | None) -> str:
    """What `audit_events.actor_ref` reads; an explicit `actor_ref` wins over the id."""
    if actor_ref is not None:
        return actor_ref
    if actor_id is None:
        raise MissingActorError("an audit event needs an actor: pass an actor id or an actor_ref")
    return str(actor_id)


class GrantNotActiveError(ToolError):
    """There is no live grant left for this (user, role).

    `POLICY_BLOCKED`, not `NOT_FOUND`: the caller has already reached this
    grant, and `NOT_FOUND` must keep "missing" and "another tenant's" alike.
    """

    code = ErrorCode.POLICY_BLOCKED
    reason = "There is no live grant of this role for this person."


async def revoke_role_access(
    session: AsyncSession,
    directory: DirectoryProvider,
    *,
    user_id: uuid.UUID,
    role_id: uuid.UUID,
    reason: str,
    revoked_by_user_id: uuid.UUID | None,
    actor_ref: str | None = None,
    actor_kind: str = USER_ACTOR_KIND,
    session_id: uuid.UUID | None = None,
    now: datetime,
) -> list[Grant]:
    """Revoke every live grant for this (user, role). All in the caller's transaction.

    `now` comes from the caller, so the grants picked and the `revoked_at`
    written agree on one instant. The rows stay locked to the end of the
    transaction, so two racing revokes write one event per row; the loser
    finds an empty set and is refused. The actor is checked before the lock.
    """
    resolved_actor = resolve_actor_ref(actor_ref, revoked_by_user_id)
    grants = await lock_live_grants_for_role(session, user_id, role_id, now)
    if not grants:
        raise GrantNotActiveError(
            f"no live grant for role {role_id} on user {user_id}: "
            "it was already revoked, or it expired"
        )

    return [
        await revoke_grant_row(
            session,
            directory,
            grant=grant,
            reason=reason,
            revoked_by_user_id=revoked_by_user_id,
            actor_ref=resolved_actor,
            actor_kind=actor_kind,
            session_id=session_id,
            revoked_count=len(grants),
            now=now,
        )
        for grant in grants
    ]


async def revoke_grant_row(
    session: AsyncSession,
    directory: DirectoryProvider,
    *,
    grant: Grant,
    reason: str,
    revoked_by_user_id: uuid.UUID | None,
    actor_ref: str | None = None,
    actor_kind: str = USER_ACTOR_KIND,
    session_id: uuid.UUID | None = None,
    revoked_count: int,
    now: datetime,
) -> Grant:
    """Revoke one grant row and write its `grant.revoked`. No liveness test.

    The caller has already picked the row; checking liveness again would
    drop the `stale` grant a review exists to end. User, role and request
    are read off the row, so the event cannot name a different grant.
    `revoked_count` is how many rows the caller's whole action ends, so an
    auditor reading one event knows whether others went with it. The person
    is told here, so every revoke path tells them.
    """
    resolved_actor = resolve_actor_ref(actor_ref, revoked_by_user_id)
    request_id = grant.request_id
    user_id = grant.user_id
    role_id = grant.role_id
    revoked = await directory.revoke_grant(grant=grant, reason=reason, revoked_at=now)
    await append_event(
        session,
        bound_tenant_id(session),
        AuditEventInput(
            occurred_at=now,
            actor_kind=actor_kind,
            actor_ref=resolved_actor,
            session_id=session_id,
            trace_id=None,
            event_type=GRANT_REVOKED_EVENT_TYPE,
            subject_type=_SUBJECT_TYPE,
            subject_id=str(grant.id),
            payload={
                "user_id": str(user_id),
                "role_id": str(role_id),
                "request_id": None if request_id is None else str(request_id),
                "reason": reason,
                "revoked_count": revoked_count,
            },
        ),
    )
    await notify_revoked(session, revoked, reason, actor_id=revoked_by_user_id)
    return revoked


__all__ = [
    "GRANT_REVOKED_EVENT_TYPE",
    "REVOKED_GRANT_STATUS",
    "SYSTEM_ACTOR_KIND",
    "USER_ACTOR_KIND",
    "GrantNotActiveError",
    "MissingActorError",
    "resolve_actor_ref",
    "revoke_grant_row",
    "revoke_role_access",
]
