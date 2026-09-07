"""`revoke_role_access`: the one place access is actually taken away (the design).

The mirror of `execute.py`, and the answer to what that module records as
UNRESOLVED. Nothing makes a `grants` row unique per (user, role): the engine
sees the target's `active_grant_role_ids` and deliberately does not block a
second one, because that is how a renewal has to work. So two requests under
two idempotency keys leave two live rows, and revoking "the grant" a caller
named would report success, write an audit event saying the access ended, and
leave the user still holding the role.

Hence the unit of `revoke_role_access` is the LIVE SET for one (user, role),
not one row: lock it, refuse typed when it is empty,
revoke every row in it, and append one `grant.revoked` per row. The
alternative — a unique constraint at insert — would contradict the
classification the requester was shown, so the consequence is answered on this
side instead.

There is a second entry point at a different unit, and only one writer under
both: `revoke_grant_row` ends ONE row, because a review item IS one row (plan
12e decision A). It is the loop body above, extracted, so the event and the
reason cannot drift between the two units; the set is not lost at the review's
unit but enumerated, every other live row for the pair being a separate item
of the same review. It applies no liveness filter, which is what lets a
`stale` item — `active`, expiry gone by, invisible to every liveness read — be
revoked at all.

What is NOT here: any policy decision. Classification exists to decide whether
someone may HAVE access; there is nothing to re-classify when it is being
removed, so `helpdesk.policy` is untouched by this module. The one refusal is
`GrantNotActiveError` (the design `POLICY_BLOCKED`, decision C) for a set that
is already empty. `NOT_FOUND` stays reserved for "missing, or another
tenant's", which is what keeps a grant id from being an existence oracle; the
caller (`mcp_server.tools.access.revoke_access`) owns that half, and its scope
check runs BEFORE this call so an out-of-scope id cannot be probed for its
state.

Everything runs in the caller's transaction. The revocations and their events
are flushed, never committed, so a caller that fails afterwards takes both
with it (pinned by
`test_access_revoke.py::test_the_revocations_and_their_events_roll_back_together`).
"""

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.access.directory import DirectoryProvider
from helpdesk.audit import AuditEventInput, append_event
from helpdesk.core.db import bound_tenant_id
from helpdesk.core.repository import REVOKED_GRANT_STATUS, lock_live_grants_for_role
from helpdesk.errors import ErrorCode, ToolError
from helpdesk.models import Grant

GRANT_REVOKED_EVENT_TYPE = "grant.revoked"
"""the design's own name for "access was taken away".

Appended per revoked ROW rather than declared as the caller's registry event
(decision D): the pipeline's own event carries a fixed payload and the TOOL as
its subject, so it would record that somebody called a tool and not which
grant died. the design's `offboard_user` line ("one audit event per revocation
plus a summary event") is the precedent, and the pipeline's `tool.succeeded`
is the summary. Pinned, with the subject and every payload key, by
`test_access_revoke.py::test_one_event_per_revoked_row_names_the_grant_and_the_reason`."""

_ACTOR_KIND = "user"
"""Unlike `execute_grant`, a revocation is always somebody's call: the design
gives `revoke_access` five principals, and the scheduler's expiry sweep (row
21) expires grants rather than revoking them."""

_SUBJECT_TYPE = "grant"


class GrantNotActiveError(ToolError):
    """There is no live grant left for this (user, role) — decision C.

    `POLICY_BLOCKED`, never `NOT_FOUND`: the caller has already been shown to
    reach this grant, so telling them it does not exist would be a lie, and
    `NOT_FOUND` is reserved for the two absences that must stay
    indistinguishable (missing, and another tenant's). Pinned by
    `test_access_revoke.py::test_the_revoke_errors_carry_their_spec_error_codes`.
    """

    code = ErrorCode.POLICY_BLOCKED


async def revoke_role_access(
    session: AsyncSession,
    directory: DirectoryProvider,
    *,
    user_id: uuid.UUID,
    role_id: uuid.UUID,
    reason: str,
    revoked_by_user_id: uuid.UUID,
    actor_ref: str | None = None,
    session_id: uuid.UUID | None = None,
    now: datetime,
) -> list[Grant]:
    """Revoke every live grant for this (user, role). All in the caller's transaction.

    `now` is keyword-only and supplied by the caller, so the liveness test that
    selected the set and the `revoked_at` written onto it answer for one
    instant; reading a clock here instead would let a grant that expired
    between the two be recorded as revoked. Pinned by
    `test_access_revoke.py::test_revoking_marks_the_row_revoked_with_the_reason_and_timestamp`,
    which passes a fixed instant far from wall-clock time and asserts
    `revoked_at` against it, so a clock read here fails.

    The lock is taken by `lock_live_grants_for_role` and held to the end of the
    caller's transaction, which is what makes two concurrent revocations of one
    role write one event per row rather than one per caller — the idempotency
    guard cannot help, because two callers send two different keys. Pinned end
    to end by
    `test_mcp_revoke_access.py::test_two_callers_racing_the_same_revocation_write_one_event_per_row`,
    where the loser is refused `POLICY_BLOCKED` by the empty set below.
    """
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
            actor_ref=actor_ref,
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
    revoked_by_user_id: uuid.UUID,
    actor_ref: str | None = None,
    session_id: uuid.UUID | None = None,
    revoked_count: int,
    now: datetime,
) -> Grant:
    """Revoke ONE grant row and append its `grant.revoked`. No liveness test.

    The loop body of `revoke_role_access`, extracted unchanged (an earlier change
    decision A) so that `grant.revoked` keeps exactly one writer across both
    units: the set, for a caller naming a (user, role) out of the blue, and
    the single row, for a review item, which IS one row. Its payload and its
    reason therefore cannot drift between the two. That the extraction changed
    nothing is the whole of `test_access_revoke.py` passing unedited; that the
    row unit reaches what the set unit cannot is
    `test_access_revoke.py::test_revoke_grant_row_revokes_a_lapsed_but_active_row_the_live_set_cannot_reach`,
    which revokes a lapsed-but-`active` row after asserting `revoke_role_access`
    refuses it.

    Nothing here filters on liveness, and that absence is the point: the caller
    has already chosen this row — under `lock_live_grants_for_role` for the set,
    under `lock_grant_row` for a review item — and re-testing it here would drop
    exactly the `stale` grant a review exists to end.

    `user_id` and `role_id` are read off the ROW for the same reason
    `request_id` always was: they are properties of the grant being ended, and
    a caller passing them could file the event against a different pair than
    the one that died.

    `revoked_count` is the CALLER's number, not one this function could know:
    it names how many rows the caller's own unit is ending (the live set's
    size, or 1 for a review item), so an auditor reading one event learns
    whether sibling rows went with it. Pinned at 1 for the row unit by the test
    named above and at the set's size by
    `::test_one_event_per_revoked_row_names_the_grant_and_the_reason`.
    """
    # The request the grant came from is read off the ROW, not passed in:
    # two live rows for one role can come from two different requests, and
    # copying one onto both would misfile which decision ended.
    request_id = grant.request_id
    user_id = grant.user_id
    role_id = grant.role_id
    revoked = await directory.revoke_grant(grant=grant, reason=reason, revoked_at=now)
    await append_event(
        session,
        bound_tenant_id(session),
        AuditEventInput(
            occurred_at=now,
            actor_kind=_ACTOR_KIND,
            actor_ref=actor_ref if actor_ref is not None else str(revoked_by_user_id),
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
    return revoked


__all__ = [
    "GRANT_REVOKED_EVENT_TYPE",
    "REVOKED_GRANT_STATUS",
    "GrantNotActiveError",
    "revoke_grant_row",
    "revoke_role_access",
]
