"""Request/argument models for the /me, /me/requests and /me/access routes."""

from pydantic import BaseModel


class ListMyRequestsArgs(BaseModel):
    """`list_my_requests` with no page size asked for.

    The tool's own `REQUESTS_LIMIT_DEFAULT` then applies. Repeating that number
    here would be a second place for it to drift from (a project rule wants
    one constant, pinned once), which is why the limited call is a SUBCLASS
    below rather than a nullable field: `model_dump` carries every declared
    field, so a `limit: int | None = None` would send `limit: null` to a tool
    whose bound does not accept it.
    """

    status: str | None = None
    """Passed through as the string it arrived as, so the TOOL's input model
    is the one validator that decides enum membership and answers the typed
    `VALIDATION` this router dresses as a 400. A second enum here would refuse
    the same value with FastAPI's own untyped 422."""


class ListMyRequestsArgsWithLimit(ListMyRequestsArgs):
    """`list_my_requests` with the caller's own page size."""

    limit: int


class ListMyAccessArgs(BaseModel):
    """No arguments: the grants listed are the token user's own."""


class GetMeArgs(BaseModel):
    """No arguments: the person is the token's own."""
