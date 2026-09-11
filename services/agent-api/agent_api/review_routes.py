"""GET /me/reviews, one review's items, and the decision on one of them (23h).

The three MCP tools an earlier change finished, reached over HTTP instead of over a turn
(23a decision A and 23c decision A, unchanged). Nothing here reads the database
and nothing here decides anything: the call crosses the MCP boundary, so RBAC,
the kill switch, the scope checks and the audit event apply to a keep or a
revoke the browser makes exactly as they do to one the model makes. a project rule
holds as written — the mutation is a typed MCP tool behind RBAC and this module
is transport.

**Identity comes from the token and from nowhere else**. The
caller's own bearer is forwarded verbatim, no route takes a user or a tenant
argument, and the reviewer in the derived key is read from the VERIFIED claims
rather than from anything the browser sent. Pinned by
`test_review_routes.py::test_the_key_is_derived_from_the_tokens_reviewer_and_never_from_the_body`,
which sends a `reviewer_user_id` for another user of the SAME tenant and
asserts the arguments as an exact key set, so a field riding along fails the
case too.

**The body is the tool's structured output, unchanged**, for the same reason
the `/me` and `/me/approvals` reads are: a route that reshaped on the way out
would be a second contract for 23f's page to track.

**The status word, the decision word and the page size stay the TOOL's**
(§5's second risk). A second enum here would refuse at FastAPI's untyped 422 a
value the tool decides, and the design's `VALIDATION` — dressed as a 400 by the
shared `STATUS_FOR_CODE` — is what 23f has a sentence for. Pinned at both ends
of the page size by
`::test_the_inclusive_ends_of_the_tools_page_size_are_accepted` and
`::test_a_page_size_outside_the_tools_bounds_is_a_typed_400_with_no_read`.

The decision's path is `/review-items/{item_id}/decide` and not
`/reviews/items/…` (decision C): the second would put a static segment beside a
dynamic `[reviewId]` in the Next tree, where only a framework precedence rule
keeps the two apart.

No lifespan resource is touched: these routes read nothing off `app.state`,
which is why their tests enter no lifespan and open no socket.
"""

import uuid

from fastapi import APIRouter, Header, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from agent_api.deps import authenticated, call_tool_route, unauthenticated
from agent_api.graph.keys import derive_review_decision_key

router = APIRouter()


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


@router.get("/me/reviews")
async def my_reviews(
    authorization: str | None = Header(default=None),
    status: str | None = Query(default=None),
    limit: int | None = Query(default=None),
) -> JSONResponse:
    """Every access review waiting on THIS caller, newest period first.

    The queue's whole filter is "mine", and that "mine" is the token's user:
    the tool declares no user argument and this route sends none. Both query
    parameters are forwarded only when asked for (decision D) — 23e put the
    status filter on the tool because a completed review is most of the table
    after two quarters, and a proxy that could not ask for one would make that
    filter unreachable from the only caller there is.
    """
    arguments: ListReviewsArgs
    if status is None:
        arguments = ListReviewsArgs() if limit is None else ListReviewsArgsWithLimit(limit=limit)
    elif limit is None:
        arguments = ListReviewsArgsWithStatus(status=status)
    else:
        arguments = ListReviewsArgsWithStatusAndLimit(status=status, limit=limit)
    return await call_tool_route(authorization, "list_reviews", arguments)


@router.get("/reviews/{review_id}/items")
async def review_items(
    review_id: uuid.UUID,
    authorization: str | None = Header(default=None),
    limit: int | None = Query(default=None),
) -> JSONResponse:
    """One review's rows, as 23e widened them, for whoever may read that review.

    `review_id` is a `uuid.UUID` path parameter, so a non-uuid path never
    reaches a tool or a query
    (`::test_a_non_uuid_path_parameter_reaches_no_tool_and_no_key`). Whether
    this caller may see the review at all is the tool's scope check, not this
    router's: an unknown or out-of-scope review crosses back typed and is
    dressed by the shared map.
    """
    arguments = (
        ListReviewItemsArgs(review_id=review_id)
        if limit is None
        else ListReviewItemsArgsWithLimit(review_id=review_id, limit=limit)
    )
    return await call_tool_route(authorization, "list_review_items", arguments)


@router.post("/review-items/{item_id}/decide")
async def decide(
    item_id: uuid.UUID,
    body: DecideBody,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    """Record this caller's keep or revoke on one row of one review.

    `item_id` is a `uuid.UUID` path parameter for the reason `review_id` is,
    and one more: a `str` id would digest happily into a perfectly valid key
    filed against nothing.

    The claims are read here for the reviewer the key is derived from, which
    means the token is verified twice on this path: once here and once inside
    `call_tool_route`. Two copies of one guard, so neither can be killed on its
    own — the 401 cases pin the ANSWER, not this branch, and
    deleting either copy leaves the other answering exactly the same way.

    The decision word is in the key's material, so a Keep and a Revoke on one
    row are two different keys: a reviewer changing their mind then meets the
    tool's own re-decision refusal rather than being handed the stored Keep
    back as if it had worked
    (`::test_one_reviewer_deciding_one_item_twice_sends_one_key_and_changing_the_word_changes_it`).
    """
    verified = authenticated(authorization)
    if verified is None:
        return unauthenticated()
    _token, claims = verified

    arguments = DecideReviewItemArgs(
        item_id=item_id,
        decision=body.decision,
        idempotency_key=derive_review_decision_key(
            reviewer_user_id=uuid.UUID(claims.sub), item_id=item_id, decision=body.decision
        ),
    )
    return await call_tool_route(authorization, "decide_review_item", arguments)
