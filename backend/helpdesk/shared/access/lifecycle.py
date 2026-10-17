"""Shared types, constants and helpers for the three lifecycle operations: onboarding, transfers and offboarding."""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.access.execute import (
    PolicyBlockedAtExecutionError,
    SodConflictAtExecutionError,
    reclassify,
)
from helpdesk.shared.access.directory import DirectoryProvider, DirectoryReader
from helpdesk.shared.auth.rbac import Principal
from helpdesk.shared.core.errors import ErrorCode, ToolError
from helpdesk.shared.models import Grant, User
from helpdesk.shared.policy.types import TargetStatus

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
    "_ACTOR_KIND",
    "_SUBJECT_TYPE",
    "_WithDecidedRoles",
    "_decide",
]
