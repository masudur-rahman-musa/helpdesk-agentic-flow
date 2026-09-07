"""Opening an access review and reading its items (the design, an earlier change).

A review is a QUESTION, never an answer. This module decides nothing about
access: it resolves the scope, refuses a duplicate, reads every ACTIVE grant
in that scope, gives each one the single word that says why a reviewer should
look at it, and writes the review with its items. Nothing here revokes,
grants or classifies.

`decide_item` is the other half and the only path from a review to
a change. It decides nothing about access either: it records the reviewer's
word, and for `revoke` hands the ONE row the item names to `access/revoke.py`,
which stays the only writer of `grant.revoked`. The unit is the row rather
than an earlier change's live set for a (user, role), because a review item IS one row
and every other active row for that pair is a separate item of the same
review; and the row is taken with no liveness filter, because the
lapsed-but-`active` grant this module flags `stale` is precisely the one a
reviewer must be able to end (decision A).

The three flags all mean "this grant is worth a second look", and the design
gives an item exactly ONE `flag` column, so a grant that is all three still
gets one word. Decision E fixes the precedence as `ReviewFlag`'s own
declaration order — `sod_conflict` > `non_standard` > `stale` > `ok` — because
a conflict is a control failure, being outside the template questions whether
the grant should exist at all, and staleness only questions its age.

`stale` is the EXPIRY half of the design's definition and not its login half
(decision C, a design note): no login timestamp exists anywhere in the schema, so
the other half would have to invent a data source the product does not have.
What is implemented is exactly what the seed already plants.

"Active" is `grants.status == "active"` and unrevoked, and deliberately NOT
the liveness triple every other read here uses: nothing flips a row to
`expired` until row 21's sweep runs, so a grant whose expiry went by last
week is still sitting there `active`, and it is the single most reviewable
row in the tenant. `core/repository.py::_granted_clauses` carries that
reasoning; a review filtered on liveness could never produce a `stale` item
at all.

Two questions are asked per HOLDER rather than per grant, and cached for the
pass: what the holder's department template says they should have, which is a
directory fact and comes through `DirectoryReader`, the port's read half; and
everything they hold, which is asked under the same `active` definition as
the scope and therefore reads the grants directly. This module never writes,
so it takes the reader rather than the whole provider and cannot revoke even
by accident.

Everything runs in the caller's transaction. The review, its items and its
`review.opened` are flushed, never committed, so a caller that fails
afterwards takes all three with it.
"""

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.access.directory import DirectoryProvider, DirectoryReader
from helpdesk.access.revoke import revoke_grant_row
from helpdesk.audit import AuditEventInput, append_event
from helpdesk.core.db import bound_tenant_id
from helpdesk.core.repository import (
    Repository,
    ScopedGrant,
    is_active_grant,
    list_active_grants_for_system,
    list_active_grants_in_department,
    list_granted_role_ids,
    lock_department,
    lock_grant_row,
    lock_system,
)
from helpdesk.errors import ErrorCode, NotFoundError, ToolError
from helpdesk.models import (
    AccessReview,
    Grant,
    ReviewDecision,
    ReviewFlag,
    ReviewItem,
    ReviewStatus,
    SodRule,
)
from helpdesk.policy.sod import held_conflicts
from helpdesk.policy.types import SodRuleSnapshot

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

_ACTOR_KIND = "user"
_SUBJECT_TYPE = "access_review"
_ITEM_SUBJECT_TYPE = "review_item"

_OPEN_STATUSES = (ReviewStatus.OPEN.value,)
"""Which existing reviews block a second one for the same (scope, period).

Only `open`: a COMPLETED review of the same quarter is history, and refusing
a re-review because last quarter's was finished would make the scope
unreviewable for ever. Pinned by
`test_access_reviews.py::test_a_completed_review_does_not_block_a_new_one_for_the_same_period`."""


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


async def _resolve_scope(
    session: AsyncSession, *, department_id: uuid.UUID | None, system_id: uuid.UUID | None
) -> ReviewScope:
    """Lock the scope row and read its owner, or answer `NOT_FOUND`.

    The lock is taken before anything is read, because the duplicate refusal
    below is a read-compute-write that two callers would otherwise both win
    (decision G). It runs through the ORM, so the tenant guard makes a scope
    id in another tenant answer exactly like a missing one (pinned by
    `test_mcp_reviews.py::test_a_missing_and_another_tenants_scope_answer_the_same_not_found`).

    Exactly one id is set by the time this runs — the input model refuses
    neither and both before any effect — so the `else` here is the system
    branch rather than a third case.
    """
    if department_id is not None:
        department = await lock_department(session, department_id)
        if department is None:
            raise NotFoundError("no such department in this tenant")
        return ReviewScope(
            department_id=department.id,
            system_id=None,
            owner_user_id=department.manager_user_id,
        )
    if system_id is None:  # pragma: no cover - the input model forbids it
        raise ValueError("a review scope needs exactly one of department_id or system_id")
    system = await lock_system(session, system_id)
    if system is None:
        raise NotFoundError("no such system in this tenant")
    return ReviewScope(department_id=None, system_id=system.id, owner_user_id=system.owner_user_id)


async def _refuse_if_open(session: AsyncSession, scope: ReviewScope, period: str) -> None:
    """Decision G: one open review per (scope, period), and no second write.

    Runs after the scope row is locked and before any write, so two concurrent
    openers serialise on the lock and the loser re-reads the review the winner
    committed. A partial unique index would refuse the same case with an
    `IntegrityError` mid-transaction rather than one of the eight typed SPEC
    §8.1 codes, which is why the check is here. Pinned by
    `test_access_reviews.py::test_a_second_open_review_for_the_same_scope_and_period_is_refused`
    and, on two connections, by
    `test_mcp_reviews.py::test_two_callers_racing_the_same_review_leave_exactly_one`.
    """
    result = await session.execute(
        select(AccessReview).where(
            AccessReview.department_id == scope.department_id,
            AccessReview.system_id == scope.system_id,
            AccessReview.period == period,
            AccessReview.status.in_(_OPEN_STATUSES),
        )
    )
    if result.scalars().first() is not None:
        raise ReviewAlreadyOpenError(
            f"a review of this scope for {period!r} is already open; complete it first"
        )


async def open_review(
    session: AsyncSession,
    directory: DirectoryReader,
    *,
    department_id: uuid.UUID | None,
    system_id: uuid.UUID | None,
    period: str,
    opened_by_user_id: uuid.UUID,
    due_at: datetime | None = None,
    actor_ref: str | None = None,
    session_id: uuid.UUID | None = None,
    now: datetime,
) -> OpenReviewResult:
    """Open one review over one scope, with an item per live grant in it.

    The order of effects is forced by the audit record: `review.opened` names
    the item count and the flag tally (decision K), neither of which is known
    until every grant has been read and flagged, so the event lands LAST —
    before the pipeline's `tool.succeeded`, which is the summary.

    The reviewer is the scope's natural owner and never the caller's choice
    (decision F): `departments.manager_user_id` or `systems.owner_user_id`,
    falling back to the caller only when the scope names nobody. the design calls
    the procedure "what managers must do", and row 21's scheduler opens
    reviews with no caller at all, so any rule reading the reviewer off the
    ARGUMENTS would have to be replaced at that row. Pinned by
    `test_access_reviews.py::test_the_reviewer_is_the_scopes_owner_not_the_caller`
    and its fallback twin
    `::test_a_scope_with_no_owner_falls_back_to_the_caller`.

    A scope holding no live grants opens a review with zero items rather than
    being refused (decision J): "nobody holds anything" is a true answer to a
    review, and refusing it would leave a department unreviewable precisely
    when it is clean. That review is written `completed` (decision F), because
    nothing could ever complete it afterwards — completion happens when the
    last undecided item is decided, and it has none. Pinned by
    `test_access_reviews.py::test_a_scope_with_no_active_grants_opens_a_review_already_completed`,
    with `::test_a_scope_with_one_active_grant_still_opens_open` beside it so
    "complete every review" cannot satisfy it vacuously.

    `now` is keyword-only and supplied by the caller, so the liveness filter
    that picked the grants, every staleness comparison and the event all
    answer for one instant; reading a clock per grant would let two items of
    one review disagree about the same boundary.

    UNCAPPED on purpose: a department with thousands of live
    grants writes one item per grant in one transaction. A cap would hide a
    grant from the only person who can review it, the same reasoning that
    left `list_pending_for_approver`'s read unbounded; it is recorded as a
    known issue rather than fixed with a limit.
    """
    scope = await _resolve_scope(session, department_id=department_id, system_id=system_id)
    await _refuse_if_open(session, scope, period)

    grants = (
        await list_active_grants_in_department(session, scope.department_id)
        if scope.department_id is not None
        else await list_active_grants_for_system(session, _checked(scope.system_id))
    )
    flags = await _flag_grants(session, directory, grants, now=now)

    # Decision F: a review with nothing to decide is answered on arrival.
    # Completion happens when the LAST undecided item is decided, so a review
    # that never had an item could never be completed by any call — the design
    # lists no `complete_review` tool — and would sit open for ever on row
    # 21's reminder list. Zero items is a true and finished answer to
    # "who holds what here", not an unfinished one.
    empty = not grants
    review = await Repository(session, AccessReview).create(
        reviewer_user_id=scope.owner_user_id
        if scope.owner_user_id is not None
        else opened_by_user_id,
        department_id=scope.department_id,
        system_id=scope.system_id,
        period=period,
        status=ReviewStatus.COMPLETED.value if empty else ReviewStatus.OPEN.value,
        due_at=due_at,
        completed_at=now if empty else None,
    )
    items = tuple(
        [
            await Repository(session, ReviewItem).create(
                review_id=review.id,
                grant_id=grant.grant_id,
                flag=flag.value,
                decision=None,
                decided_at=None,
            )
            for grant, flag in zip(grants, flags, strict=True)
        ]
    )
    counts = _count(flags)
    await _append_opened(
        session,
        review=review,
        scope=scope,
        period=period,
        item_count=len(items),
        flag_counts=counts,
        actor_ref=actor_ref if actor_ref is not None else str(opened_by_user_id),
        session_id=session_id,
        now=now,
    )
    return OpenReviewResult(review=review, items=items, flag_counts=counts)


def _checked(system_id: uuid.UUID | None) -> uuid.UUID:
    """`_resolve_scope` guarantees one of the two ids; this tells mypy so."""
    if system_id is None:  # pragma: no cover - unreachable after _resolve_scope
        raise ValueError("a system-scoped review has no system id")
    return system_id


async def _flag_grants(
    session: AsyncSession,
    directory: DirectoryReader,
    grants: Sequence[ScopedGrant],
    *,
    now: datetime,
) -> list[ReviewFlag]:
    """One flag per grant, in the order the grants came back.

    The template and the held-role set are asked per HOLDER and cached, not
    per grant: a department review over one person's eight grants asks twice,
    not sixteen times. The SoD rules are read once for the tenant, because a
    rule is not about any one holder.

    The conflict question is asked over everything the holder HOLDS — the
    whole tenant, not the scope, and under the review's own definition of held
    rather than the directory's liveness one. `list_granted_role_ids` carries
    both halves of that reasoning; the template question stays on
    `DirectoryReader`, because what a department is supposed to have is a
    directory fact with no liveness in it at all.
    """
    rules = tuple(
        SodRuleSnapshot(
            rule_id=rule.id,
            role_a_id=rule.role_a_id,
            role_b_id=rule.role_b_id,
            policy_ref=rule.policy_ref,
        )
        for rule in await Repository(session, SodRule).list()
    )
    latest_expiry = _latest_expiry_per_role(grants)

    conflicts: dict[uuid.UUID, frozenset[uuid.UUID]] = {}
    templates: dict[uuid.UUID, frozenset[uuid.UUID]] = {}
    flags: list[ReviewFlag] = []
    for grant in grants:
        if grant.user_id not in conflicts:
            held = await list_granted_role_ids(session, grant.user_id)
            conflicts[grant.user_id] = held_conflicts(rules, held)
            templates[grant.user_id] = await directory.list_membership_role_ids(grant.user_id)
        flags.append(
            _flag_for(
                grant,
                conflicting_role_ids=conflicts[grant.user_id],
                template_role_ids=templates[grant.user_id],
                latest_expiry=latest_expiry,
                now=now,
            )
        )
    return flags


def _count(flags: Sequence[ReviewFlag]) -> dict[str, int]:
    """Every flag counted, zeros kept — see `OpenReviewResult.flag_counts`.

    Key ORDER is not this function's problem, and deliberately so: a mutating
    tool's response is normalised by `helpdesk.idempotency.guard`, which is
    the only place that can make the first call and its replay agree. This
    dict is the first dict-valued field any tool output has, which is what
    exposed that the guard normalised one path and not the other.
    """
    return {flag.value: sum(1 for found in flags if found is flag) for flag in ReviewFlag}


async def _append_opened(
    session: AsyncSession,
    *,
    review: AccessReview,
    scope: ReviewScope,
    period: str,
    item_count: int,
    flag_counts: Mapping[str, int],
    actor_ref: str,
    session_id: uuid.UUID | None,
    now: datetime,
) -> None:
    """the design's `review.opened`, subject the REVIEW, decision K's keys exactly.

    The subject is the review and not the tool, and not the department: an
    auditor asking "what was this person asked to check, and what did the
    system already think was wrong" reads all of it here. `item_count` is
    carried beside `flag_counts` rather than left to be summed from it, so a
    review with zero items records a scope that was checked and found empty
    rather than looking like a truncated record. Pinned, subject and every
    key, by
    `test_access_reviews.py::test_review_opened_names_the_review_and_carries_exactly_seven_keys`.
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
            event_type=REVIEW_OPENED_EVENT_TYPE,
            subject_type=_SUBJECT_TYPE,
            subject_id=str(review.id),
            payload={
                "review_id": str(review.id),
                "department_id": None if scope.department_id is None else str(scope.department_id),
                "system_id": None if scope.system_id is None else str(scope.system_id),
                "period": period,
                "reviewer_user_id": str(review.reviewer_user_id),
                "item_count": item_count,
                "flag_counts": dict(flag_counts),
            },
        ),
    )


async def find_review(session: AsyncSession, review_id: uuid.UUID) -> AccessReview | None:
    """The bound tenant's review, or None.

    Through the ORM, so the tenant guard injects its filter and a review id
    from another tenant reads exactly like one that does not exist — the
    caller turns both into one `NOT_FOUND`, and only AFTER its scope check,
    so a review id is neither an existence nor a state oracle.
    """
    return await Repository(session, AccessReview).get(review_id)


async def find_item(session: AsyncSession, item_id: uuid.UUID) -> ReviewItem | None:
    """The bound tenant's review item, or None.

    Through the ORM, so the tenant guard injects its filter and an item id
    from another tenant reads exactly like one that does not exist — the
    caller turns both into one `NOT_FOUND`, and only AFTER its scope check, so
    an item id is neither an existence nor a state oracle. That the guard
    really reaches this table (an earlier change decision B put `tenant_id` on the
    child for it) is pinned by
    `test_access_reviews.py::test_find_item_answers_nothing_for_another_tenants_item`.
    """
    return await Repository(session, ReviewItem).get(item_id)


async def decide_item(
    session: AsyncSession,
    directory: DirectoryProvider,
    *,
    item: ReviewItem,
    decision: ReviewDecision,
    decided_by_user_id: uuid.UUID,
    actor_ref: str | None = None,
    session_id: uuid.UUID | None = None,
    now: datetime,
) -> DecideItemResult:
    """Record one reviewer's word on one item, and act on it. Caller's transaction.

    This decides nothing about access either: `keep` is a record and nothing
    else (decision D — extending an expiry IS granting access, and every grant
    in this system comes from the engine with a decision behind it, so a
    renewal is a new access request), and `revoke` hands the row to
    `access/revoke.py`, which stays the only writer of `grant.revoked`.

    The order of effects is forced by the audit record: `review.item_decided`
    names `revoked_grant_count` and `review_complete`, neither knowable until
    the revocation has been attempted and the review re-counted, so the event
    lands LAST — after `grant.revoked` and before the pipeline's
    `tool.succeeded`. Pinned end to end by
    `test_mcp_reviews.py::test_the_audit_sequence_is_review_item_decided_then_tool_succeeded`.

    The GRANT row is locked first and the item re-read under that lock
    (decision I). Locking the grant rather than the item is what keeps one
    lock order with `lock_live_grants_for_role`, so a review decision and a
    concurrent `revoke_access` for the same person cannot deadlock; re-reading
    the item afterwards is what makes the loser of a race see the winner's
    decision instead of the undecided row it read before waiting, and be
    refused by `ItemAlreadyDecidedError`. Both halves are pinned on two real
    connections by
    `test_access_reviews.py::test_two_concurrent_deciders_leave_one_decided_item_and_one_refusal`,
    which fails if the lock is taken AFTER the already-decided check.

    Decision B: an item whose grant was revoked between the review opening and
    this call records the decision, revokes nothing and reports
    `revoked_grant_count: 0`. Refusing would leave an item nobody can decide
    and therefore a review nobody can complete, and the reviewer's only
    remaining move would be `keep`, which would record a lie.

    `now` is keyword-only and supplied by the caller, so `decided_at`, the
    revocation's `revoked_at`, any completion and the event all answer for one
    instant.
    """
    grant = await lock_grant_row(session, item.grant_id)
    if grant is None:  # pragma: no cover - the item's FK guarantees the row
        raise NotFoundError("no such grant in this tenant")
    decided = await _reread_under_lock(session, item)
    if decided.decision is not None:
        raise ItemAlreadyDecidedError(
            f"review item {decided.id} was already decided {decided.decision!r}"
        )

    updated = await Repository(session, ReviewItem).update(
        decided.id, decision=decision.value, decided_at=now
    )
    if updated is None:  # pragma: no cover - the row was just read under the lock
        raise NotFoundError("no such review item in this tenant")

    revoked_grant_count = 0
    if decision is ReviewDecision.REVOKE and is_active_grant(grant):
        # `is_active_grant`, never `is_live_grant`: a lapsed-but-`active` row
        # is exactly what this review flagged `stale`, and a liveness test
        # here would leave the most reviewable grant in the tenant the one a
        # reviewer cannot end (decision A).
        await revoke_grant_row(
            session,
            directory,
            grant=grant,
            reason=REVIEW_REVOKE_REASON,
            revoked_by_user_id=decided_by_user_id,
            actor_ref=actor_ref,
            session_id=session_id,
            revoked_count=1,
            now=now,
        )
        revoked_grant_count = 1

    review, complete = await _complete_if_finished(session, updated.review_id, now=now)
    await _append_item_decided(
        session,
        item=updated,
        grant=grant,
        decision=decision,
        revoked_grant_count=revoked_grant_count,
        review_complete=complete,
        actor_ref=actor_ref if actor_ref is not None else str(decided_by_user_id),
        session_id=session_id,
        now=now,
    )
    return DecideItemResult(
        item=updated,
        review=review,
        revoked_grant_count=revoked_grant_count,
        review_complete=complete,
    )


async def _reread_under_lock(session: AsyncSession, item: ReviewItem) -> ReviewItem:
    """The item's CURRENT row, re-read now that the grant row is held.

    `populate_existing=True` is the whole of this function. The caller loaded
    this item before it waited on the lock, so a plain read would come back
    from the session's identity map carrying the stale `decision = None` it
    saw then; the loser of a race would overwrite the winner's decision while
    every single-caller test still passed. Pinned by
    `test_access_reviews.py::test_two_concurrent_deciders_leave_one_decided_item_and_one_refusal`.
    """
    result = await session.execute(
        select(ReviewItem).where(ReviewItem.id == item.id).execution_options(populate_existing=True)
    )
    return result.scalars().one()


async def _complete_if_finished(
    session: AsyncSession, review_id: uuid.UUID, *, now: datetime
) -> tuple[AccessReview, bool]:
    """Close the review if this decision was its last undecided item.

    The count is over THIS review's items only, not the tenant's: a
    neighbouring review's open item must not hold this one open (pinned by
    `test_access_reviews.py::test_a_neighbouring_reviews_undecided_item_does_not_hold_this_one_open`).
    It runs after the item's own write is flushed, so the row just decided is
    counted as decided.

    Returns the review either way, so the caller reports its status without a
    second read.
    """
    remaining = await session.execute(
        select(ReviewItem.id).where(
            ReviewItem.review_id == review_id, ReviewItem.decision.is_(None)
        )
    )
    review = await Repository(session, AccessReview).get(review_id)
    if review is None:  # pragma: no cover - the item's FK guarantees the row
        raise NotFoundError("no such access review in this tenant")
    if remaining.scalars().first() is not None:
        return review, False
    completed = await Repository(session, AccessReview).update(
        review_id, status=ReviewStatus.COMPLETED.value, completed_at=now
    )
    if completed is None:  # pragma: no cover - just read above
        raise NotFoundError("no such access review in this tenant")
    return completed, True


async def _append_item_decided(
    session: AsyncSession,
    *,
    item: ReviewItem,
    grant: Grant,
    decision: ReviewDecision,
    revoked_grant_count: int,
    review_complete: bool,
    actor_ref: str,
    session_id: uuid.UUID | None,
    now: datetime,
) -> None:
    """the design's `review.item_decided`, subject the ITEM, decision G's keys exactly.

    The subject is the item and not the review, the grant or the tool: an
    auditor asking "what happened to this item" queries `subject_id`, and an
    event naming the review answers that for no item at all. `user_id` and
    `role_id` are read off the GRANT because the item row names only a grant,
    `flag` says why the grant was in front of a reviewer, `revoked_grant_count`
    tells decision B's revoke-that-ended-nothing from a real revocation, and
    `review_complete` is the only record that a review closed. Pinned, subject
    and every key, by
    `test_access_reviews.py::test_review_item_decided_names_the_item_and_carries_decision_gs_keys`.
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
            event_type=REVIEW_ITEM_DECIDED_EVENT_TYPE,
            subject_type=_ITEM_SUBJECT_TYPE,
            subject_id=str(item.id),
            payload={
                "review_id": str(item.review_id),
                "item_id": str(item.id),
                "grant_id": str(grant.id),
                "user_id": str(grant.user_id),
                "role_id": str(grant.role_id),
                "flag": item.flag,
                "decision": decision.value,
                "revoked_grant_count": revoked_grant_count,
                "review_complete": review_complete,
            },
        ),
    )


async def list_items(
    session: AsyncSession, review_id: uuid.UUID, *, limit: int
) -> Sequence[ReviewItem]:
    """One review's items, oldest first, at most `limit` of them.

    Ordered by `created_at` then `id`, which is the order they were written
    and therefore the scope listing's own order (holder, then grant age): a
    reviewer works down a stable list, and two calls return the same page.

    The caller has already resolved the review and checked its scope, so
    there is deliberately no scope argument here — one scope authority
    (`request_in_scope`) answers for approvals, revocation and reviews alike,
    and a second filter in this query would be a second place for it to drift.
    """
    result = await session.execute(
        select(ReviewItem)
        .where(ReviewItem.review_id == review_id)
        .order_by(ReviewItem.created_at, ReviewItem.id)
        .limit(limit)
    )
    return result.scalars().all()


__all__ = [
    "REVIEW_ITEM_DECIDED_EVENT_TYPE",
    "REVIEW_OPENED_EVENT_TYPE",
    "REVIEW_REVOKE_REASON",
    "STALE_WINDOW",
    "DecideItemResult",
    "ItemAlreadyDecidedError",
    "OpenReviewResult",
    "ReviewAlreadyOpenError",
    "ReviewScope",
    "decide_item",
    "find_item",
    "find_review",
    "list_items",
    "open_review",
]
