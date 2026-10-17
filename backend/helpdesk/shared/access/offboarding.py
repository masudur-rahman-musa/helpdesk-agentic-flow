"""Marking a user as a leaver and taking back everything they hold."""

import uuid
from datetime import date, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.access.directory import DirectoryProvider
from helpdesk.shared.access.lifecycle import (
    _SUBJECT_TYPE,
    LEAVER_STATUS,
    OFFBOARD_REVOKE_REASON,
    USER_OFFBOARDED_EVENT_TYPE,
    AlreadyLeaverError,
    OffboardResult,
)
from helpdesk.shared.access.requests import (
    append_request_rejected,
    lock_open_requests_for_target,
    set_request_status,
)
from helpdesk.shared.access.revoke import (
    USER_ACTOR_KIND,
    GrantNotActiveError,
    resolve_actor_ref,
    revoke_role_access,
)
from helpdesk.shared.audit import AuditEventInput, append_event
from helpdesk.shared.core.db import bound_tenant_id
from helpdesk.shared.core.errors import NotFoundError
from helpdesk.shared.crud import Repository
from helpdesk.shared.crud.users import lock_user
from helpdesk.shared.models import RequestStatus, User

__all__ = ["OFFBOARD_REVOKE_REASON", "USER_OFFBOARDED_EVENT_TYPE", "offboard_user"]


async def offboard_user(
    session: AsyncSession,
    directory: DirectoryProvider,
    *,
    user_id: uuid.UUID,
    end_date: date,
    offboarded_by_user_id: uuid.UUID | None,
    actor_ref: str | None = None,
    actor_kind: str = USER_ACTOR_KIND,
    session_id: uuid.UUID | None = None,
    now: datetime,
) -> OffboardResult:
    """Mark one person a leaver, take back everything they hold, close what is open.

    A missing user and another tenant's user give the same `NOT_FOUND`. The
    user row is locked first, so a second offboard racing this one waits,
    sees `leaver`, and is refused.

    Order: the status is set first, so a new request read after it is
    refused instead of granting access to someone halfway out. Then the
    revokes, then the request closures, then the departure event, which
    needs all of their results. A role emptied by someone else in between
    is just dropped from the list. A person holding nothing still leaves
    cleanly, or they would stay `active` for ever.

    `end_date` is written as given, not taken from `now`, since a departure
    can be recorded late. A None actor id is for the nightly leaver sweep.
    """
    resolved_actor = resolve_actor_ref(actor_ref, offboarded_by_user_id)

    user = await lock_user(session, user_id)
    if user is None:
        raise NotFoundError("no such user in this tenant")
    if user.status == LEAVER_STATUS:
        raise AlreadyLeaverError("that user has already been offboarded")

    # Read once, before any write, and sorted so events come out in a
    # steady order.
    held = sorted(await directory.list_account_role_ids(user_id, now))
    open_requests = await lock_open_requests_for_target(session, user_id)

    await Repository(session, User).update(user_id, status=LEAVER_STATUS, end_date=end_date)

    revoked_role_ids: list[uuid.UUID] = []
    revoked_grant_count = 0
    for role_id in held:
        try:
            revoked = await revoke_role_access(
                session,
                directory,
                user_id=user_id,
                role_id=role_id,
                reason=OFFBOARD_REVOKE_REASON,
                revoked_by_user_id=offboarded_by_user_id,
                actor_ref=resolved_actor,
                actor_kind=actor_kind,
                session_id=session_id,
                now=now,
            )
        except GrantNotActiveError:
            # Someone else emptied this role first; that is the end state we
            # wanted, so skip it.
            continue
        revoked_role_ids.append(role_id)
        revoked_grant_count += len(revoked)

    closed_request_ids: list[uuid.UUID] = []
    for request in open_requests:
        await set_request_status(session, request.id, status=RequestStatus.REJECTED)
        await append_request_rejected(
            session,
            request,
            reason=OFFBOARD_REVOKE_REASON,
            actor_ref=resolved_actor,
            actor_kind=actor_kind,
            session_id=session_id,
            now=now,
        )
        closed_request_ids.append(request.id)

    await _append_offboarded(
        session,
        user_id=user_id,
        end_date=end_date,
        revoked_role_ids=revoked_role_ids,
        revoked_grant_count=revoked_grant_count,
        closed_request_ids=closed_request_ids,
        actor_ref=resolved_actor,
        actor_kind=actor_kind,
        session_id=session_id,
        now=now,
    )
    return OffboardResult(
        user=user,
        revoked_role_ids=tuple(revoked_role_ids),
        revoked_grant_count=revoked_grant_count,
        closed_request_ids=tuple(closed_request_ids),
    )


async def _append_offboarded(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    end_date: date,
    revoked_role_ids: list[uuid.UUID],
    revoked_grant_count: int,
    closed_request_ids: list[uuid.UUID],
    actor_ref: str,
    actor_kind: str,
    session_id: uuid.UUID | None,
    now: datetime,
) -> None:
    """The departure event, with the leaver as subject.

    The grant count is kept beside the role list because they differ when
    one role was held twice.
    """
    await append_event(
        session,
        bound_tenant_id(session),
        AuditEventInput(
            occurred_at=now,
            actor_kind=actor_kind,
            actor_ref=actor_ref,
            session_id=session_id,
            trace_id=None,
            event_type=USER_OFFBOARDED_EVENT_TYPE,
            subject_type=_SUBJECT_TYPE,
            subject_id=str(user_id),
            payload={
                "user_id": str(user_id),
                "end_date": end_date.isoformat(),
                "revoked_role_ids": [str(role_id) for role_id in revoked_role_ids],
                "revoked_grant_count": revoked_grant_count,
                "closed_request_ids": [str(request_id) for request_id in closed_request_ids],
            },
        ),
    )
