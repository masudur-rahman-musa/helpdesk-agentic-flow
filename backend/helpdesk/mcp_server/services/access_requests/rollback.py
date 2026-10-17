"""`rollback_request`: the design's undo, within 24 hours of the grant.

The mirror of `execute.py` at the REQUEST's unit. `revoke.py` answers "take
this role away from this person" and knows nothing about why; this module
answers "that decision was wrong, put it back", which is a different unit and
a different record: every row the request produced goes together, and each one
carries a link back to the request that made it so an auditor reading a dead
grant learns which decision was undone.

It writes no revocation of its own. `revoke_grant_row` — the shared writer
an earlier change extracted, and the only writer of `grant.revoked` — ends each row,
so a grant ended by a rollback reads exactly like one ended by hand to anyone
counting revocations (decision E). What this module adds beside it is one
`grant.rolled_back` per row, naming the request, the ORIGINAL `granted_at` and
`expires_at`, and the caller's own reason.

`grants.revoke_reason` gets the literal `rollback` and never the caller's text
(decision B): row 21's sweeps and row 24's admin views match that column by
value, and the caller's sentence is not lost — `grant.rolled_back` carries it.

What is NOT here: any policy decision (a rollback re-opens nothing the engine
decided), and any reinstatement. the design's other half — "revocations are
reinstated with the original expiry" — has no reachable input in this model:
`grants` is the only table with a FK to `access_requests`, so the executed
artefact of a request is always a grant and no request id can name a
revocation to put back (decision F, pinned by
`test_access_rollback.py::test_the_only_executed_artefact_of_a_request_is_a_grant`).

Every refusal lives HERE, beside the writes it guards, and not in the caller:
the package exports `rollback_request`, so a second caller (an earlier change, row 21's
sweeps, the agent API) would otherwise get the writes without the window and
undo a request granted a month ago. The caller owns the lookup and the clock
read; this module owns the status test, the window and the liveness refusal.
Everything runs in the caller's transaction, flushed and
never committed, so a caller that fails afterwards takes the revocations,
their events and the status write with it (pinned by
`test_access_rollback.py::test_the_revocations_and_their_events_roll_back_together`).
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.access.requests import set_request_status
from helpdesk.shared.access.revoke import GrantNotActiveError, revoke_grant_row
from helpdesk.shared.core.db import bound_tenant_id
from helpdesk.shared.core.errors import ErrorCode, ToolError
from helpdesk.shared.crud.grants import lock_live_grants_for_request
from helpdesk.shared.models import AccessRequest, Grant, RequestStatus
from helpdesk.shared.access.directory import DirectoryProvider
from helpdesk.shared.audit import AuditEventInput, append_event

WINDOW_HOURS = 24
"""The window's length in hours, as ONE number.

`ROLLBACK_WINDOW` is built from it, and so is every sentence that leaves the
process — the refusal message below and the tool's registry description — so
widening the window cannot leave a description telling an agent that an undo
is impossible past 24 hours."""

ROLLBACK_WINDOW = timedelta(hours=WINDOW_HOURS)
"""the design's window, measured from the grant rather than from the decision.

24 hours exactly, and the boundary is inclusive: a request granted at
`granted_at` may be rolled back at `granted_at + ROLLBACK_WINDOW` and not one
microsecond later. Pinned from both sides by
`test_access_rollback.py::test_the_rollback_window_is_inclusive_at_exactly_24_hours`,
whose table has a row that only an exclusive comparison fails and a row that
only a widened window fails."""

ROLLBACK_REVOKE_REASON = "rollback"
"""What `grants.revoke_reason` says on a row a rollback ended (decision B).

The literal, never `rollback: <caller text>`: it is the design's own word and
it stays machine-matchable, which is what lets row 21's sweeps and row 24's
admin views tell a rollback's revocations from a manual `revoke_access`, from
a transfer's (`TRANSFER_REVOKE_REASON`), from an offboard's and from a
review's. The caller's sentence goes to `grant.rolled_back` instead, so
nothing is lost. Pinned to the exact string by
`test_access_rollback.py::test_the_rollback_reason_written_to_the_grant_is_the_literal_rollback`."""

GRANT_ROLLED_BACK_EVENT_TYPE = "grant.rolled_back"
"""the design's own name for "that grant should never have happened".

Appended per revoked ROW, beside the `grant.revoked` the shared writer files,
rather than declared as the tool's registry event (decision E): the pipeline's
own event carries a fixed payload and the TOOL as its subject, so it would
record that somebody called a tool and not which grant was taken back. Its
subject is the GRANT and its payload names the request and the row's original
lifetime, pinned by
`test_access_rollback.py::test_a_rolled_back_row_writes_grant_revoked_and_grant_rolled_back`."""

ROLLBACK_STATUS = RequestStatus.EXECUTED
"""The one status a rollback can undo (criterion 5).

Every other member of the design's vocabulary is refused: a `pending`,
`approved` or `rejected` request never granted anything, a `failed` one tried
and did not, an `expired` one ended on its own terms, and a `rolled_back` one
has been undone already — which is also what the loser of a race reads, the
winner having taken `lock_request` and moved the status first. All six are
pinned, each with a live in-window grant planted so no other guard can answer,
by `test_mcp_rollback_request.py::
test_a_request_in_any_status_but_executed_is_refused_policy_blocked`."""

_ACTOR_KIND = "user"
"""A rollback is always somebody's call: the design gives the tool `it_admin` and
`platform_admin`, and no scheduled job rolls anything back."""

_SUBJECT_TYPE = "grant"


class RequestNotExecutedError(ToolError):
    """This request is in no state to be undone (criterion 5).

    `POLICY_BLOCKED`, never `NOT_FOUND`: the caller has already been shown to
    reach this request, and a `pending`, `rejected` or already `rolled_back`
    row exists — it simply has no executed effect to take back. Pinned by
    `test_access_rollback.py::test_the_rollback_errors_carry_their_spec_error_codes`.
    """

    code = ErrorCode.POLICY_BLOCKED


class RollbackWindowExpiredError(ToolError):
    """The 24 hours are up (criterion 3).

    `POLICY_BLOCKED` for the same reason: the request is real, reachable and
    executed, and what refuses the call is a policy window rather than an
    absence. Access that has outlived the window is ended by `revoke_access`,
    which is a revocation and reads as one. Pinned by
    `test_access_rollback.py::test_the_rollback_errors_carry_their_spec_error_codes`.
    """

    code = ErrorCode.POLICY_BLOCKED


@dataclass(frozen=True)
class RollbackResult:
    """The request as it now stands, and every row the undo ended."""

    request: AccessRequest
    revoked_grants: tuple[Grant, ...]


def within_rollback_window(granted_at: datetime, now: datetime) -> bool:
    """Is `now` still inside the design's 24 hours after the grant?

    Pure, and inclusive at the edge: `<=` rather than `<`, so exactly 24 hours
    after `granted_at` is the last instant a rollback is allowed. The caller
    passes the instant it read once, so the window test, the locked liveness
    read and every `revoked_at` answer together; a clock read here instead
    would let a grant that lapsed between two of them be recorded as rolled
    back.

    Both sides of the boundary are pinned, one microsecond apart, by
    `test_access_rollback.py::test_the_rollback_window_is_inclusive_at_exactly_24_hours`.
    """
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
    """Undo one executed request: every live row it produced, and its status.

    `now` is keyword-only and supplied by the caller for the reason above: the
    window test, the liveness read that selects the set, every `revoked_at`
    written onto it and both events answer for one instant.

    Three refusals, in this order, before anything is written:

    `RequestNotExecutedError` — the status is anything but `executed`. Its own
    case plants a live, in-window grant under each of the design's other six
    statuses, so neither the window nor the liveness refusal can answer
    (`test_mcp_rollback_request.py::test_a_request_in_any_status_but_executed_is_refused_policy_blocked`).

    `GrantNotActiveError` — nothing live is left (decision C). Tested before
    the window because a request with no live grant has no `granted_at` to
    measure a window against
    (`test_access_rollback.py::test_a_request_whose_grants_are_all_gone_raises_grant_not_active`).

    `RollbackWindowExpiredError` — the earliest live grant is older than
    `ROLLBACK_WINDOW`, pinned at the transport by
    `test_mcp_rollback_request.py::test_a_request_granted_twenty_five_hours_ago_is_refused_policy_blocked`
    and on the multi-row half by
    `::test_the_window_is_measured_from_the_earliest_grant_of_the_request`.

    The locked read is the liveness test (decision C). An empty set is
    `GrantNotActiveError` — the same typed refusal `revoke_access` raises,
    reused rather than duplicated, because "there is nothing left to take
    away" is one situation whatever asked — and it is raised BEFORE any write,
    so a request whose grants were already revoked or expired keeps its
    `executed` status rather than recording an undo that undid nothing (pinned
    by `test_access_rollback.py::test_a_request_whose_grants_are_all_gone_raises_grant_not_active`).

    Order of effects: each row is revoked and linked, then the request moves
    to `rolled_back` last, before the pipeline's `tool.succeeded`. The status
    write goes last so the row still reads `executed` for as long as anything
    can fail — a status written first and a revocation that then raised would
    leave a request marked undone whose access was still live, which is the
    one inconsistency an auditor cannot see.
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
    # `grants[0]` is the EARLIEST: the locked read is ordered by `granted_at`.
    # Measuring from the newest would admit an undo of access a day past its
    # window whenever one request left several rows (decision A).
    if not within_rollback_window(grants[0].granted_at, now):
        raise RollbackWindowExpiredError(
            f"access request {request.id} was granted at {grants[0].granted_at.isoformat()}, "
            f"which is more than {WINDOW_HOURS} hours ago"
        )

    revoked: list[Grant] = []
    for grant in grants:
        # Read before the revocation, though nothing in it touches these two:
        # the payload's job is to say what the grant WAS, and taking the
        # values off the pre-revocation row is what keeps that true whatever
        # the writer starts doing to the column.
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
    """One `grant.rolled_back` for one row: what died, and what it had been.

    The subject is the GRANT rather than the request, so an auditor asking
    what happened to one grant reads its whole end here — the revocation, and
    the decision that was undone — instead of having to find a request-level
    event and work back. `request_id` is the link the other way.

    `granted_at` and `expires_at` are the row's ORIGINAL lifetime, which is
    what the design's reinstatement half would need if a request could ever
    name a revocation (decision F), and what tells a reader how much access
    actually existed and for how long. `reason` is the CALLER's text: the
    column carries the literal `rollback` (decision B), so this is the only
    record of why. `rolled_back_count` names how many rows went together, so
    one event says whether siblings died with it.

    Subject and every payload key pinned by
    `test_access_rollback.py::test_a_rolled_back_row_writes_grant_revoked_and_grant_rolled_back`,
    and the nullable expiry by
    `::test_a_rolled_back_row_carries_a_null_expiry_when_the_grant_had_none`.
    """
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
