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
from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Query, Request

from helpdesk.agent_api.agent.keys import derive_review_decision_key
from helpdesk.agent_api.core.deps import request_claims
from helpdesk.agent_api.schemas.reviews import (
    DecideBody,
    DecideReviewItemArgs,
    ListReviewItemsArgs,
    ListReviewItemsArgsWithLimit,
    ListReviewsArgs,
    ListReviewsArgsWithLimit,
    ListReviewsArgsWithStatus,
    ListReviewsArgsWithStatusAndLimit,
)
from helpdesk.agent_api.services.tool_proxy import proxy_tool

router = APIRouter()


@router.get("/me/reviews")
async def my_reviews(
    request: Request,
    status: str | None = Query(default=None),
    limit: int | None = Query(default=None),
) -> Mapping[str, Any]:
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
    return await proxy_tool(request, "list_reviews", arguments)


@router.get("/reviews/{review_id}/items")
async def review_items(
    review_id: uuid.UUID,
    request: Request,
    limit: int | None = Query(default=None),
) -> Mapping[str, Any]:
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
    return await proxy_tool(request, "list_review_items", arguments)


@router.post("/review-items/{item_id}/decide")
async def decide(
    item_id: uuid.UUID,
    body: DecideBody,
    request: Request,
) -> Mapping[str, Any]:
    """Record this caller's keep or revoke on one row of one review.

    `item_id` is a `uuid.UUID` path parameter for the reason `review_id` is,
    and one more: a `str` id would digest happily into a perfectly valid key
    filed against nothing.

    The reviewer the key is derived from is read off the verified claims on
    `request.state`, which `RequireAuthMiddleware` put there after the
    one verify every request gets; the browser's own `reviewer_user_id` never
    decides it (a project rule,
    `::test_the_key_is_derived_from_the_tokens_reviewer_and_never_from_the_body`).

    The decision word is in the key's material, so a Keep and a Revoke on one
    row are two different keys: a reviewer changing their mind then meets the
    tool's own re-decision refusal rather than being handed the stored Keep
    back as if it had worked
    (`::test_one_reviewer_deciding_one_item_twice_sends_one_key_and_changing_the_word_changes_it`).
    """
    claims = request_claims(request)

    arguments = DecideReviewItemArgs(
        item_id=item_id,
        decision=body.decision,
        idempotency_key=derive_review_decision_key(
            reviewer_user_id=uuid.UUID(claims.sub), item_id=item_id, decision=body.decision
        ),
    )
    return await proxy_tool(request, "decide_review_item", arguments)
