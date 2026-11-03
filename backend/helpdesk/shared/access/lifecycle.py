"""Types, constants and helpers shared by onboarding, transfers and offboarding."""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.access.directory import DirectoryProvider, DirectoryReader
from helpdesk.shared.access.execute import (
    PolicyBlockedAtExecutionError,
    SodConflictAtExecutionError,
    reclassify,
)
from helpdesk.shared.auth.rbac import Principal
from helpdesk.shared.core.errors import ErrorCode, ToolError
from helpdesk.shared.models import Grant, User
from helpdesk.shared.policy.types import TargetStatus

USER_ONBOARDED_EVENT_TYPE = "user.onboarded"
"""The audit event for "this person was hired".

Written here with the new user as subject. The tool pipeline's own event
names only the tool, so it would not say who was hired. The same holds for
the offboard and transfer events below."""

UNUSABLE_PASSWORD_HASH = "!no-password-set:onboarded-user-has-no-credential"
"""What a joiner's NOT NULL `password_hash` holds until they set one.

Not a valid argon2 value, so no password can ever match it, and it is easy
to spot in the table, unlike a random real hash."""

JOINER_PRINCIPALS: tuple[str, ...] = (Principal.EMPLOYEE.value,)
"""What every joiner gets, fixed so a caller cannot create an `it_admin`."""

JOINER_STATUS = TargetStatus.ACTIVE.value
"""A joiner is `active`; `LEAVER_STATUS` below is the other direction."""

LEAVER_STATUS = TargetStatus.LEAVER.value
"""What `users.status` says once somebody has left. Taken from the same value
the policy engine's leaver rule checks, so the two cannot drift."""

USER_OFFBOARDED_EVENT_TYPE = "user.offboarded"
"""The audit event for "this person left", with the whole departure as payload."""

OFFBOARD_REVOKE_REASON = "offboarded: the user left the organisation"
"""The `grants.revoke_reason` on every revoke an offboard makes, fixed so it can be searched."""

USER_TRANSFERRED_EVENT_TYPE = "user.transferred"
"""The audit event for "this person moved department", with the whole change as payload."""

TRANSFER_REVOKE_REASON = "transferred out of the department that granted this role"
"""The `grants.revoke_reason` on every revoke a transfer makes, fixed so it can be searched."""


class SameDepartmentError(ToolError):
    """The mover is already in the target department.

    Raised before any write, so no event records a move that never happened.
    It also refuses the loser of a race: the second caller waits on the user
    lock, then sees the department the winner wrote.
    """

    code = ErrorCode.POLICY_BLOCKED
    reason = "The person is already in that department."


class LeaverTransferError(ToolError):
    """The mover is a `leaver`, so there is no move to make.

    Raised before any revoke. The engine would refuse every new role anyway,
    but the revokes would still run and strip the leaver's access under an
    event saying they moved.
    """

    code = ErrorCode.POLICY_BLOCKED
    reason = "The person has left the company, so there is no move to make."


class AlreadyLeaverError(ToolError):
    """The target has already been offboarded.

    Raised before any write. A second offboard would write an event with
    empty lists that read as if the person had held no access at all.
    """

    code = ErrorCode.POLICY_BLOCKED
    reason = "The person has already left the company."


_ACTOR_KIND = "user"
"""Lifecycle changes are always a person's call."""

_SUBJECT_TYPE = "user"


class DuplicateEmailError(ToolError):
    """This tenant already has a user with that address.

    Checked by a read before any write. Leaving it to the unique index would
    report an `INTERNAL` error, telling the caller to retry something that
    can never work.
    """

    code = ErrorCode.POLICY_BLOCKED
    reason = "Someone in this company already has that email address."


@dataclass(frozen=True)
class SkippedRole:
    """One template role the engine refused, and why."""

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
    """The mover and the four role sets; each role is in exactly one.

    Retained and non-standard roles were left alone, but the caller still
    needs to tell "kept because the new template names it" from "kept
    because no template does", since a review will flag the second.
    `from_department_id` is None for a person who had no department.
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

    The grant count counts rows, which differs from the roles when one role was held twice.
    """

    user: User
    revoked_role_ids: tuple[uuid.UUID, ...]
    revoked_grant_count: int
    closed_request_ids: tuple[uuid.UUID, ...]


class _WithDecidedRoles:
    """The directory, answering as if this onboarding's decided roles were live.

    A conflict inside a template only shows once the first of the two roles
    is held. Deciding against untouched data would miss it, so this view
    adds each role as it is accepted. It has no write methods.
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

    Only classifies; nothing is written. Uses `reclassify`, the same code
    that guards the real grant, so the two cannot drift.
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
    "_ACTOR_KIND",
    "_SUBJECT_TYPE",
    "AlreadyLeaverError",
    "DuplicateEmailError",
    "LeaverTransferError",
    "OffboardResult",
    "OnboardResult",
    "SameDepartmentError",
    "SkippedRole",
    "TransferResult",
    "_WithDecidedRoles",
    "_decide",
]
