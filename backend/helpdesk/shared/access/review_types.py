"""Types, constants and pure helpers for access reviews."""

import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from helpdesk.shared.core.errors import ErrorCode, ToolError
from helpdesk.shared.crud.grants import ScopedGrant
from helpdesk.shared.models import (
    AccessReview,
    ReviewFlag,
    ReviewItem,
    ReviewStatus,
)
from helpdesk.shared.models.access import PERIOD_LENGTH

REVIEW_OPENED_EVENT_TYPE = "review.opened"
"""The audit event for "somebody was asked to check this access"."""

REVIEW_ITEM_DECIDED_EVENT_TYPE = "review.item_decided"
"""The audit event for "a reviewer answered for one grant". Its
`review_complete` is the only record of a review closing."""

REVIEW_REVOKE_REASON = "revoked by an access review decision"
"""The `grants.revoke_reason` a review's revoke writes, so an auditor can tell
it from a manual revoke, a transfer or an offboard by the reason alone."""

STALE_WINDOW = timedelta(days=14)
"""How close an expiry has to be for a grant to read `stale`, end day included."""

REVIEW_PERIOD_PATTERN = r"[0-9]{4}-Q[1-4]"
"""What a review period may look like. Matched with `re.fullmatch`: `re.match`
would accept a trailing newline, and that period would never equal the real one."""

_PERIOD_RE = re.compile(REVIEW_PERIOD_PATTERN)

_ACTOR_KIND = "user"
_SUBJECT_TYPE = "access_review"
_ITEM_SUBJECT_TYPE = "review_item"

_OPEN_STATUSES = (ReviewStatus.OPEN.value,)
"""Only an open review blocks a second one; a finished one is history."""


class InvalidPeriodError(ToolError):
    """A period that cannot be stored, or that no two runs would agree on."""

    code = ErrorCode.VALIDATION
    reason = "The review period is not one the help desk can use; give it like 2026-Q4."


class UnassignableReviewError(ToolError):
    """A scope with no owner, opened by a caller with no id. No current caller can reach this."""

    code = ErrorCode.VALIDATION


class ReviewAlreadyOpenError(ToolError):
    """A review of this scope for this period is already open."""

    code = ErrorCode.POLICY_BLOCKED
    reason = "A review of this scope and period is already open, so a second one is not opened."


class ItemAlreadyDecidedError(ToolError):
    """This review item already carries somebody's decision.

    Refused rather than overwritten: a review records what a named person
    decided at a named time, and a quiet second write would destroy that. A
    true retry of the same call is replayed by the idempotency guard and
    never gets here.
    """

    code = ErrorCode.POLICY_BLOCKED
    reason = "This review item already has a decision."


@dataclass(frozen=True)
class DecideItemResult:
    """One decided item, its review, and what the decision actually did.

    `revoked_grant_count` is 0 for a `keep` or a grant already gone, so the
    log never claims access was taken when there was none left.
    `review_complete` says whether this decision closed the review.
    """

    item: ReviewItem
    review: AccessReview
    revoked_grant_count: int
    review_complete: bool


@dataclass(frozen=True)
class OpenReviewResult:
    """The review that was opened, its items, and what the pass found.

    `flag_counts` keeps every flag, zeros too, so "no conflicts found" reads
    differently from "never looked for conflicts".
    """

    review: AccessReview
    items: tuple[ReviewItem, ...]
    flag_counts: Mapping[str, int]


@dataclass(frozen=True)
class ReviewScope:
    """One resolved scope, department or system, with its owner (the reviewer)."""

    department_id: uuid.UUID | None
    system_id: uuid.UUID | None
    owner_user_id: uuid.UUID | None


@dataclass(frozen=True)
class ReviewWithCounts:
    """One review of a queue, its scope name, its reviewer and its progress."""

    review: AccessReview
    department_name: str | None
    system_name: str | None
    reviewer_display_name: str
    item_count: int
    decided_count: int


@dataclass(frozen=True)
class ReviewItemWithContext:
    """One review item and the grant behind it, named.

    A reviewer cannot judge a uuid, so the item travels with the person,
    role, system, privilege and dates, all read from the rows the ids point
    at.
    """

    item: ReviewItem
    target_display_name: str
    role_name: str
    system_name: str
    privilege: str
    granted_at: datetime
    expires_at: datetime | None


def check_period(value: str) -> str:
    """The accepted period, or a typed refusal. Length first, so a huge string skips the regex."""
    if len(value) > PERIOD_LENGTH:
        raise InvalidPeriodError(
            f"a period longer than {PERIOD_LENGTH} characters cannot be stored: {len(value)}"
        )
    if _PERIOD_RE.fullmatch(value) is None:
        raise InvalidPeriodError(
            f"{value!r} is not a review period: it must match {REVIEW_PERIOD_PATTERN}"
        )
    return value


def quarter_period(now: datetime) -> str:
    """The calendar quarter `now` falls in, as `YYYY-Qn`.

    The daily review job opens only what this label has no review for yet,
    so two runs in one quarter give the same label and the second opens
    nothing. `now` comes from the caller so one run stays in one quarter.
    """
    return f"{now.year}-Q{(now.month - 1) // 3 + 1}"


def _flag_for(
    grant: ScopedGrant,
    *,
    conflicting_role_ids: frozenset[uuid.UUID],
    template_role_ids: frozenset[uuid.UUID],
    latest_expiry: Mapping[tuple[uuid.UUID, uuid.UUID], datetime | None],
    now: datetime,
) -> ReviewFlag:
    """The one flag this grant gets. Order matters: the first match wins.

    A grant that is stale, outside the template and in a conflict reads
    `sod_conflict`.
    """
    if grant.role_id in conflicting_role_ids:
        return ReviewFlag.SOD_CONFLICT
    if grant.role_id not in template_role_ids:
        return ReviewFlag.NON_STANDARD
    if _is_stale(grant, latest_expiry=latest_expiry, now=now):
        return ReviewFlag.STALE
    return ReviewFlag.OK


def _is_stale(
    grant: ScopedGrant,
    *,
    latest_expiry: Mapping[tuple[uuid.UUID, uuid.UUID], datetime | None],
    now: datetime,
) -> bool:
    """Is this grant ending inside the window with no renewal behind it?

    A grant that never expires is never stale. An expiry already past still
    counts: a live row past its date is what a reviewer must see. A renewal
    is a second live row for the same user and role; if any such row
    reaches further, the access is not ending and neither row is stale.
    """
    if grant.expires_at is None:
        return False
    if grant.expires_at > now + STALE_WINDOW:
        return False
    latest = latest_expiry[(grant.user_id, grant.role_id)]
    return latest is not None and latest <= now + STALE_WINDOW


def _latest_expiry_per_role(
    grants: Iterable[ScopedGrant],
) -> dict[tuple[uuid.UUID, uuid.UUID], datetime | None]:
    """The furthest an access reaches, per (user, role), across the scope.

    `None` means one grant never expires, which beats any date, so this is
    not a plain `max`. A renewal is always in the same scope, so no second
    query is needed.
    """
    latest: dict[tuple[uuid.UUID, uuid.UUID], datetime | None] = {}
    for grant in grants:
        key = (grant.user_id, grant.role_id)
        if key not in latest:
            latest[key] = grant.expires_at
            continue
        current = latest[key]
        if current is None or grant.expires_at is None:
            latest[key] = None
        elif grant.expires_at > current:
            latest[key] = grant.expires_at
    return latest


def _count(flags: "list[ReviewFlag]") -> dict[str, int]:
    """Every flag counted, zeros kept. Key order is left to the idempotency guard."""
    return {flag.value: sum(1 for found in flags if found is flag) for flag in ReviewFlag}


__all__ = [
    "PERIOD_LENGTH",
    "REVIEW_ITEM_DECIDED_EVENT_TYPE",
    "REVIEW_OPENED_EVENT_TYPE",
    "REVIEW_PERIOD_PATTERN",
    "REVIEW_REVOKE_REASON",
    "STALE_WINDOW",
    "DecideItemResult",
    "InvalidPeriodError",
    "ItemAlreadyDecidedError",
    "OpenReviewResult",
    "ReviewAlreadyOpenError",
    "ReviewItemWithContext",
    "ReviewScope",
    "ReviewWithCounts",
    "UnassignableReviewError",
    "check_period",
    "quarter_period",
]
