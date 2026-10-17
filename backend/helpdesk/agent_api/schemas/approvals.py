"""Pydantic models for the approval queue routes."""

import uuid

from pydantic import BaseModel


class ListPendingApprovalsArgs(BaseModel):
    """`list_pending_approvals` with no page size, so the tool's default applies.

    The limit lives on a subclass, not a nullable field, because `model_dump`
    would send `limit: null` and the tool refuses it.
    """


class ListPendingApprovalsArgsWithLimit(ListPendingApprovalsArgs):
    """`list_pending_approvals` with the caller's own page size."""

    limit: int


class ApproveRequestArgs(BaseModel):
    """`approve_request`'s arguments, built by the route.

    The route makes the idempotency key. A key from the browser could be reused
    across two requests and clash by accident.
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
    """The browser's half of an approval: an optional comment, checked by the tool."""

    comment: str | None = None


class RejectBody(BaseModel):
    """The browser's half of a rejection. An empty reason reaches the tool and is refused there."""

    reason: str
