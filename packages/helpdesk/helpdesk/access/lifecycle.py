"""The joiner and mover halves of the design's lifecycle (requirement #15).

A joiner is two facts, not one: a person exists, and that person holds what
their department's template says they should. The second is access, so it is
not written here — every role goes through `helpdesk.access.execute_grant`, which re-runs the whole policy engine per role. That
is what makes onboarding obey the design like every other path to a grant:
the expiry a joiner gets is the one the engine computed, never the role's raw
`default_expiry_days`, and a template naming two roles that cannot be held
together is refused rather than granted (pinned by
`test_access_lifecycle.py::test_the_whole_template_is_applied_with_the_engines_expiry_not_the_roles`
and `::test_a_template_role_in_sod_conflict_is_skipped_and_the_rest_stand`).

A refused template role costs that role, not the hire (decision B): it comes
back in `skipped` carrying its the design code, and the joiner keeps the rest.
A broken template is a data defect somebody has to see, and leaving a new
hire with nothing on their first day is the worse of the two failures.

Two passes over the template, which the audit order forces. the design's
`user.onboarded` has to land BEFORE the grants — access recorded against a
person the log has not hired yet reads backwards — and its payload names
which roles were applied and which were skipped, neither of which is known
until the engine has been asked. So the first pass DECIDES (classify only, no
writes) and the second EXECUTES. The two cannot disagree: the deciding pass
runs the same `reclassify` the executing pass runs, in the same sorted role
order, over a directory view that reports the roles already decided as held —
which is exactly what the executing pass will read from the grants it has
written by then. Pinned end to end by
`test_mcp_onboard_user.py::test_the_audit_sequence_is_onboarded_then_the_grants_then_tool_succeeded`.

Everything runs in the caller's transaction: the person, their grants and
every event are flushed, never committed, so a failure afterwards takes all
of them (pinned by `test_access_lifecycle.py::
test_a_duplicate_email_is_refused_before_any_write`).

`transfer_user` is the mover half, and it decides nothing either:
`revoke_role_access` takes access away, `execute_grant` gives it, and this
module only works out WHICH roles each half gets. That diff is four sets
computed at one instant against the target's live grants and the two
department templates — revoke, apply, retain, and the non-standard grants
that belong to neither template and are left alone (decision H).

One pass, unlike onboarding: the design's `user.transferred` lands AFTER the
grant events (decision C) because the person already exists, so nothing has
to be recorded before the engine has been asked and the classify-only pass
above has nothing to buy here.

`offboard_user` is the leaver half, and it decides least of all:
there is nothing to classify when access is being taken away, so it composes
`revoke_role_access` over every role the person holds and `set_request_status`
over every request still open against them. The `status = leaver` write goes
FIRST (decision D), because the engine blocks every new grant for a leaver
(the design): a concurrent `create_access_request` reading the row after it is
refused rather than granting access to somebody mid-departure. It narrows
that window without closing it — `lock_user` serialises offboards and
transfers, not request creation — and row 21's leaver sweep is the backstop.
"""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.access.directory import DirectoryProvider, DirectoryReader
from helpdesk.access.execute import (
    PolicyBlockedAtExecutionError,
    SodConflictAtExecutionError,
    execute_grant,
    reclassify,
)
from helpdesk.access.requests import (
    append_request_rejected,
    lock_open_requests_for_target,
    set_request_status,
)
from helpdesk.access.revoke import GrantNotActiveError, revoke_role_access
from helpdesk.audit import AuditEventInput, append_event
from helpdesk.auth.rbac import Principal
from helpdesk.core.db import bound_tenant_id
from helpdesk.core.repository import Repository, find_user_by_email, lock_user
from helpdesk.errors import ErrorCode, NotFoundError, ToolError
from helpdesk.models import Grant, RequestStatus, User
from helpdesk.policy.types import TargetStatus

USER_ONBOARDED_EVENT_TYPE = "user.onboarded"
"""the design's own name for "this person was hired".

Appended by this module with the NEW USER as its subject (decision F), not
declared as the calling tool's registry event: the pipeline's event carries
the TOOL as subject and a fixed payload, so it would record that somebody
called a tool and not who was hired. The subject and the
exact payload key set are pinned by `test_access_lifecycle.py::
test_user_onboarded_names_the_new_user_and_carries_exactly_six_keys` and
again through the transport by `test_mcp_onboard_user.py::
test_the_audit_sequence_is_onboarded_then_the_grants_then_tool_succeeded`."""

UNUSABLE_PASSWORD_HASH = "!no-password-set:onboarded-user-has-no-credential"
"""What a joiner's NOT NULL `password_hash` holds until they set one.

Deliberately not a valid argon2 encoding (decision C): `verify_password`
answers False for a malformed hash, so NO input can match this value, and the
constant is greppable in the table — a random real hash would be unmatchable
too, but indistinguishable from a genuine credential. Pinned by
`test_access_lifecycle.py::test_the_unusable_password_hash_verifies_against_nothing`
and, end to end, by `test_mcp_onboard_user.py::
test_login_refuses_an_onboarded_user_exactly_like_an_unknown_one`, which
sends this very string as the password."""

JOINER_PRINCIPALS: tuple[str, ...] = (Principal.EMPLOYEE.value,)
"""What every joiner gets, fixed rather than taken from the input (decision D).

the design's signature has no principals field, and an HR caller able to name
them could mint an `it_admin` through a lifecycle tool — a privilege
escalation path around the design entirely. Pinned as the exact tuple by
`test_access_lifecycle.py::test_the_onboard_constants_and_error_code_are_exactly_the_spec_values`
and read off the created row by `test_mcp_onboard_user.py::
test_the_three_permitted_principals_each_onboard_a_joiner`."""

JOINER_STATUS = TargetStatus.ACTIVE.value
"""A joiner is `active`; `LEAVER_STATUS` below is the other direction."""

LEAVER_STATUS = TargetStatus.LEAVER.value
"""What `users.status` says once somebody has left.

Read off `TargetStatus`, which is what the policy engine's the design leaver
rule tests, so the status an offboard writes and the status that blocks every
future grant cannot drift into two strings. Pinned to the exact value by
`test_access_offboard.py::test_the_offboard_constants_and_error_code_are_exactly_the_spec_values`
and read off the row by `::test_the_status_becomes_leaver_and_the_end_date_is_the_argument`."""

USER_OFFBOARDED_EVENT_TYPE = "user.offboarded"
"""the design's own name for "this person left".

Appended by this module with the LEAVER as its subject and the whole
departure as its payload, not declared as the calling tool's registry event
(decision C's shape, shared with 12a and 12b): the pipeline's event carries
the TOOL as subject and a fixed payload, so it would record that somebody
called a tool and not who left, what they lost, or what was closed against
them. The subject and the exact five payload keys are
pinned by `test_access_offboard.py::
test_user_offboarded_names_the_user_and_carries_exactly_five_keys` and again
through the transport by `test_mcp_offboard_user.py::
test_the_audit_sequence_is_revoked_then_rejected_then_offboarded_then_succeeded`."""

OFFBOARD_REVOKE_REASON = "offboarded: the user left the organisation"
"""What `grants.revoke_reason` says on every revocation an offboard makes.

A fixed sentence rather than a caller-supplied one (decision I) so an auditor
can grep the column for departures and tell them from a manual
`revoke_access` (free text) and from a transfer's
`TRANSFER_REVOKE_REASON`. It is also what the closure event carries, so an
offboard's rejected requests are tellable from a person's. Pinned to the
exact string by `test_access_offboard.py::
test_the_offboard_constants_and_error_code_are_exactly_the_spec_values` and
read off the revoked row by
`::test_every_live_grant_is_revoked_and_the_count_is_rows_not_roles`."""

USER_TRANSFERRED_EVENT_TYPE = "user.transferred"
"""the design's own name for "this person moved department".

Appended by this module with the MOVER as its subject and the whole diff as
its payload, not declared as the calling tool's registry event (decision C):
the pipeline's event carries the TOOL as subject and a fixed payload, so it
would record that somebody called a tool and not who moved or what they
gained and lost. The subject and the exact nine payload
keys are pinned by `test_access_transfer.py::
test_user_transferred_names_the_user_and_carries_exactly_nine_keys` and again
through the transport by `test_mcp_transfer_user.py::
test_the_audit_sequence_is_revoked_then_executed_then_transferred_then_succeeded`."""

TRANSFER_REVOKE_REASON = "transferred out of the department that granted this role"
"""What `grants.revoke_reason` says on every revocation a transfer makes.

A fixed sentence rather than a caller-supplied one so an auditor can grep the
column for the moves and tell them from a manual `revoke_access`. Pinned to
the exact string by `test_access_transfer.py::
test_the_transfer_constants_and_error_codes_are_exactly_the_spec_values` and
read off the revoked row by
`::test_the_diff_revokes_applies_retains_and_leaves_the_non_standard_grant`."""


class SameDepartmentError(ToolError):
    """The mover is already in the department they were asked to move to.

    `POLICY_BLOCKED`, and raised BEFORE any write (decision E). The diff is
    empty by construction, so nothing about the tables differs either way and
    the only trace of the alternative is a `user.transferred` recording a move
    that never happened. The code is pinned by `test_access_transfer.py::
    test_the_transfer_constants_and_error_codes_are_exactly_the_spec_values`
    and the "before any write" half by
    `::test_a_transfer_into_the_same_department_is_refused_before_any_write`.

    It is also what refuses the loser of a race: the second caller waits on
    `lock_user`, then re-reads the department the winner wrote (pinned by
    `test_mcp_transfer_user.py::test_two_callers_racing_the_same_transfer_leave_one_move`).
    """

    code = ErrorCode.POLICY_BLOCKED


class LeaverTransferError(ToolError):
    """The mover is a `leaver`, so there is no move to make (decision F).

    `POLICY_BLOCKED`, and raised BEFORE any revocation. The engine blocks
    every new grant for a leaver on its own (the design), so letting one
    through would report each arriving role in `skipped` and look harmless —
    but a revocation is not a policy decision and would still run, stripping
    the leaver's remaining access under an event that says they moved. Pinned
    by `test_access_transfer.py::test_a_leaver_target_is_refused_before_any_revocation`,
    which asserts the old grant STILL LIVE; the `skipped`-only alternative
    leaves it revoked.
    """

    code = ErrorCode.POLICY_BLOCKED


class AlreadyLeaverError(ToolError):
    """The target has already been offboarded, so there is no departure to make.

    `POLICY_BLOCKED`, and raised BEFORE any write (decision F). A second
    offboard has nothing left to revoke, so succeeding would look harmless and
    still write a `user.offboarded` recording a departure that already
    happened, with empty lists that read like the person had held no access at
    all. Row 21's leaver sweep selects `status = active`, so it never reaches
    this. Pinned by `test_access_offboard.py::
    test_an_already_leaver_target_is_refused_before_any_write`, which asserts
    the leaver's grant STILL LIVE and their open request still `pending`, and
    the code by
    `::test_the_offboard_constants_and_error_code_are_exactly_the_spec_values`.
    """

    code = ErrorCode.POLICY_BLOCKED


_ACTOR_KIND = "user"
"""Onboarding is always somebody's call: the design gives the row three
principals, unlike `execute_grant`, which is system-only."""

_SUBJECT_TYPE = "user"


class DuplicateEmailError(ToolError):
    """This tenant already has a user with that address (decision G).

    `POLICY_BLOCKED`, and raised from a read taken BEFORE any write: letting
    the unique index raise instead surfaces an `IntegrityError`, which the
    pipeline maps to `INTERNAL` — a caller-caused condition reported as a
    server fault, telling the caller to retry the one thing that cannot
    work. The code is pinned by `test_access_lifecycle.py::
    test_the_onboard_constants_and_error_code_are_exactly_the_spec_values`
    and the "before any write" half by
    `::test_a_duplicate_email_is_refused_before_any_write`.
    """

    code = ErrorCode.POLICY_BLOCKED


@dataclass(frozen=True)
class SkippedRole:
    """One template role the engine refused, and why (decision B)."""

    role_id: uuid.UUID
    reason_code: ErrorCode
    reason: str


@dataclass(frozen=True)
class OnboardResult:
    """The person, what they hold, and what their template failed to give them."""

    user: User
    applied: tuple[Grant, ...]
    skipped: tuple[SkippedRole, ...]


@dataclass(frozen=True)
class TransferResult:
    """The mover and the four sets of §1, each role in exactly one of them.

    `retained_role_ids` and `non_standard_role_ids` name roles NOTHING was
    done to, and they are part of the answer rather than a detail: a caller
    who cannot tell "kept because the new template names it" from "kept
    because no template does" cannot see what an access review will later
    flag (decision H). `from_department_id` is nullable because a person with
    no department transfers additively.
    """

    user: User
    from_department_id: uuid.UUID | None
    revoked_role_ids: tuple[uuid.UUID, ...]
    applied_role_ids: tuple[uuid.UUID, ...]
    skipped: tuple[SkippedRole, ...]
    retained_role_ids: tuple[uuid.UUID, ...]
    non_standard_role_ids: tuple[uuid.UUID, ...]


@dataclass(frozen=True)
class OffboardResult:
    """The person who left, what was taken away, and what was closed.

    `revoked_grant_count` counts ROWS and `revoked_role_ids` counts ROLES, and
    the two differ whenever one role was held twice: nothing makes a grant
    unique per (user, role), so `revoke_role_access` takes the whole live set
    and a caller told only "3 roles" cannot tell how much access ended
    (pinned by `test_access_offboard.py::
    test_every_live_grant_is_revoked_and_the_count_is_rows_not_roles`).
    """

    user: User
    revoked_role_ids: tuple[uuid.UUID, ...]
    revoked_grant_count: int
    closed_request_ids: tuple[uuid.UUID, ...]


class _WithDecidedRoles:
    """The directory, answering as if this onboarding's decided roles were live.

    The deciding pass exists because `user.onboarded` must name the applied
    and skipped roles before either grant is written. A pass that classified
    every template role against untouched state would see the joiner holding
    nothing and find no conflict at all — the conflict in a broken template is
    between two roles OF that template, and only appears once the first of
    them is held. This wrapper supplies exactly what the executing pass will
    have written by the same point, so the two passes cannot disagree.

    Read-only by construction: it forwards the three read methods the snapshot
    builder uses and implements no mutation half, so the deciding pass cannot
    write through it even by mistake.
    """

    def __init__(self, inner: DirectoryReader, user_id: uuid.UUID) -> None:
        self._inner = inner
        self._user_id = user_id
        self._decided: set[uuid.UUID] = set()

    def add(self, role_id: uuid.UUID) -> None:
        self._decided.add(role_id)

    async def lookup_user(self, user_id: uuid.UUID) -> User | None:
        return await self._inner.lookup_user(user_id)

    async def list_membership_role_ids(self, user_id: uuid.UUID) -> frozenset[uuid.UUID]:
        return await self._inner.list_membership_role_ids(user_id)

    async def list_account_role_ids(
        self, user_id: uuid.UUID, now: datetime
    ) -> frozenset[uuid.UUID]:
        held = await self._inner.list_account_role_ids(user_id, now)
        if user_id != self._user_id:
            return held
        return held | self._decided


async def onboard_user(
    session: AsyncSession,
    directory: DirectoryProvider,
    *,
    email: str,
    display_name: str,
    department_id: uuid.UUID,
    start_date: date,
    manager_user_id: uuid.UUID,
    created_by_user_id: uuid.UUID,
    actor_ref: str | None = None,
    session_id: uuid.UUID | None = None,
    now: datetime,
) -> OnboardResult:
    """Create the person, apply their department's template, record the hire.

    The caller has already resolved `department_id` and `manager_user_id`
    inside its own tenant and refused either absence `NOT_FOUND`
    (`mcp_server.tools.lifecycle.onboard_user`); what is left here is the
    domain decision. `manager_user_id` is carried into the event and nowhere
    else: the design models management on `departments`, so `users` has no
    manager column to write it to (decision E).

    `now` is keyword-only and supplied by the caller, so the tenure the engine
    reads, every classification and every `granted_at` answer for one instant;
    reading a clock here would let two template roles be classified against
    two different moments.
    """
    if await find_user_by_email(session, email) is not None:
        raise DuplicateEmailError("a user with that email already exists in this tenant")

    user = await directory.create_user(
        email=email,
        display_name=display_name,
        department_id=department_id,
        start_date=start_date,
        principals=JOINER_PRINCIPALS,
        password_hash=UNUSABLE_PASSWORD_HASH,
        status=JOINER_STATUS,
    )

    # Sorted, so the deciding pass and the executing pass walk the template in
    # the same order. Which half of a conflicting pair loses is then a
    # property of the data rather than of the order a set happened to iterate.
    template_role_ids = sorted(await directory.list_department_role_ids(department_id))
    to_apply, skipped = await _decide(
        session,
        directory,
        user_id=user.id,
        created_by_user_id=created_by_user_id,
        template_role_ids=template_role_ids,
        now=now,
    )

    await _append_onboarded(
        session,
        user=user,
        email=email,
        department_id=department_id,
        manager_user_id=manager_user_id,
        applied_role_ids=to_apply,
        skipped=skipped,
        actor_ref=actor_ref if actor_ref is not None else str(created_by_user_id),
        session_id=session_id,
        now=now,
    )

    applied = [
        await execute_grant(
            session,
            directory,
            request_id=None,
            requester_user_id=created_by_user_id,
            target_user_id=user.id,
            role_id=role_id,
            requested_expiry_days=None,
            granted_by_user_id=created_by_user_id,
            actor_ref=actor_ref if actor_ref is not None else str(created_by_user_id),
            session_id=session_id,
            now=now,
        )
        for role_id in to_apply
    ]
    return OnboardResult(user=user, applied=tuple(applied), skipped=tuple(skipped))


async def transfer_user(
    session: AsyncSession,
    directory: DirectoryProvider,
    *,
    user_id: uuid.UUID,
    new_department_id: uuid.UUID,
    effective_date: date,
    transferred_by_user_id: uuid.UUID,
    actor_ref: str | None = None,
    session_id: uuid.UUID | None = None,
    now: datetime,
) -> TransferResult:
    """Move one person between departments and diff the two templates.

    The caller has already resolved `new_department_id` inside its own tenant
    and refused an unknown one `NOT_FOUND`
    (`mcp_server.tools.lifecycle.transfer_user`); `lock_user` below answers
    None for a user id that is missing OR another tenant's, so the same
    `NOT_FOUND` covers both here (pinned by `test_mcp_transfer_user.py::
    test_a_missing_and_another_tenants_user_answer_the_same_not_found`).

    The lock comes FIRST, before either refusal, because both of them read
    state a concurrent transfer can change: the department the
    same-department check compares against is exactly what the winner of a
    race rewrites (decision D).

    Order of effects, and each part of it is a decision:

    1. revoke, then apply (decision B) — an SoD pair spanning the two
       departments then resolves in favour of the department the person is
       moving INTO, which is the point of a transfer;
    2. the department is written BEFORE the applications (decision A), because
       `build_request_snapshot` reads `role_in_department_template` off the
       target's CURRENT department: an application classified before the write
       sees every arriving role as out-of-template and caps its expiry as
       `elevated` (pinned by
       `test_access_transfer.py::test_the_applications_are_classified_against_the_new_department`,
       the only case that flips if the write moves);
    3. `user.transferred` lands after both sets of grant events (decision C),
       because every set it names is known only by then.

    `now` is keyword-only and supplied by the caller, so the four sets, every
    classification, each `revoked_at` and each `granted_at` answer for one
    instant; reading a clock here would let a grant be diffed against one
    moment and revoked at another.

    Everything runs in the caller's transaction. Decision B leaves an instant
    where the mover holds neither the departing role nor the arriving one; it
    is inside that single transaction, so no other reader ever sees it
    (pinned by `test_access_transfer.py::test_the_transfer_and_its_events_roll_back_together`).
    """
    user = await lock_user(session, user_id)
    if user is None:
        raise NotFoundError("no such user in this tenant")
    if user.status == TargetStatus.LEAVER.value:
        raise LeaverTransferError("a leaver cannot be transferred")
    if user.department_id == new_department_id:
        raise SameDepartmentError("that user is already in this department")

    from_department_id = user.department_id
    held = await directory.list_account_role_ids(user_id, now)
    old_template = (
        frozenset()
        if from_department_id is None
        else await directory.list_department_role_ids(from_department_id)
    )
    new_template = await directory.list_department_role_ids(new_department_id)

    # The four sets of §1, computed together at this one instant so no role can
    # fall into two of them or into none. Sorted, so two runs over the same
    # data write their events in the same order.
    to_revoke = sorted((held & old_template) - new_template)
    retained = sorted(held & new_template)
    to_apply = sorted(new_template - held)
    non_standard = sorted(held - old_template - new_template)

    for role_id in to_revoke:
        await revoke_role_access(
            session,
            directory,
            user_id=user_id,
            role_id=role_id,
            reason=TRANSFER_REVOKE_REASON,
            revoked_by_user_id=transferred_by_user_id,
            actor_ref=actor_ref if actor_ref is not None else str(transferred_by_user_id),
            session_id=session_id,
            now=now,
        )

    # Before the applications, never after (decision A).
    await Repository(session, User).update(user_id, department_id=new_department_id)

    applied: list[uuid.UUID] = []
    skipped: list[SkippedRole] = []
    for role_id in to_apply:
        try:
            await execute_grant(
                session,
                directory,
                request_id=None,
                requester_user_id=transferred_by_user_id,
                target_user_id=user_id,
                role_id=role_id,
                requested_expiry_days=None,
                granted_by_user_id=transferred_by_user_id,
                actor_ref=actor_ref if actor_ref is not None else str(transferred_by_user_id),
                session_id=session_id,
                now=now,
            )
        except (SodConflictAtExecutionError, PolicyBlockedAtExecutionError) as refusal:
            # A refused role costs that role, not the move (§1, criterion 5):
            # a broken template is a data defect somebody has to see, and
            # leaving a mover in their new department with nothing is worse.
            skipped.append(
                SkippedRole(role_id=role_id, reason_code=refusal.code, reason=str(refusal))
            )
            continue
        applied.append(role_id)

    await _append_transferred(
        session,
        user_id=user_id,
        from_department_id=from_department_id,
        to_department_id=new_department_id,
        effective_date=effective_date,
        revoked_role_ids=to_revoke,
        applied_role_ids=applied,
        skipped=skipped,
        retained_role_ids=retained,
        non_standard_role_ids=non_standard,
        actor_ref=actor_ref if actor_ref is not None else str(transferred_by_user_id),
        session_id=session_id,
        now=now,
    )
    return TransferResult(
        user=user,
        from_department_id=from_department_id,
        revoked_role_ids=tuple(to_revoke),
        applied_role_ids=tuple(applied),
        skipped=tuple(skipped),
        retained_role_ids=tuple(retained),
        non_standard_role_ids=tuple(non_standard),
    )


async def _append_transferred(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    from_department_id: uuid.UUID | None,
    to_department_id: uuid.UUID,
    effective_date: date,
    revoked_role_ids: list[uuid.UUID],
    applied_role_ids: list[uuid.UUID],
    skipped: list[SkippedRole],
    retained_role_ids: list[uuid.UUID],
    non_standard_role_ids: list[uuid.UUID],
    actor_ref: str,
    session_id: uuid.UUID | None,
    now: datetime,
) -> None:
    """the design's move record, subject the mover, nine keys exactly.

    `effective_date` is here and nowhere else (decision G): the design puts it
    in the signature, there is no column for it and no scheduler until row 21,
    so the transfer takes effect now and this payload is the only record that
    a date was asked for. `non_standard_role_ids` is likewise decision H's
    only record — nothing flags those grants until an access review runs.
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
            event_type=USER_TRANSFERRED_EVENT_TYPE,
            subject_type=_SUBJECT_TYPE,
            subject_id=str(user_id),
            payload={
                "user_id": str(user_id),
                "from_department_id": (
                    None if from_department_id is None else str(from_department_id)
                ),
                "to_department_id": str(to_department_id),
                "effective_date": effective_date.isoformat(),
                "revoked_role_ids": [str(role_id) for role_id in revoked_role_ids],
                "applied_role_ids": [str(role_id) for role_id in applied_role_ids],
                "skipped_role_ids": [str(record.role_id) for record in skipped],
                "retained_role_ids": [str(role_id) for role_id in retained_role_ids],
                "non_standard_role_ids": [str(role_id) for role_id in non_standard_role_ids],
            },
        ),
    )


async def offboard_user(
    session: AsyncSession,
    directory: DirectoryProvider,
    *,
    user_id: uuid.UUID,
    end_date: date,
    offboarded_by_user_id: uuid.UUID,
    actor_ref: str | None = None,
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
    """
    user = await lock_user(session, user_id)
    if user is None:
        raise NotFoundError("no such user in this tenant")
    if user.status == LEAVER_STATUS:
        raise AlreadyLeaverError("that user has already been offboarded")

    resolved_actor = actor_ref if actor_ref is not None else str(offboarded_by_user_id)
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
    session_id: uuid.UUID | None,
    now: datetime,
) -> None:
    """the design's departure record, subject the leaver, five keys exactly.

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
            actor_kind=_ACTOR_KIND,
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


async def _decide(
    session: AsyncSession,
    directory: DirectoryProvider,
    *,
    user_id: uuid.UUID,
    created_by_user_id: uuid.UUID,
    template_role_ids: Iterable[uuid.UUID],
    now: datetime,
) -> tuple[list[uuid.UUID], list[SkippedRole]]:
    """Which template roles the engine will take, and why it refuses the rest.

    Classification only — no grant is written and no event is appended, so
    running the engine here costs queries and nothing else. `reclassify` is
    imported from `execute.py` rather than reimplemented: which of the two
    the design codes a refusal carries is decided by the order that function
    tests conflicts and decisions in, and a second copy of that order here
    would be free to drift from the one that actually writes the grants.
    """
    view = _WithDecidedRoles(directory, user_id)
    to_apply: list[uuid.UUID] = []
    skipped: list[SkippedRole] = []
    for role_id in template_role_ids:
        try:
            await reclassify(
                session,
                view,
                requester_user_id=created_by_user_id,
                target_user_id=user_id,
                role_id=role_id,
                requested_expiry_days=None,
                now=now,
            )
        except (SodConflictAtExecutionError, PolicyBlockedAtExecutionError) as refusal:
            skipped.append(
                SkippedRole(role_id=role_id, reason_code=refusal.code, reason=str(refusal))
            )
            continue
        to_apply.append(role_id)
        view.add(role_id)
    return to_apply, skipped


async def _append_onboarded(
    session: AsyncSession,
    *,
    user: User,
    email: str,
    department_id: uuid.UUID,
    manager_user_id: uuid.UUID,
    applied_role_ids: list[uuid.UUID],
    skipped: list[SkippedRole],
    actor_ref: str,
    session_id: uuid.UUID | None,
    now: datetime,
) -> None:
    """the design's hire record, subject the new user, six keys exactly."""
    await append_event(
        session,
        bound_tenant_id(session),
        AuditEventInput(
            occurred_at=now,
            actor_kind=_ACTOR_KIND,
            actor_ref=actor_ref,
            session_id=session_id,
            trace_id=None,
            event_type=USER_ONBOARDED_EVENT_TYPE,
            subject_type=_SUBJECT_TYPE,
            subject_id=str(user.id),
            payload={
                "user_id": str(user.id),
                "email": email,
                "department_id": str(department_id),
                "manager_user_id": str(manager_user_id),
                "applied_role_ids": [str(role_id) for role_id in applied_role_ids],
                "skipped_role_ids": [str(record.role_id) for record in skipped],
            },
        ),
    )


__all__ = [
    "JOINER_PRINCIPALS",
    "JOINER_STATUS",
    "LEAVER_STATUS",
    "OFFBOARD_REVOKE_REASON",
    "TRANSFER_REVOKE_REASON",
    "UNUSABLE_PASSWORD_HASH",
    "USER_OFFBOARDED_EVENT_TYPE",
    "USER_ONBOARDED_EVENT_TYPE",
    "USER_TRANSFERRED_EVENT_TYPE",
    "AlreadyLeaverError",
    "DuplicateEmailError",
    "LeaverTransferError",
    "OffboardResult",
    "OnboardResult",
    "SameDepartmentError",
    "SkippedRole",
    "TransferResult",
    "offboard_user",
    "onboard_user",
    "transfer_user",
]
