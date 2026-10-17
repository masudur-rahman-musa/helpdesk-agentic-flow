"""Pydantic argument shapes for the review routes."""

import uuid

from pydantic import BaseModel


class ListReviewsArgs(BaseModel):
    """`list_reviews` with neither a status filter nor a page size.

    The tool's defaults then apply (every status). The narrower shapes are
    subclasses, not nullable fields, because `model_dump` would send `null`.
    """


class ListReviewsArgsWithStatus(ListReviewsArgs):
    """`list_reviews` filtered to one status word, forwarded untouched."""

    status: str


class ListReviewsArgsWithLimit(ListReviewsArgs):
    """`list_reviews` with the caller's own page size and every status."""

    limit: int


class ListReviewsArgsWithStatusAndLimit(ListReviewsArgs):
    """`list_reviews` with both a status and a page size."""

    status: str
    limit: int


class ListReviewItemsArgs(BaseModel):
    """`list_review_items` for one review, at the tool's own page size."""

    review_id: uuid.UUID


class ListReviewItemsArgsWithLimit(ListReviewItemsArgs):
    """The same read with the caller's page size."""

    limit: int


class DecideReviewItemArgs(BaseModel):
    """`decide_review_item`'s arguments, built by the route.

    The route makes the idempotency key, so a reused browser key cannot clash.
    No reviewer field: the tool reads the reviewer from the token.
    """

    item_id: uuid.UUID
    decision: str
    idempotency_key: str


class DecideBody(BaseModel):
    """One decision word, checked by the tool. Other keys, like a reviewer id, are dropped."""

    decision: str
