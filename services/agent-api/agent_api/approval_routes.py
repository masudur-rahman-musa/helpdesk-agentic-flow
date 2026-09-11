"""GET /me/approvals and the two decisions the queue can take.

The same three MCP tools the agent already calls, reached over HTTP instead of
over a turn (23a decision A, unchanged). Nothing here reads the database and
nothing here decides anything: the call crosses the MCP boundary, so RBAC, the
kill switch, the seat rules and the audit event apply to a decision the browser
makes exactly as they do to one the model makes. a project rule holds as written —
the mutation is a typed MCP tool behind RBAC and this module is transport.

**Identity comes from the token and from nowhere else**. The
caller's own bearer is forwarded verbatim, no route takes a user argument, and
the approver in the derived key below is read from the VERIFIED claims rather
than from anything the browser sent. Pinned by
`test_approval_routes.py::test_the_approve_route_answers_the_tools_output_under_the_callers_own_bearer`,
which asserts the arguments as an exact key set so an `approver_user_id` riding
along fails the case.

**The body is the tool's structured output, unchanged**, for the same reason
the `/me` reads are: a route that reshaped on the way out would be a second
contract for 23d's page to track.

**The comment and reason bounds are the TOOL's** (decision E). Re-declaring
them here would refuse at FastAPI's untyped 422 a value the tool accepts, and
the design's `VALIDATION` is what the page has a sentence for.

Routing reads as what it is: the queue is mine (`GET /me/approvals`), the
decision acts on somebody else's request (`POST /approvals/{request_id}/…`).

No lifespan resource is touched: these routes read nothing off `app.state`,
which is why their tests enter no lifespan and open no socket.
"""

import uuid

from fastapi import APIRouter, Header, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from agent_api.deps import authenticated, call_tool_route, unauthenticated
from agent_api.graph.keys import derive_decision_key

router = APIRouter()

APPROVE_VERB = "approve"
REJECT_VERB = "reject"
"""The two path segments, and the two verbs the decision key is taken over.

One constant per verb, used by the route and by the key alike, so the segment
a browser posts to and the material the key digests cannot drift apart. Pinned
as two DIFFERENT keys for one approver and one request by
`test_approval_routes.py::test_the_reject_route_answers_the_tools_output_under_the_callers_own_bearer`."""


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


@router.get("/me/approvals")
async def my_approvals(
    authorization: str | None = Header(default=None),
    limit: int | None = Query(default=None),
) -> JSONResponse:
    """Every pending request waiting on THIS caller, oldest first.

    The queue's whole filter is "waiting for me", and that "me" is the token's
    user: the tool declares no user argument and this route sends none. An
    absent `limit` sends no argument at all, so the tool's own default applies
    and a page cannot silently show a shorter queue than the caller has
    (`test_approval_routes.py::test_the_queue_route_sends_no_limit_when_none_is_asked_for`).
    """
    arguments = (
        ListPendingApprovalsArgs()
        if limit is None
        else ListPendingApprovalsArgsWithLimit(limit=limit)
    )
    return await call_tool_route(authorization, "list_pending_approvals", arguments)


@router.post("/approvals/{request_id}/approve")
async def approve(
    request_id: uuid.UUID,
    body: ApproveBody | None = None,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    """Record this caller's approval of one seat of one request's chain.

    `request_id` is a `uuid.UUID` path parameter, so a non-uuid path never
    reaches a tool, a key or a query — it would otherwise digest happily into
    a perfectly valid key filed against nothing
    (`::test_a_non_uuid_request_id_reaches_no_tool_and_no_key`).

    The claims are read here for the approver the key is derived from, which
    means the token is verified twice on this path: once here and once inside
    `call_tool_route`. Two copies of one guard, so neither can be killed on
    its own — the 401 cases pin the ANSWER, not this
    branch, and deleting either copy leaves the other answering exactly the
    same way.
    """
    verified = authenticated(authorization)
    if verified is None:
        return unauthenticated()
    _token, claims = verified

    arguments = ApproveRequestArgs(
        request_id=request_id,
        comment=None if body is None else body.comment,
        idempotency_key=derive_decision_key(
            approver_user_id=uuid.UUID(claims.sub), request_id=request_id, verb=APPROVE_VERB
        ),
    )
    return await call_tool_route(authorization, "approve_request", arguments)


@router.post("/approvals/{request_id}/reject")
async def reject(
    request_id: uuid.UUID,
    body: RejectBody,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    """Refuse one request, with the reason the audit record keeps.

    The key is derived over `REJECT_VERB`, so an Approve and a Reject by one
    approver on one request are two different keys: reusing the approve key
    would replay an approval's stored response for a rejection, which is the
    worst failure this derivation exists to prevent.

    The same two copies of the token guard as `approve` above, for the same
    reason and with the same consequence for a project rule.
    """
    verified = authenticated(authorization)
    if verified is None:
        return unauthenticated()
    _token, claims = verified

    arguments = RejectRequestArgs(
        request_id=request_id,
        reason=body.reason,
        idempotency_key=derive_decision_key(
            approver_user_id=uuid.UUID(claims.sub), request_id=request_id, verb=REJECT_VERB
        ),
    )
    return await call_tool_route(authorization, "reject_request", arguments)
