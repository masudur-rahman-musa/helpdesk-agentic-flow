"""Argument models for the /me, /me/requests and /me/access routes."""

from pydantic import BaseModel


class ListMyRequestsArgs(BaseModel):
    """`list_my_requests` with no page size, so the tool's default applies.

    The limit lives on a subclass, not a nullable field, because `model_dump`
    would send `limit: null` and the tool refuses it.
    """

    status: str | None = None
    """Left a plain string so the tool alone checks it and answers a typed 400."""


class ListMyRequestsArgsWithLimit(ListMyRequestsArgs):
    """`list_my_requests` with the caller's own page size."""

    limit: int


class ListMyAccessArgs(BaseModel):
    """No arguments: the grants listed are the token user's own."""


class GetMeArgs(BaseModel):
    """No arguments: the person is the token's own."""
