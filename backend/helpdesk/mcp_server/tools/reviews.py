"""The review tools: open a review, list reviews, read its items, decide one.

Scope is checked before existence: a caller whose scope does not reach an id
gets `FORBIDDEN` whether or not it exists, so ids reveal nothing. The
caller's department is read from the directory, never the token.
"""

import re
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.mcp_server.tools.base import MutatingToolInput
from helpdesk.mcp_server.tools.context import ToolContext
from helpdesk.shared.access.approvals import request_in_scope
from helpdesk.shared.access.review_queries import (
    find_item,
    find_review,
    list_items_with_context,
    list_owned_system_ids,
    list_reviews_for_scope,
)
from helpdesk.shared.access.reviews import (
    ItemAlreadyDecidedError as ItemAlreadyDecidedError,
)
from helpdesk.shared.access.reviews import ReviewAlreadyOpenError as ReviewAlreadyOpenError
from helpdesk.shared.access.reviews import (
    decide_item,
    open_review,
)
from helpdesk.shared.auth import rbac
from helpdesk.shared.core.errors import NotFoundError
from helpdesk.shared.crud import Repository
from helpdesk.shared.models import (
    PERIOD_LENGTH,
    AccessReview,
    ReviewDecision,
    ReviewFlag,
    ReviewStatus,
    System,
)

PERIOD_PATTERN = re.compile(r"\d{4}-Q[1-4]")
r"""One quarter, `YYYY-Qn`, used with `fullmatch` so no trailing newline or space gets in."""

_IGNORE_EXTRAS = ConfigDict(extra="ignore")

LIMIT_MIN = 1
LIMIT_DEFAULT = 50
LIMIT_MAX = 200


class OpenAccessReviewInput(MutatingToolInput):
    """Extras are refused: a stray `reviewer_user_id` must not be quietly dropped."""

    department_id: uuid.UUID | None = None
    system_id: uuid.UUID | None = None
    period: str = Field(min_length=1, max_length=PERIOD_LENGTH)

    @field_validator("period")
    @classmethod
    def _period_is_one_quarter(cls, value: str) -> str:
        """Checked with `fullmatch`, not `pattern=`; see `PERIOD_PATTERN`."""
        if PERIOD_PATTERN.fullmatch(value) is None:
            raise ValueError("period must be a quarter of the form YYYY-Qn, n in 1..4")
        return value

    @model_validator(mode="after")
    def _exactly_one_scope(self) -> "OpenAccessReviewInput":
        """Exactly one scope, refused as `VALIDATION` before the table constraint could fail."""
        if (self.department_id is None) == (self.system_id is None):
            raise ValueError("exactly one of department_id or system_id must be set")
        return self


class OpenAccessReviewOutput(BaseModel):
    review_id: uuid.UUID
    department_id: uuid.UUID | None
    system_id: uuid.UUID | None
    period: str
    reviewer_user_id: uuid.UUID
    """The scope's owner, or the caller when the scope has none."""

    status: str
    item_count: int
    flag_counts: dict[str, int]


class ListReviewItemsInput(BaseModel):
    model_config = _IGNORE_EXTRAS

    review_id: uuid.UUID
    limit: int = Field(default=LIMIT_DEFAULT, ge=LIMIT_MIN, le=LIMIT_MAX)
    after_item_id: uuid.UUID | None = None
    """Start just after this item. Only ever a `next_after_item_id` handed back."""


class ReviewItemOutput(BaseModel):
    """One item, with the grant behind it named, since a reviewer cannot decide a bare id."""

    item_id: uuid.UUID
    grant_id: uuid.UUID
    flag: ReviewFlag
    decision: str | None
    decided_at: datetime | None
    target_display_name: str
    role_name: str
    system_name: str
    privilege: str
    granted_at: datetime
    expires_at: datetime | None


class ListReviewItemsOutput(BaseModel):
    review_id: uuid.UUID
    items: list[ReviewItemOutput]
    next_after_item_id: uuid.UUID | None = None
    """Where the next page starts, or None when this page is the last."""


class ListReviewsInput(BaseModel):
    """No user or tenant argument. No status means every status, not just open."""

    model_config = _IGNORE_EXTRAS

    status: ReviewStatus | None = None
    limit: int = Field(default=LIMIT_DEFAULT, ge=LIMIT_MIN, le=LIMIT_MAX)


class ReviewQueueRow(BaseModel):
    """One review in the queue. Counts ride on the row so a page needs no extra calls."""

    review_id: uuid.UUID
    period: str
    status: str
    due_at: datetime | None
    completed_at: datetime | None
    department_id: uuid.UUID | None
    department_name: str | None
    system_id: uuid.UUID | None
    system_name: str | None
    reviewer_user_id: uuid.UUID
    reviewer_display_name: str
    item_count: int
    decided_count: int


class ListReviewsOutput(BaseModel):
    reviews: list[ReviewQueueRow]


class DecideReviewItemInput(MutatingToolInput):
    """Extras are refused. The revoke reason is fixed in code; no caller supplies it."""

    item_id: uuid.UUID
    decision: ReviewDecision


class DecideReviewItemOutput(BaseModel):
    item_id: uuid.UUID
    review_id: uuid.UUID
    decision: ReviewDecision
    decided_at: datetime | None
    revoked_grant_count: int
    """1 only when this decision ended a grant, so the caller sees what it did."""

    review_complete: bool
    """Whether this was the review's last open item; the record of a review closing."""


async def _in_scope(
    ctx: ToolContext,
    tool: rbac.Tool,
    *,
    department_id: uuid.UUID | None,
    system_owner_user_id: uuid.UUID | None,
) -> bool:
    """Does the caller's scope on this tool reach this department or system owner?

    The caller's department comes from the directory, so a manager who moved
    cannot act on their old team.
    """
    caller = await ctx.directory.lookup_user(ctx.identity.user_id)
    return request_in_scope(
        rbac.authorize(ctx.identity.claims, tool).scopes,
        approver_user_id=ctx.identity.user_id,
        approver_department_id=None if caller is None else caller.department_id,
        target_department_id=department_id,
        system_owner_user_id=system_owner_user_id,
    )


async def open_access_review(
    ctx: ToolContext, args: OpenAccessReviewInput
) -> OpenAccessReviewOutput:
    """Open a review over one department or one system for one period.

    `FORBIDDEN` when out of scope, raised as `RbacDenied` so it is logged as a
    denial. `POLICY_BLOCKED` when this scope already has an open review for the
    period; a row lock means two openers leave one review.
    """
    system_owner_user_id = None
    if args.system_id is not None:
        # Not turned into `NOT_FOUND` here: a missing system just has no owner,
        # so an out-of-scope caller gets `FORBIDDEN` either way.
        system = await Repository(ctx.session, System).get(args.system_id)
        system_owner_user_id = None if system is None else system.owner_user_id
    if not await _in_scope(
        ctx,
        rbac.Tool.OPEN_ACCESS_REVIEW,
        department_id=args.department_id,
        system_owner_user_id=system_owner_user_id,
    ):
        raise rbac.RbacDenied(rbac.Tool.OPEN_ACCESS_REVIEW.value)

    result = await open_review(
        ctx.session,
        ctx.directory,
        department_id=args.department_id,
        system_id=args.system_id,
        period=args.period,
        opened_by_user_id=ctx.identity.user_id,
        actor_ref=str(ctx.identity.user_id),
        session_id=ctx.identity.session_id,
        now=ctx.now,
    )
    return OpenAccessReviewOutput(
        review_id=result.review.id,
        department_id=result.review.department_id,
        system_id=result.review.system_id,
        period=result.review.period,
        reviewer_user_id=result.review.reviewer_user_id,
        status=result.review.status,
        item_count=len(result.items),
        flag_counts=dict(result.flag_counts),
    )


async def list_review_items(ctx: ToolContext, args: ListReviewItemsInput) -> ListReviewItemsOutput:
    """The items of one review this caller may reach, oldest first.

    The system owner is read now, so a former owner loses access.
    """
    review = await find_review(ctx.session, args.review_id)
    if not await _in_scope(
        ctx,
        rbac.Tool.LIST_REVIEW_ITEMS,
        department_id=None if review is None else review.department_id,
        system_owner_user_id=await _owner_of(ctx.session, review),
    ):
        raise rbac.RbacDenied(rbac.Tool.LIST_REVIEW_ITEMS.value)
    if review is None:
        raise NotFoundError("no such access review in this tenant")

    # One row past the page says whether another page exists.
    rows = await list_items_with_context(
        ctx.session, review.id, limit=args.limit + 1, after_item_id=args.after_item_id
    )
    items = rows[: args.limit]
    return ListReviewItemsOutput(
        review_id=review.id,
        next_after_item_id=items[-1].item.id if len(rows) > args.limit else None,
        items=[
            ReviewItemOutput(
                item_id=row.item.id,
                grant_id=row.item.grant_id,
                flag=ReviewFlag(row.item.flag),
                decision=row.item.decision,
                decided_at=row.item.decided_at,
                target_display_name=row.target_display_name,
                role_name=row.role_name,
                system_name=row.system_name,
                privilege=row.privilege,
                granted_at=row.granted_at,
                expires_at=row.expires_at,
            )
            for row in items
        ],
    )


async def list_reviews(ctx: ToolContext, args: ListReviewsInput) -> ListReviewsOutput:
    """The reviews this caller's scope reaches, oldest due first.

    Filtered by scope, not by named reviewer: a department's review belongs to
    whoever manages it now. Reaching nothing gives an empty list.
    """
    scopes = rbac.authorize(ctx.identity.claims, rbac.Tool.LIST_REVIEWS).scopes
    caller = await ctx.directory.lookup_user(ctx.identity.user_id)
    department_id = (
        caller.department_id if rbac.Scope.OWN_DEPARTMENT in scopes and caller is not None else None
    )
    system_ids = (
        await list_owned_system_ids(ctx.session, ctx.identity.user_id)
        if rbac.Scope.OWN_SYSTEMS in scopes
        else frozenset()
    )

    rows = await list_reviews_for_scope(
        ctx.session,
        department_id=department_id,
        system_ids=system_ids,
        any_scope=rbac.Scope.ANY in scopes,
        status=args.status,
        limit=args.limit,
    )
    return ListReviewsOutput(
        reviews=[
            ReviewQueueRow(
                review_id=row.review.id,
                period=row.review.period,
                status=row.review.status,
                due_at=row.review.due_at,
                completed_at=row.review.completed_at,
                department_id=row.review.department_id,
                department_name=row.department_name,
                system_id=row.review.system_id,
                system_name=row.system_name,
                reviewer_user_id=row.review.reviewer_user_id,
                reviewer_display_name=row.reviewer_display_name,
                item_count=row.item_count,
                decided_count=row.decided_count,
            )
            for row in rows
        ]
    )


async def decide_review_item(
    ctx: ToolContext, args: DecideReviewItemInput
) -> DecideReviewItemOutput:
    """Record a reviewer's keep-or-revoke on one item, and act on it.

    Scope is checked before existence, like the other review tools. A row lock
    means two deciders leave one decision and one `POLICY_BLOCKED`.
    """
    item = await find_item(ctx.session, args.item_id)
    review = None if item is None else await find_review(ctx.session, item.review_id)
    if not await _in_scope(
        ctx,
        rbac.Tool.DECIDE_REVIEW_ITEM,
        department_id=None if review is None else review.department_id,
        system_owner_user_id=await _owner_of(ctx.session, review),
    ):
        raise rbac.RbacDenied(rbac.Tool.DECIDE_REVIEW_ITEM.value)
    if item is None:
        raise NotFoundError("no such access review item in this tenant")

    result = await decide_item(
        ctx.session,
        ctx.directory,
        item=item,
        decision=args.decision,
        decided_by_user_id=ctx.identity.user_id,
        actor_ref=str(ctx.identity.user_id),
        session_id=ctx.identity.session_id,
        now=ctx.now,
    )
    return DecideReviewItemOutput(
        item_id=result.item.id,
        review_id=result.item.review_id,
        # The value just written; the column is typed `str | None`.
        decision=args.decision,
        decided_at=result.item.decided_at,
        revoked_grant_count=result.revoked_grant_count,
        review_complete=result.review_complete,
    )


async def _owner_of(session: AsyncSession, review: AccessReview | None) -> uuid.UUID | None:
    """The review's system owner, or None, so missing and department reviews look alike."""
    if review is None or review.system_id is None:
        return None
    system = await Repository(session, System).get(review.system_id)
    return None if system is None else system.owner_user_id


__all__ = [
    "LIMIT_DEFAULT",
    "LIMIT_MAX",
    "LIMIT_MIN",
    "PERIOD_PATTERN",
    "DecideReviewItemInput",
    "DecideReviewItemOutput",
    "ItemAlreadyDecidedError",
    "ListReviewItemsInput",
    "ListReviewItemsOutput",
    "ListReviewsInput",
    "ListReviewsOutput",
    "OpenAccessReviewInput",
    "OpenAccessReviewOutput",
    "ReviewAlreadyOpenError",
    "ReviewItemOutput",
    "ReviewQueueRow",
    "decide_review_item",
    "list_review_items",
    "list_reviews",
    "open_access_review",
]
