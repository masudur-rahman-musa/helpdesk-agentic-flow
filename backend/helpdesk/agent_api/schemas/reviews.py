"""Pydantic argument shapes for the review routes (23h)."""

import uuid

from pydantic import BaseModel


class ListReviewsArgs(BaseModel):
    """`list_reviews` with neither a status filter nor a page size asked for.

    An absent status means EVERY status to the tool (23e decision E), and an
    absent limit means its own `LIMIT_DEFAULT`. Repeating either here would be
    a second place for them to drift from, which is why the
    three narrower shapes below are SUBCLASSES rather than nullable fields:
    `model_dump` carries every declared field, so a `limit: int | None = None`
    would send `limit: null` into a bound of 1..200 and answer `VALIDATION`,
    and a `status: str | None = None` would send `status: null` where absent
    means "every status". Pinned shape by shape by
    `test_review_routes.py::test_the_queue_route_sends_each_query_parameter_only_when_it_is_asked_for`.
    """


class ListReviewsArgsWithStatus(ListReviewsArgs):
    """`list_reviews` filtered to one status word, forwarded untouched."""

    status: str


class ListReviewsArgsWithLimit(ListReviewsArgs):
    """`list_reviews` with the caller's own page size and every status."""

    limit: int


class ListReviewsArgsWithStatusAndLimit(ListReviewsArgs):
    """`list_reviews` with both, the shape 23f's filtered paging sends."""

    status: str
    limit: int


class ListReviewItemsArgs(BaseModel):
    """`list_review_items` for one review, at the tool's own page size."""

    review_id: uuid.UUID


class ListReviewItemsArgsWithLimit(ListReviewItemsArgs):
    """The same read with the caller's page size, for the same reason as above."""

    limit: int


class DecideReviewItemArgs(BaseModel):
    """`decide_review_item`'s the design arguments, assembled by this router.

    `idempotency_key` is DERIVED here and never accepted from the browser
    (decision B): a client-supplied key is an identifier the client is free to
    reuse across two different items, which is `IDEMPOTENCY_CONFLICT` taken by
    accident. There is no reviewer field, by a project rule — the tool reads the
    reviewer from the same bearer this router forwards.
    """

    item_id: uuid.UUID
    decision: str
    idempotency_key: str


class DecideBody(BaseModel):
    """The browser's half of a review decision: one word, and nothing else.

    `decision` is a plain `str` and NOT a route-side enum (§5's second risk):
    the tool's `ReviewDecision` is the only membership check, so an unknown
    word crosses back as the typed `VALIDATION` this router dresses as a 400
    rather than FastAPI's untyped 422
    (`::test_a_decision_that_is_not_a_tool_enum_member_is_a_typed_400_with_no_write`).
    Any other key the browser sends is ignored rather than forwarded, which is
    what keeps a `reviewer_user_id` out of the arguments.
    """

    decision: str
