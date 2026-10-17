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
    Grant,
    ReviewDecision,
    ReviewFlag,
    ReviewItem,
    ReviewStatus,
)
from helpdesk.shared.models.access import PERIOD_LENGTH

REVIEW_OPENED_EVENT_TYPE = "review.opened"
"""the design's name for "somebody was asked to check this access".

Appended by the caller (`mcp_server.tools.reviews.open_access_review`) rather
than declared as the registry `event_type` (decision K): the pipeline's own
event carries a fixed payload and the TOOL as its subject, so it would record
that somebody called a tool and not which scope was reviewed or what was
found. The pipeline's `tool.succeeded` lands after it as the summary, the
same shape as an earlier change's `grant.revoked` and 12d's `user.offboarded`."""

REVIEW_ITEM_DECIDED_EVENT_TYPE = "review.item_decided"
"""the design's name for "a reviewer answered for one grant" (decision G).

Appended by `decide_item` rather than declared as the registry `event_type`,
for the same reason as `review.opened` above and an earlier change's `grant.revoked`: the pipeline's own event carries a fixed payload and the
TOOL as its subject, so as the declared type it would record that somebody
called a tool and not which grant was ended, why it was in front of a reviewer,
or whether the review is now finished. The pipeline's `tool.succeeded` lands
after it as the summary. There is no `review.completed` event — the design lists
three review tools and none of them completes a review on its own —
so `review_complete` on this event is the entire record of a review closing,
mirroring an earlier change's `chain_complete`."""

REVIEW_REVOKE_REASON = "revoked by an access review decision"
"""The one sentence a review's revocation writes (decision E).

On `grants.revoke_reason` and on the `grant.revoked` payload, exactly as plan
11d's caller-supplied reason, 12b's `TRANSFER_REVOKE_REASON` and 12d's
`OFFBOARD_REVOKE_REASON` are, so an auditor can tell the four apart by the
reason alone. Pinned to this value, and against the other two constants, by
`test_access_reviews.py::test_the_decide_constants_and_error_code_are_exactly_the_spec_values`."""

STALE_WINDOW = timedelta(days=14)
"""How close an expiry has to be for a grant to read `stale` (decision C).

the design's own number. The comparison is INCLUSIVE — an expiry at exactly
`now + STALE_WINDOW` is stale — because the window is "expiry within 14
days", and a grant expiring on the boundary day is the one a reviewer most
needs to see. Pinned to the exact value, and inclusively at both ends, by
`test_access_reviews.py::test_the_stale_boundary_is_inclusive_at_fourteen_days`."""

REVIEW_PERIOD_PATTERN = r"[0-9]{4}-Q[1-4]"
"""What an `access_reviews.period` may look like, matched with `re.fullmatch`.

`re.match` and a Pydantic `Field(pattern=...)` anchor only at the start, so
both accept `"2026-Q1\\n"` — a period that reads identically to the real one in
a list and compares UNEQUAL to it everywhere, which is exactly the value
`_refuse_if_open` would fail to recognise as the quarter already open. The rejection table, with its trailing newline, CRLF and
space rows, is
`test_access_review_generation.py::test_check_period_refuses_these`.

The NARROWER of the two gates: this fixes seven characters, where
`PERIOD_LENGTH` admits sixteen, so the inclusive boundary criterion is written
against the pattern and the length is the column's backstop (standing
rule 32)."""

_PERIOD_RE = re.compile(REVIEW_PERIOD_PATTERN)

_ACTOR_KIND = "user"
_SUBJECT_TYPE = "access_review"
_ITEM_SUBJECT_TYPE = "review_item"

_OPEN_STATUSES = (ReviewStatus.OPEN.value,)
"""Which existing reviews block a second one for the same (scope, period).

Only `open`: a COMPLETED review of the same quarter is history, and refusing
a re-review because last quarter's was finished would make the scope
unreviewable for ever. Pinned by
`test_access_reviews.py::test_a_completed_review_does_not_block_a_new_one_for_the_same_period`."""


class InvalidPeriodError(ToolError):
    """A period that cannot be stored, or that no two runs would agree on.

    `VALIDATION`, the the design code for an input to be fixed rather than
    retried. Raised before anything is read or written; pinned as a typed
    refusal with its code by the rejection table above and by
    `::test_check_period_refuses_a_period_longer_than_the_column`.
    """

    code = ErrorCode.VALIDATION


class UnassignableReviewError(ToolError):
    """A scope with no owner, opened by a caller with no id.

    `VALIDATION`, because `access_reviews.reviewer_user_id` is not nullable and
    the pair names nobody to review the scope. Deliberately not reachable from
    any registered caller — see `_reviewer_for` — so it exists to make the
    impossible pair a typed refusal instead of an `IntegrityError` from a null
    column, and no test names it.
    """

    code = ErrorCode.VALIDATION


class ReviewAlreadyOpenError(ToolError):
    """A review of this scope for this period is already open (decision G).

    `POLICY_BLOCKED`, never `NOT_FOUND` or a database `IntegrityError`: the
    caller reached the scope legitimately and the refusal is about state, not
    about existence or permission. `NOT_FOUND` stays reserved for the two
    absences that must stay indistinguishable (missing, and another
    tenant's), which is what keeps a scope id from being an existence oracle.
    Pinned to its code by
    `test_access_reviews.py::test_the_review_errors_carry_their_spec_error_codes`.
    """

    code = ErrorCode.POLICY_BLOCKED


class ItemAlreadyDecidedError(ToolError):
    """This review item already carries somebody's decision (decision C).

    `POLICY_BLOCKED`, never `NOT_FOUND`: the caller reached the item
    legitimately and the refusal is about state, so `NOT_FOUND` stays reserved
    for the two absences that must stay indistinguishable (missing, and
    another tenant's) — which is what keeps an item id from being an existence
    oracle.

    Refusing rather than overwriting is the whole point of the table: a review
    is the record of what a named person decided at a named instant, and a
    silent second write destroys the only thing it is for. A genuine retry of
    the same call never reaches here — the idempotency guard replays the first
    response — so what this refuses is a DIFFERENT call on a decided item.
    Pinned to its code by
    `test_access_reviews.py::test_the_decide_constants_and_error_code_are_exactly_the_spec_values`
    and to its behaviour by
    `::test_a_second_decision_on_a_decided_item_is_refused_and_writes_nothing`.
    """

    code = ErrorCode.POLICY_BLOCKED


@dataclass(frozen=True)
class DecideItemResult:
    """One decided item, its review, and what the decision actually did.

    `revoked_grant_count` is 0 for every `keep` and for decision B's item
    whose grant was already revoked, and 1 for a revocation that ended a row;
    it is what stops the audit log from claiming access was taken away when
    there was nothing left to take. `review_complete` says whether THIS
    decision closed the review.
    """

    item: ReviewItem
    review: AccessReview
    revoked_grant_count: int
    review_complete: bool


@dataclass(frozen=True)
class OpenReviewResult:
    """The review that was opened, its items, and what the pass found.

    `flag_counts` counts every `ReviewFlag` member, `ok` included and zeros
    kept: a review whose audit record listed only the flags that occurred
    would make "no conflicts found" indistinguishable from "conflicts were
    never looked for". Pinned by
    `test_access_reviews.py::test_the_flag_counts_carry_every_flag_including_the_zeros`.
    """

    review: AccessReview
    items: tuple[ReviewItem, ...]
    flag_counts: Mapping[str, int]


@dataclass(frozen=True)
class ReviewScope:
    """One resolved scope: the department or the system, never both.

    Carries the row's own natural owner because that is who the review is
    FOR (decision F) — `departments.manager_user_id` or
    `systems.owner_user_id` — and the caller has no say in it.
    """

    department_id: uuid.UUID | None
    system_id: uuid.UUID | None
    owner_user_id: uuid.UUID | None


@dataclass(frozen=True)
class ReviewWithCounts:
    """One review of a queue, its two scope names, its reviewer, its progress.

    A frozen dataclass around the ORM row rather than a Pydantic model
    repeating its columns, exactly as `approvals.PendingWithContext` is: the
    wire shape is `mcp_server.tools.reviews.ReviewQueueRow`, which the design
    owns, and a second thirteen-field model here would be two contracts to
    keep in step. What this carries beside the row is what the JOIN answered
    and the row cannot — the scope's name and the reviewer's, resolved from
    the rows their ids point at — plus the two counts.

    Exactly one of `department_name` and `system_name` is set on any row,
    because `access_reviews`' own check constraint allows exactly one scope
    id. Pinned by
    `test_mcp_reviews.py::test_each_permitted_principal_reads_the_queue_of_their_own_scope`,
    which asserts the unused pair is null per principal.
    """

    review: AccessReview
    department_name: str | None
    system_name: str | None
    reviewer_display_name: str
    item_count: int
    decided_count: int


@dataclass(frozen=True)
class ReviewItemWithContext:
    """One review item and the grant behind it, named.

    `grant_id` is a uuid and a reviewer cannot decide a uuid (an earlier change
    decision B), so the item travels with the PERSON, the role, the system,
    the privilege and the grant's two dates. Every one of them is read from
    the row its id points at rather than echoed off the item, pinned by
    `test_access_reviews.py::test_list_items_with_context_carries_the_grants_six_context_fields`
    and by
    `test_mcp_reviews.py::test_list_review_items_answers_eleven_keys_naming_the_grant_behind_the_item`,
    whose grant holder carries a display name no other row in that file has —
    so a join to the reviewer instead of the holder answers a different string
    rather than a plausible one.
    """

    item: ReviewItem
    target_display_name: str
    role_name: str
    system_name: str
    privilege: str
    granted_at: datetime
    expires_at: datetime | None


def check_period(value: str) -> str:
    """The accepted period, or a typed refusal.

    The length first and the shape second, so an enormous string is refused
    without being run through a regex — `check_job_name`'s order, for its
    reason. `PERIOD_LENGTH` is imported from `models/access.py` rather than
    written again here, so the pre-check and the column it guards stay one
    number by construction.
    """
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

    The label every review of one scheduler tick is filed under, and therefore
    the whole of decision A's idempotence: the generator runs daily and opens
    only what this string has no review for yet. Derived from the month alone
    — no locale, no fiscal offset — so two runs a day apart inside one quarter
    answer the same string and the second opens nothing.

    `now` is the caller's instant, never a clock read here, so every scope of
    one run lands in one quarter. The boundary is pinned to the
    microsecond by
    `test_access_review_generation.py::test_the_last_microsecond_of_a_quarter_and_the_first_of_the_next_differ`.
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
    """The ONE word this grant gets, by decision E's precedence.

    Written as an ordered chain rather than a set of independent tests
    because the order IS the decision: the first match wins, and a grant that
    is stale, outside the template and half of an SoD pair reads
    `sod_conflict`. Pinned on a grant that is all three by
    `test_access_reviews.py::test_a_grant_that_is_all_three_reads_sod_conflict`,
    so reordering these branches fails rather than quietly changing what a
    reviewer is shown first.
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
    """Is this grant expiring inside the window with no renewal behind it?

    Three conditions, and the third is what makes this a rule about ACCESS
    rather than about a row. A grant that never expires is never stale. An
    expiry at or before `now + STALE_WINDOW` is inside the window, an
    already-past one included — a still-`active` row whose expiry has gone by
    is exactly what the seed plants and exactly what a reviewer must see, and
    it reaches this function only because the scope listings filter on
    `_granted_clauses` rather than on liveness.

    And then the renewal: nothing makes a grant unique per (user, role), so
    the normal way to extend access is a second live row. If any live grant
    for the same (user, role) reaches further — a later expiry, or no expiry
    at all — the access is not ending and neither row is stale. Pinned by
    `test_access_reviews.py::test_a_renewal_for_the_same_role_makes_neither_grant_stale`,
    which plants the renewal as a second row rather than by moving the first
    one's date, so a rule written per row cannot pass it.
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

    `None` means "one of these grants never expires", which is further than
    any date and is why this cannot be a plain `max`. The renewal partner of
    an in-scope grant is always in scope itself — a department's scope holds
    every grant of its members, a system's scope every grant of its roles —
    so the answer here is complete without a second query.
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
    """Every flag counted, zeros kept — see `OpenReviewResult.flag_counts`.

    Key ORDER is not this function's problem, and deliberately so: a mutating
    tool's response is normalised by `helpdesk.idempotency.guard`, which is
    the only place that can make the first call and its replay agree. This
    dict is the first dict-valued field any tool output has, which is what
    exposed that the guard normalised one path and not the other.
    """
    return {flag.value: sum(1 for found in flags if found is flag) for flag in ReviewFlag}


__all__ = [
    "REVIEW_OPENED_EVENT_TYPE",
    "REVIEW_ITEM_DECIDED_EVENT_TYPE",
    "REVIEW_REVOKE_REASON",
    "STALE_WINDOW",
    "REVIEW_PERIOD_PATTERN",
    "DecideItemResult",
    "ItemAlreadyDecidedError",
    "InvalidPeriodError",
    "OpenReviewResult",
    "ReviewAlreadyOpenError",
    "ReviewItemWithContext",
    "ReviewScope",
    "ReviewWithCounts",
    "UnassignableReviewError",
    "check_period",
    "quarter_period",
]
