"""Pydantic models for the approval queue routes."""

import uuid

from pydantic import BaseModel


class ListPendingApprovalsArgs(BaseModel):
    """`list_pending_approvals` with no page size asked for.

    The tool's own `LIMIT_DEFAULT` then applies. Repeating that number here
    would be a second place for it to drift from, which is
    why the limited call is a SUBCLASS below rather than a nullable field:
    `model_dump` carries every declared field, so a `limit: int | None = None`
    would send `limit: null` to a tool whose bound is 1..200 inclusive and
    would answer `VALIDATION` for it.
    """


class ListPendingApprovalsArgsWithLimit(ListPendingApprovalsArgs):
    """`list_pending_approvals` with the caller's own page size."""

    limit: int


class ApproveRequestArgs(BaseModel):
    """`approve_request`'s the design arguments, assembled by this router.

    `idempotency_key` is DERIVED here and never accepted from the browser
    (decision B): a client-supplied key is an identifier the client is free to
    reuse across two different requests, which is `IDEMPOTENCY_CONFLICT` taken
    by accident.
    """

    request_id: uuid.UUID
    comment: str | None
    idempotency_key: str


class RejectRequestArgs(BaseModel):
    """`reject_request`'s arguments. The reason's bounds are the tool's."""

    request_id: uuid.UUID
    reason: str
    idempotency_key: str


class ApproveBody(BaseModel):
    """The browser's half of an approval: an optional comment and nothing else.

    No `max_length` here on purpose (decision E). The tool's
    `COMMENT_MAX_LENGTH` is the only validator, so a comment of exactly 2000
    characters is accepted and 2001 crosses back as the typed `VALIDATION`
    this router dresses as a 400 — pinned at both ends by
    `test_approval_routes.py::test_a_comment_of_exactly_the_tools_maximum_is_accepted`
    and `::test_a_comment_one_character_over_the_maximum_is_a_typed_400_with_no_decision`.
    """

    comment: str | None = None


class RejectBody(BaseModel):
    """The browser's half of a rejection.

    `reason` is required but UNBOUNDED here: an empty string is forwarded as
    it arrived, so the tool's `REASON_MIN_LENGTH` refuses it typed rather than
    this router dropping it to `None` and having the tool report a missing
    field instead (`::test_an_empty_reason_is_a_typed_400_with_no_decision`).
    """

    reason: str
