"""The three review tools: opening one (12c), reading its items (12c), and
deciding one of them (12e).

`open_access_review` is the design's `open_access_review(department_id |
system_id, period, idempotency_key)`. Everything it decides lives in
`helpdesk.access.reviews`; what lives here is the wire contract, the two
input pre-checks, and the the design scope question the domain deliberately does
not ask.

Scope goes through an earlier change's `request_in_scope` with review-derived
arguments — one scope authority for approvals, revocation and now reviews —
and every department id it compares is read from the DIRECTORY, never from
the token's `department_id` claim. The user id itself still comes only from
the token.

The scope check runs BEFORE the scope row's existence is tested, so a caller
whose the design row does not reach a department or a system learns `FORBIDDEN`
whether or not that id exists, and the id is neither an existence nor a state
oracle. For a department the check needs no read at all — it compares the
caller's own department to the argument. For a system it has to read the
row to learn its owner, and a missing or foreign system answers with no owner
and therefore with the same `FORBIDDEN` a system owner gets for somebody
else's system; only an `any`-scope caller (`hr`, `it_admin`,
`platform_admin`) reaches the `NOT_FOUND` behind it, which is a caller
already entitled to know.

`review.opened` is appended by `helpdesk.access.reviews` itself, and the
registry declares the generic `tool.succeeded` (decision K): the pipeline's
own event carries a fixed payload and the TOOL as its subject, so as the
declared type it would record that somebody called a tool and not which scope
was reviewed. The pipeline's event lands last, as the summary — the same
shape as an earlier change's `grant.revoked` and 12a/12b/12d's lifecycle events.

No function here opens a session or touches the transport: the pipeline binds
the session to the token's tenant before the handler is called.
"""

import re
import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.mcp_server.core.identity import Identity
from helpdesk.mcp_server.tools.base import MutatingToolInput
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
from helpdesk.shared.services.access import (
    ItemAlreadyDecidedError as ItemAlreadyDecidedError,  # re-exported for the tests
)
from helpdesk.shared.services.access import (
    ReviewAlreadyOpenError as ReviewAlreadyOpenError,  # re-exported for the tests
)
from helpdesk.shared.services.access import (
    SimulatedDirectory,
    decide_item,
    find_item,
    find_review,
    list_items_with_context,
    list_owned_system_ids,
    list_reviews_for_scope,
    open_review,
    request_in_scope,
)

PERIOD_PATTERN = re.compile(r"\d{4}-Q[1-4]")
r"""What a review period may look like (decision H).

Used with `re.fullmatch` and never `match`, `search` or Pydantic's
`Field(pattern=...)`: all three of those are PARTIAL
matches, so `"2026-Q1\n"`, `"2026-Q1\r\n"` and `"2026-Q1 "` pass them and the
control character rides into the column. Those three cases, plus `2026-Q0`,
`2026-Q5` and an over-length value, are the rejection table in
`test_mcp_reviews.py::test_the_period_bounds_are_inclusive_and_reject_before_any_write`;
a non-anchored match accepts the first three, which is what tells `fullmatch`
apart here.

The shape is deliberately narrower than the column: `access_reviews.period`
is `String(PERIOD_LENGTH)` so row 21's scheduler can widen the vocabulary
without a migration, and until it does, a period is one quarter.
"""

_IGNORE_EXTRAS = ConfigDict(extra="ignore")
"""The read tool's contract: identity arrives on the token, so a spare
argument is data to discard rather than a reason to fail the call. The
mutating input below FORBIDS extras instead, through `MutatingToolInput`."""

LIMIT_MIN = 1
LIMIT_DEFAULT = 50
LIMIT_MAX = 200
"""Bounds on `list_review_items` (decision I), the same as an earlier change's queue.

Items go over the wire and nothing downstream bounds them, so an absent cap
is an unbounded response. Pinned exactly, and the default read off the input
model, by
`test_mcp_reviews.py::test_the_limit_boundary_is_inclusive_at_1_and_200`
and `::test_the_default_limit_is_exactly_fifty`."""


class OpenAccessReviewInput(MutatingToolInput):
    """the design's signature. `extra="forbid"` comes from `MutatingToolInput`.

    Forbidding extras matters here for the same reason it does on the
    lifecycle tools: a `tenant_id` or a `reviewer_user_id` in the arguments is
    a caller who believes they are choosing something they are not, and
    dropping it silently would open the review under a success they read as
    something else. The reviewer is never an argument at all (decision F).
    """

    department_id: uuid.UUID | None = None
    system_id: uuid.UUID | None = None
    period: str = Field(min_length=1, max_length=PERIOD_LENGTH)
    """The column's own length, imported rather than retyped (standing rule
    1), so the pre-check and the storage cannot drift. The shape below is
    narrower still, so the length bound is a floor under a future vocabulary
    rather than the thing that refuses today's bad values."""

    @field_validator("period")
    @classmethod
    def _period_is_one_quarter(cls, value: str) -> str:
        """`re.fullmatch`, and deliberately NOT Pydantic's `pattern=` — see
        `PERIOD_PATTERN`, whose docstring names the cases that tell them apart.
        """
        if PERIOD_PATTERN.fullmatch(value) is None:
            raise ValueError("period must be a quarter of the form YYYY-Qn, n in 1..4")
        return value

    @model_validator(mode="after")
    def _exactly_one_scope(self) -> "OpenAccessReviewInput":
        """Exactly one of `department_id` / `system_id`, refused before any effect.

        Neither and both are the same refusal (`VALIDATION`, the pipeline's
        argument-parse code), and it happens at input parsing, so no row is
        written and no scope row is even read. A review of "everything" and a
        review of two different things are both scopes nobody could answer
        for, and the table's own check constraint would refuse the second
        with an `IntegrityError` mid-transaction rather than a typed SPEC
        §8.1 code. Pinned by
        `test_mcp_reviews.py::test_neither_scope_and_both_scopes_are_each_refused_with_no_row`.
        """
        if (self.department_id is None) == (self.system_id is None):
            raise ValueError("exactly one of department_id or system_id must be set")
        return self


class OpenAccessReviewOutput(BaseModel):
    review_id: uuid.UUID
    department_id: uuid.UUID | None
    system_id: uuid.UUID | None
    period: str
    reviewer_user_id: uuid.UUID
    """Who the review is FOR: the scope's own owner, or the caller when the
    scope names nobody (decision F). Part of the answer because a caller who
    opened a review for somebody else has to be told who that is."""

    status: str
    item_count: int
    flag_counts: dict[str, int]
    """Every `ReviewFlag`, zeros kept — see `OpenReviewResult.flag_counts`."""


class ListReviewItemsInput(BaseModel):
    model_config = _IGNORE_EXTRAS

    review_id: uuid.UUID
    limit: int = Field(default=LIMIT_DEFAULT, ge=LIMIT_MIN, le=LIMIT_MAX)


class ReviewItemOutput(BaseModel):
    """One item as a reviewer sees it, with the grant behind it named.

    The six context fields arrived with an earlier change (decision B), widened in
    place rather than added as a second tool: every caller of the item list
    needs them, because a reviewer cannot decide a `grant_id`, and a second
    tool would be a second call on every page load and a second scope check to
    keep in step. Each one is read from the row its id points at, off the same
    joined read — pinned by
    `test_mcp_reviews.py::test_list_review_items_answers_eleven_keys_naming_the_grant_behind_the_item`,
    whose grant holder carries a name no other row in that file has.
    """

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


class ListReviewsInput(BaseModel):
    """The queue's whole input: a page size and an optional status.

    No user and no tenant argument at all — both are the token's (hard rule
    6) — so there is nothing here for a caller to name somebody else with.
    Extras are ignored rather than forbidden, the read tool's contract.

    `status` is the `ReviewStatus` StrEnum itself and not a `str` with a
    validator (decision E): the enum IS the pre-check, so `"OPEN"`, a member
    with a trailing newline, one with a trailing space and an unknown word are
    each refused as `VALIDATION` at argument parsing, before any read. That is
    why no `re.fullmatch` validator appears here as it does on `period` —
    there is no pattern to get wrong. An absent `status` answers every status,
    never an implicit `open`: a completed review is most of the table after
    two quarters, and a page that cannot ask for one cannot show a reviewer
    what they already did. Pinned by
    `test_mcp_reviews.py::test_the_status_filter_is_an_exact_enum_member_or_refused_before_any_read`
    and `::test_each_status_answers_only_its_own_rows_and_an_absent_status_answers_both`.
    """

    model_config = _IGNORE_EXTRAS

    status: ReviewStatus | None = None
    limit: int = Field(default=LIMIT_DEFAULT, ge=LIMIT_MIN, le=LIMIT_MAX)
    """The same three constants the item list reads, never a second copy of
    the numbers, so the two page sizes cannot drift apart.
    Pinned by `::test_the_queue_reads_the_same_three_limit_constants_as_the_item_list`
    and `::test_the_queue_limit_boundary_is_inclusive_at_1_and_200`."""


class ReviewQueueRow(BaseModel):
    """One review of the queue: its scope, its reviewer, and its progress.

    Exactly one of the two id/name pairs is non-null on any row, because
    `access_reviews`' own check constraint allows exactly one scope id. The
    two counts are the design's "progress", carried ON the row so a page of
    fifty reviews needs no second call per review (decision D).
    """

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
    """the design's signature. `extra="forbid"` comes from `MutatingToolInput`.

    `decision` is the `ReviewDecision` StrEnum itself, not a `str` with a
    validator: the enum is the pre-check, so an unknown
    word, an empty string, a member with a trailing newline and a member with
    a trailing space are each refused as `VALIDATION` at argument parsing,
    before any effect runs and with no client string anywhere near the
    `review_items.decision` column. That is why no `re.fullmatch` validator
    appears here as it does on `period` above — there is no pattern to get
    wrong. Pinned, accepted and rejected rows alike, by
    `test_mcp_reviews.py::test_the_decision_word_is_an_exact_enum_member_or_refused_before_any_write`.

    Forbidding extras matters for the usual reason: a `tenant_id`, a
    `decided_by_user_id` or a `reason` in the arguments is a caller who
    believes they are choosing something they are not, and the reason is a
    module constant nobody may supply (decision E).
    """

    item_id: uuid.UUID
    decision: ReviewDecision


class DecideReviewItemOutput(BaseModel):
    item_id: uuid.UUID
    review_id: uuid.UUID
    decision: ReviewDecision
    decided_at: datetime | None
    revoked_grant_count: int
    """0 for every `keep`, and 0 for decision B's item whose grant was already
    revoked when the decision landed; 1 for a revocation that ended a row. The
    caller is told what their decision actually did, rather than being left to
    assume `revoke` always ends access."""

    review_complete: bool
    """Whether THIS decision was the review's last undecided item. There is no
    `review.completed` event and no `complete_review` tool, so this and the
    same key on `review.item_decided` are the whole record of a review
    closing (decision F/G)."""


async def _in_scope(
    session: AsyncSession,
    identity: Identity,
    tool: rbac.Tool,
    *,
    department_id: uuid.UUID | None,
    system_owner_user_id: uuid.UUID | None,
) -> bool:
    """Does the design's row on this tool reach this scope?

    `request_in_scope`'s parameters are already neutral — an actor, the
    actor's department, the subject's department, the system's owner — so
    approvals, revocation and reviews cannot answer the scope question
    differently. A department-scoped
    review passes the department as the "subject's department" and no system
    owner, so a `system_owner`-only caller is refused; a system-scoped review
    passes the owner and no department, so a `manager`-only caller is refused.
    That is the design's row read literally in both directions.

    The caller's department is read from the DIRECTORY, never from the
    token's `department_id` claim: a manager transferred after their token was
    issued must not open reviews in the department they left. Pinned by
    `test_mcp_reviews.py::test_the_managers_department_comes_from_the_directory_not_the_token`,
    which moves the caller's row and reuses the token minted before the move.
    """
    caller = await SimulatedDirectory(session).lookup_user(identity.user_id)
    return request_in_scope(
        rbac.authorize(identity.claims, tool).scopes,
        approver_user_id=identity.user_id,
        approver_department_id=None if caller is None else caller.department_id,
        target_department_id=department_id,
        system_owner_user_id=system_owner_user_id,
    )


async def open_access_review(
    session: AsyncSession, identity: Identity, args: OpenAccessReviewInput
) -> OpenAccessReviewOutput:
    """Open a review over one department or one system for one period.

    Two refusals here, then the domain's own:

    `FORBIDDEN` — the design's scope does not reach the scope row. Raised as
    `RbacDenied`, which the pipeline records as `tool.denied` rather than
    `tool.failed`: a permission decision filed as an outage would be a false
    record. Its own kill is
    `test_mcp_reviews.py::test_a_manager_may_not_open_a_review_outside_their_department`,
    whose caller the matrix admits, so only the department comparison can
    refuse it. As on `revoke_access`, the pipeline's matrix stage has no
    independent kill on this tool and that is a property of the code, not a
    gap: `_in_scope` re-runs `rbac.authorize`, which raises for a principal
    holding no cell on this row, so deleting the pipeline stage changes no
    answer here.

    `NOT_FOUND` — no such department or system, or one in another tenant.
    Raised inside `open_review`'s lock, which runs through the tenant-bound
    session so the two are indistinguishable
    (`::test_a_missing_and_another_tenants_scope_answer_the_same_not_found`).
    It is reachable only for a caller whose scope already reaches the id.

    `POLICY_BLOCKED` — a review of this scope for this period is already
    open, from `ReviewAlreadyOpenError`, refused under the scope row's lock so
    two concurrent openers leave exactly one review.

    `now` is read once and passed on, so the liveness filter that picked the
    grants, every staleness comparison and `review.opened` answer for one
    instant. Everything runs in the transaction the pipeline opened, so the
    review, its items and its event commit or roll back together.
    """
    system_owner_user_id = None
    if args.system_id is not None:
        # Read for the owner alone, and never turned into a `NOT_FOUND` here:
        # a missing or foreign system answers with no owner, so an out-of-scope
        # caller gets the same `FORBIDDEN` either way and the id says nothing
        # about what exists.
        system = await Repository(session, System).get(args.system_id)
        system_owner_user_id = None if system is None else system.owner_user_id
    if not await _in_scope(
        session,
        identity,
        rbac.Tool.OPEN_ACCESS_REVIEW,
        department_id=args.department_id,
        system_owner_user_id=system_owner_user_id,
    ):
        raise rbac.RbacDenied(rbac.Tool.OPEN_ACCESS_REVIEW.value)

    result = await open_review(
        session,
        SimulatedDirectory(session),
        department_id=args.department_id,
        system_id=args.system_id,
        period=args.period,
        opened_by_user_id=identity.user_id,
        actor_ref=str(identity.user_id),
        session_id=identity.session_id,
        now=datetime.now(UTC),
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


async def list_review_items(
    session: AsyncSession, identity: Identity, args: ListReviewItemsInput
) -> ListReviewItemsOutput:
    """The items of one review this caller may reach, oldest first.

    Scope is checked BEFORE the review's existence is answered, from the
    review's OWN scope columns: a review whose id is missing or in another
    tenant reads as no scope at all, so a `manager` or `system_owner` caller
    is refused `FORBIDDEN` without learning whether it exists, and only an
    `any`-scope caller reaches the `NOT_FOUND` behind it. Pinned by
    `::test_a_missing_and_another_tenants_review_answer_the_same_not_found`
    for the `any` caller and by
    `::test_a_manager_may_not_read_a_review_outside_their_department` for the
    scoped one.

    A system-scoped review's scope is its system's OWNER, re-read now rather
    than copied onto the review when it was opened: ownership moves, and a
    former owner holding an old review id must not keep reading it.
    """
    review = await find_review(session, args.review_id)
    if not await _in_scope(
        session,
        identity,
        rbac.Tool.LIST_REVIEW_ITEMS,
        department_id=None if review is None else review.department_id,
        system_owner_user_id=await _owner_of(session, review),
    ):
        raise rbac.RbacDenied(rbac.Tool.LIST_REVIEW_ITEMS.value)
    if review is None:
        raise NotFoundError("no such access review in this tenant")

    items = await list_items_with_context(session, review.id, limit=args.limit)
    return ListReviewItemsOutput(
        review_id=review.id,
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


async def list_reviews(
    session: AsyncSession, identity: Identity, args: ListReviewsInput
) -> ListReviewsOutput:
    """The reviews this caller's the design scope reaches, oldest due first.

    The queue the other three review tools never had: each of them
    takes an id, and nothing answered which ids a reviewer may act on. What it
    answers is decided by the design's own row — a `manager` their department's
    reviews, a `system_owner` those of the systems they own, an `any`-scope
    principal every one in the tenant — and never by a `reviewer_user_id`
    filter, because a department's review belongs to the manager who inherited
    the department as much as to the person named on it (decision A).

    The scope is resolved HERE, from `rbac` and the directory, and handed to
    `list_reviews_for_scope` already decided: nothing in `helpdesk/access/`
    reads a token. `rbac.authorize` is what refuses a principal holding no
    cell on this row, so the pipeline's matrix stage has no independent kill
    on this tool and that is a property of the code rather than a gap — the same shape 12c's two tools carry.

    The caller's department comes from the DIRECTORY, never from the token's
    `department_id` claim: a manager transferred after their token was issued
    must not keep the queue of the department they left.

    There is no `NOT_FOUND` here at all, and that is the point of criterion 11:
    the queue takes no id, so nothing can be missing, and a scope reaching no
    review answers an EMPTY list. An empty queue is a fact about the tenant —
    "nothing is waiting on you" — and a page that read it as a refusal would
    show a manager a failure instead. Pinned by
    `::test_a_scope_that_reaches_nothing_answers_an_empty_queue_and_never_not_found`.
    """
    scopes = rbac.authorize(identity.claims, rbac.Tool.LIST_REVIEWS).scopes
    caller = await SimulatedDirectory(session).lookup_user(identity.user_id)
    department_id = (
        caller.department_id if rbac.Scope.OWN_DEPARTMENT in scopes and caller is not None else None
    )
    system_ids = (
        await list_owned_system_ids(session, identity.user_id)
        if rbac.Scope.OWN_SYSTEMS in scopes
        else frozenset()
    )

    rows = await list_reviews_for_scope(
        session,
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
    session: AsyncSession, identity: Identity, args: DecideReviewItemInput
) -> DecideReviewItemOutput:
    """Record a reviewer's keep-or-revoke on one item, and act on it.

    Scope is checked BEFORE the item's existence (decision H), from the item's
    REVIEW, exactly as `list_review_items` above does it: an item id that is
    missing or in another tenant reads as no scope at all, so a `manager` or
    `system_owner` caller is refused `FORBIDDEN` without learning whether it
    exists, and only an `any`-scope caller reaches the `NOT_FOUND` behind it.
    Forced rather than chosen — 12c's two tools already answer in that order,
    and one family answering differently for the same id would make this tool
    the oracle the other two are not. Pinned by
    `::test_an_out_of_scope_item_is_refused_before_its_existence_is_tested` and
    `::test_a_missing_and_another_tenants_item_answer_the_same_not_found`.

    Every refusal in order:

    `FORBIDDEN` — the design's scope does not reach the item's review. Its own
    kills are `::test_a_manager_may_not_decide_an_item_of_another_departments_review`
    and `::test_a_system_owner_may_not_decide_an_item_of_a_system_they_do_not_own`,
    whose callers the matrix admits, so only the scope comparison can refuse
    them. As on 12c's two tools, the pipeline's matrix stage has no independent
    kill here and that is a property of the code, not a gap: `_in_scope`
    re-runs `rbac.authorize`, which raises for a principal holding no cell on
    this row, so deleting the pipeline stage changes no answer (standing rule
    20).

    `NOT_FOUND` — no such item, or one in another tenant, indistinguishable
    because `find_item` runs through the tenant-bound session.

    `POLICY_BLOCKED` — the item already carries a decision
    (`ItemAlreadyDecidedError`), refused under the grant row's lock so two
    concurrent deciders leave one decided item and one refusal.

    The caller's department is read from the DIRECTORY, never from the token's
    `department_id` claim, through the same `_in_scope` the other two use
    (`::test_the_deciders_department_comes_from_the_directory_not_the_token`).
    Everything runs in the transaction the pipeline opened, so the decision,
    the revocation, any completion and `review.item_decided` commit or roll
    back together.
    """
    item = await find_item(session, args.item_id)
    review = None if item is None else await find_review(session, item.review_id)
    if not await _in_scope(
        session,
        identity,
        rbac.Tool.DECIDE_REVIEW_ITEM,
        department_id=None if review is None else review.department_id,
        system_owner_user_id=await _owner_of(session, review),
    ):
        raise rbac.RbacDenied(rbac.Tool.DECIDE_REVIEW_ITEM.value)
    if item is None:
        raise NotFoundError("no such access review item in this tenant")

    result = await decide_item(
        session,
        SimulatedDirectory(session),
        item=item,
        decision=args.decision,
        decided_by_user_id=identity.user_id,
        actor_ref=str(identity.user_id),
        session_id=identity.session_id,
        now=datetime.now(UTC),
    )
    return DecideReviewItemOutput(
        item_id=result.item.id,
        review_id=result.item.review_id,
        # `args.decision` rather than the column read back: they are the same
        # value — `decide_item` just wrote it — and the column is `str | None`,
        # so re-parsing it would only add a widening the enum already forbids.
        decision=args.decision,
        decided_at=result.item.decided_at,
        revoked_grant_count=result.revoked_grant_count,
        review_complete=result.review_complete,
    )


async def _owner_of(session: AsyncSession, review: AccessReview | None) -> uuid.UUID | None:
    """The owner of a system-scoped review's system, or None.

    None for a department-scoped review and for a review that does not exist,
    which is what makes those two answer a `system_owner` caller identically.
    """
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
