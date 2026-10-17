"""Opening and deciding access reviews (the design, an earlier change, an earlier change split). The read half lives in review_queries.py."""

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.access.directory import DirectoryProvider, DirectoryReader
from helpdesk.shared.access.review_types import (
    REVIEW_ITEM_DECIDED_EVENT_TYPE,
    REVIEW_OPENED_EVENT_TYPE,
    REVIEW_REVOKE_REASON,
    STALE_WINDOW,
    DecideItemResult,
    InvalidPeriodError,
    ItemAlreadyDecidedError,
    OpenReviewResult,
    ReviewAlreadyOpenError,
    ReviewScope,
    UnassignableReviewError,
    _ACTOR_KIND,
    _ITEM_SUBJECT_TYPE,
    _OPEN_STATUSES,
    _SUBJECT_TYPE,
    _count,
    _flag_for,
    _is_stale,
    _latest_expiry_per_role,
)
from helpdesk.shared.access.revoke import USER_ACTOR_KIND, resolve_actor_ref, revoke_grant_row
from helpdesk.shared.audit import AuditEventInput, append_event
from helpdesk.shared.core.db import bound_tenant_id
from helpdesk.shared.core.errors import NotFoundError
from helpdesk.shared.crud import Repository
from helpdesk.shared.crud.grants import (
    ScopedGrant,
    is_active_grant,
    list_active_grants_for_system,
    list_active_grants_in_department,
    list_granted_role_ids,
    lock_grant_row,
)
from helpdesk.shared.crud.users import lock_department, lock_system
from helpdesk.shared.models import (
    AccessReview,
    Department,
    Grant,
    ReviewDecision,
    ReviewFlag,
    ReviewItem,
    ReviewStatus,
    SodRule,
    System,
    User,
)
from helpdesk.shared.policy.sod import held_conflicts
from helpdesk.shared.policy.types import SodRuleSnapshot

__all__ = ["open_review", "decide_item"]


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
    opened_by_user_id: uuid.UUID | None,
    due_at: datetime | None = None,
    actor_ref: str | None = None,
    actor_kind: str = USER_ACTOR_KIND,
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

    `opened_by_user_id` is nullable and `actor_kind` defaults to
    `USER_ACTOR_KIND`: an earlier change's
    quarterly generator has no caller at all, and the alternative — a `user`
    kind carrying a system-shaped ref — would file an automatic sweep among
    people's decisions. The DEFAULT is what keeps that honest, and it is
    pinned rather than assumed by
    `test_access_review_generation.py::test_open_review_with_an_opener_id_and_no_actor_kind_still_writes_the_user_actor`,
    because no case before this plan read `actor_kind` off this event at all.

    Both are resolved through `revoke.resolve_actor_ref` — one implementation,
    so this engine and the two lifecycle ones cannot drift on what counts as
    an actor — and it refuses `VALIDATION` when the id and the ref are BOTH
    absent, before anything is read or written, so no `review.opened` can name
    an actor called `None`
    (`::test_open_review_with_neither_an_opener_id_nor_an_actor_ref_is_refused`).
    """
    resolved_actor_ref = resolve_actor_ref(actor_ref, opened_by_user_id)
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
        reviewer_user_id=_reviewer_for(scope, opened_by_user_id),
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
        actor_kind=actor_kind,
        actor_ref=resolved_actor_ref,
        session_id=session_id,
        now=now,
    )
    return OpenReviewResult(review=review, items=items, flag_counts=counts)


def _reviewer_for(scope: ReviewScope, opened_by_user_id: uuid.UUID | None) -> uuid.UUID:
    """The scope's owner, or the caller when it names nobody (decision F).

    `access_reviews.reviewer_user_id` is not nullable, so a scope with no owner
    opened by a caller with no id has nobody to review it. No registered caller
    produces that pair: every MCP call passes an opener, and an earlier change's
    generator skips an ownerless scope before it ever calls here (decision C),
    which is why this raises rather than inventing a reviewer and why no case
    reaches the branch.
    """
    if scope.owner_user_id is not None:
        return scope.owner_user_id
    if opened_by_user_id is None:  # pragma: no cover - no caller produces this pair
        raise UnassignableReviewError(
            "a scope with no owner needs an opener to be reviewed by somebody"
        )
    return opened_by_user_id


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


async def _append_opened(
    session: AsyncSession,
    *,
    review: AccessReview,
    scope: ReviewScope,
    period: str,
    item_count: int,
    flag_counts: Any,
    actor_kind: str,
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

    `actor_kind` is passed in rather than fixed to `_ACTOR_KIND` (an earlier change
    decision D): this is the ONLY event `open_review` composes (standing rule
    52), so it is also the only place a review opened by the quarterly
    generator can be told apart from one a manager asked for. Pinned on both
    sides by
    `test_access_review_generation.py::test_every_review_opened_event_the_generator_causes_carries_the_system_actor`
    and its default twin
    `::test_open_review_with_an_opener_id_and_no_actor_kind_still_writes_the_user_actor`.
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
