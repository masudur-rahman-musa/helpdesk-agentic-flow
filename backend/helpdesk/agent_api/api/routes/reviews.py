"""GET /me/reviews, one review's items, and the decision on one of them.

These call the same MCP tools the agent uses, so access checks, the kill
switch and audit apply to a browser keep or revoke too. The caller's token is
forwarded and the reviewer comes from the verified claims. Status, decision
words and page size are checked by the tool alone, so errors stay typed.
The decision lives under `/review-items/` so a fixed path segment never sits
beside the web app's dynamic `[reviewId]` folder.
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
    """Access reviews waiting on this caller, newest period first."""
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
    """One review's rows. The tool decides who may read it."""
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

    The decision word is part of the key, so changing one's mind meets the tool's
    refusal instead of a replay of the first answer.
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
