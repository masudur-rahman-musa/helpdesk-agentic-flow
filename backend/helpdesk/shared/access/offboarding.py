"""Marking a user as a leaver and revoking everything they hold. See `lifecycle.py` for the shared types this uses."""

import uuid
from datetime import date, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.access.directory import DirectoryProvider
from helpdesk.shared.access.lifecycle import (
    LEAVER_STATUS,
    OFFBOARD_REVOKE_REASON,
    USER_OFFBOARDED_EVENT_TYPE,
    AlreadyLeaverError,
    OffboardResult,
    _SUBJECT_TYPE,
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

__all__ = ["offboard_user"]


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

    The caller resolves nothing first: `lock_user` answers None for a user id
    that is missing OR another tenant's, so one `NOT_FOUND` covers both and
    the id is no cross-tenant existence oracle (pinned by
    `test_mcp_offboard_user.py::test_a_missing_and_another_tenants_user_answer_the_same_not_found`).
    The lock comes first because the refusal below reads state a concurrent
    offboard rewrites: the loser waits on it, re-reads the `leaver` the winner
    wrote and is refused, which is what leaves one departure in the log
    (pinned by `::test_two_callers_racing_the_same_offboard_leave_one_departure`).

    Order of effects, each part of it a decision:

    1. `status = leaver` and `end_date` are written BEFORE anything is taken
       away (decision D), because the engine blocks every new grant for a
       leaver (the design): a `create_access_request` that reads the row after
       this write is refused instead of granting access to somebody halfway
       out. The window is narrowed, not closed — see the module docstring.
    2. the revocations, then the closures. §1 and §6 fix that observable
       order; §4's decision D lists the two the other way round and argues
       only the point above, so the acceptance criteria are what this follows
       (recorded in the plan's §9).
    3. `user.offboarded` last (before the pipeline's `tool.succeeded`), because
       every list it names is known only by then.

    A role whose live set was emptied between the read and its revocation
    costs that role and nothing else (decision H): the end state that role
    wanted already holds, so it is dropped from `revoked_role_ids` and the
    departure stands (pinned by
    `test_access_offboard.py::test_a_role_emptied_before_its_revocation_is_omitted_and_the_rest_stands`).

    A person holding nothing at all is offboarded cleanly, not refused
    (decision G): offboarding is about the person's state, and refusing here
    would leave a departing employee `active` for ever precisely because they
    had no access (pinned by
    `::test_a_user_with_no_grants_and_no_open_requests_offboards_cleanly`).

    `now` is keyword-only and supplied by the caller, so the liveness test that
    picked the held set, every `revoked_at` and the event all answer for one
    instant. `end_date` is a separate argument and is written as given, never
    derived from `now`: the two are different days whenever a departure is
    recorded late (pinned by
    `::test_the_status_becomes_leaver_and_the_end_date_is_the_argument`).

    Everything runs in the caller's transaction, so the status, the
    revocations, the closures and every event commit or roll back together
    (pinned by `::test_the_offboard_and_its_events_roll_back_together`).

    `actor_kind` defaults to `USER_ACTOR_KIND` and the actor id is nullable
    only so an earlier change's leaver sweep can offboard as `system` with no person to
    name (decision D). Every existing caller passes an id and no kind, so its
    events read exactly as they did (pinned by
    `test_access_leavers.py::test_an_offboard_with_an_actor_id_and_no_actor_kind_still_writes_the_user_actor`);
    a call with neither an id nor a ref is refused `VALIDATION` before the
    lock, so nothing is written for an event nobody could be named on.
    """
    resolved_actor = resolve_actor_ref(actor_ref, offboarded_by_user_id)

    user = await lock_user(session, user_id)
    if user is None:
        raise NotFoundError("no such user in this tenant")
    if user.status == LEAVER_STATUS:
        raise AlreadyLeaverError("that user has already been offboarded")

    # Both sets are read at this one instant, before any write, so nothing the
    # loop below does can add to what it is walking. Sorted, so two runs over
    # the same rows write their events in the same order.
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
            # Decision H: somebody else emptied this role first, which is the
            # end state this loop wanted. The refusal costs the role, not the
            # departure.
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
    """the design's departure record, subject the leaver, five keys exactly.

    `actor_kind` is the caller's, which is `user` for
    everybody but the leaver sweep: a nightly job's departures have to be
    separable from a person's in the log, pinned by
    `test_access_leavers.py::test_every_event_the_sweep_causes_carries_the_system_actor_and_its_ref`.

    `revoked_grant_count` is carried beside `revoked_role_ids` rather than
    left to be inferred from it: the two differ whenever one role was held
    twice, and a log that named only the roles would understate how much
    access ended. `end_date` is the argument, ISO-rendered, and is the only
    record that a date was asked for beyond the column itself. Pinned by
    `test_access_offboard.py::test_user_offboarded_names_the_user_and_carries_exactly_five_keys`.
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
