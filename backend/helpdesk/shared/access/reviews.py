"""Opening and deciding access reviews. Reads are in `review_queries.py`."""

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

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
from helpdesk.shared.crud.reviews import (
    find_first_undecided_item_id,
    find_open_review_for_scope,
    reread_review_item,
)
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

    Locked first, so two callers cannot both pass the duplicate check below.
    Another tenant's id reads as missing.
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
    """One open review per scope and period.

    Checked under the scope lock, so the second of two callers sees the
    first one's review. A unique index would fail mid-transaction with a
    database error instead of a typed refusal.
    """
    existing = await find_open_review_for_scope(
        session,
        department_id=scope.department_id,
        system_id=scope.system_id,
        period=period,
        open_statuses=list(_OPEN_STATUSES),
    )
    if existing is not None:
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

    The reviewer is the scope's owner (department manager or system owner),
    never the caller's pick; the caller is used only when the scope has no
    owner. The nightly job opens reviews with no caller at all.

    A scope with no live grants still gets a review, marked `completed` at
    once: nothing could ever complete it later, and "nobody holds anything"
    is a true answer. No cap on items: a cap would hide a grant from the
    only person who can review it. The opened event comes last, since it
    needs the item count and flag tally. A missing opener id and a missing
    actor ref together are refused before anything is read.
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

    # A review with nothing to decide is finished on arrival; no later call
    # could ever complete it.
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
    """The scope's owner, or the caller when it names nobody.

    No current caller sends an ownerless scope with no opener; this raises
    rather than invent a reviewer.
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

    Template and held roles are read once per holder and cached. Conflicts
    are checked against everything the holder holds in the whole tenant,
    not just this scope.
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
    """The `review.opened` event, with the review as subject.

    `item_count` is stored beside the flag tally, so an empty review reads
    as checked and empty, not as a cut-short record. `actor_kind` comes in
    so the nightly job's reviews can be told from a manager's.
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

    `keep` only records: extending access is a new grant, so it needs a new
    request. `revoke` hands the row to the revoke module, the only writer
    of `grant.revoked`.

    The grant row is locked first, then the item is read again. Locking the
    grant keeps the same lock order as a manual revoke, so the two cannot
    deadlock, and the second of two racing reviewers sees the first one's
    decision and is refused. An item whose grant was already revoked still
    records the decision and revokes nothing; refusing would leave a review
    nobody could finish.
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
        # `is_active_grant`, not `is_live_grant`: a past-expiry but still
        # `active` row is what the review flagged `stale`, and the reviewer
        # must be able to end it.
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
    """The item's current row, read again now the grant row is held.

    `populate_existing=True` matters: a plain read would return the cached
    row from before the wait, and the loser of a race would overwrite the
    winner's decision.
    """
    return await reread_review_item(session, item)


async def _complete_if_finished(
    session: AsyncSession, review_id: uuid.UUID, *, now: datetime
) -> tuple[AccessReview, bool]:
    """Close the review if this decision was its last undecided item.

    Counts only this review's items. Returns the review either way, so the
    caller needs no second read.
    """
    undecided_id = await find_first_undecided_item_id(session, review_id)
    review = await Repository(session, AccessReview).get(review_id)
    if review is None:  # pragma: no cover - the item's FK guarantees the row
        raise NotFoundError("no such access review in this tenant")
    if undecided_id is not None:
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
    """The `review.item_decided` event, with the item as subject.

    User and role come from the grant, since the item names only the grant.
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
