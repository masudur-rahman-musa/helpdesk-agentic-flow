"""/admin/policies: this tenant's policy corpus, read (24g) and re-ingested (31a).

`list_policy_documents` is this plan's read and nothing in the browser could
reach it. This module is the wire and nothing else: the call crosses the MCP
boundary, so RBAC, the kill switch and the audit event apply to an admin
reading the corpus from a page exactly as they do to one reading it in a turn
(23a decision A, unchanged).

**Identity comes from the token and from nowhere else**. The
caller's own bearer is forwarded verbatim by `deps.call_tool_route` on both
routes. The GET takes no query parameter, no body and no argument of any kind,
and the POST takes exactly the three keys naming ONE document -- no tenant and
no path -- so there is nothing on either for a caller to trust: the tenant
whose corpus is read and rewritten is the token's, decided behind the MCP
boundary. Pinned by
`test_policies_routes.py::test_the_board_route_sends_an_exactly_empty_argument_set`,
which asserts the arguments as the exact dict, so a field riding along fails
the case too.

**A new module rather than a route in `rules_routes`** (decision F, 24eb
decision B's reason unchanged): this is a different resource on a different
page, and a module named for the rules carrying the corpus is a name that
stops being true.

**The body is the tool's structured output, unchanged**, for the same reason
`/admin/rules` and `/admin/audit/search` are: a route that reshaped on the way
out would be a second contract for 24h's page to track, and the grouping by
slug, the title rule and both orders are already what
`helpdesk.mcp_server.services.rag.documents.read_policy_documents` fixed. The rows cross in the
tool's own sequence, unfiltered and unreordered, which
`::test_the_rows_cross_in_the_tools_own_order` asserts as the exact list of
slugs.

No new row in `deps.STATUS_FOR_CODE`: its five are 24cb's and the GET answers
with `FORBIDDEN` as 403 and `TOOL_DISABLED` as 503 (pinned by
`::test_a_tool_refusal_answers_its_own_typed_code`), while `VALIDATION`,
`NOT_FOUND` and `POLICY_BLOCKED` are unreachable from a route with no input at
all.

**The POST is an earlier change**, the write half of the same page: one document, named
by its slug and version, re-read from the corpus and made live. It is the one
thing here a project rule does bite on, and it obeys it the way every mutation in
this service does -- the effect is the typed MCP tool behind RBAC, and this
module forwards three keys and the caller's own bearer to it. It mints no
idempotency key: the browser's is forwarded, so a retry is a replay rather
than a second ingest.

That route carries its own path, `REINGEST_PATH`, rather than a second verb on
`POLICIES_PATH` (31a decision I): the two paths differ upstream, and 24eb
decision C's rule is that the browser's path equals this service's. Its four
reachable refusals are `VALIDATION`, `FORBIDDEN`, `NOT_FOUND` and
`TOOL_DISABLED`, four of the shared map's codes, so `deps.STATUS_FOR_CODE`
gains no row for it either.

No lifespan resource is touched: neither route reads anything off `app.state`,
which is why their tests enter no lifespan and open no socket.
"""

from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Request
from pydantic import ValidationError

from helpdesk.agent_api.core.deps import InvalidBodyError
from helpdesk.agent_api.schemas.policies import (
    ListPolicyDocumentsArgs as ListPolicyDocumentsArgs,
)
from helpdesk.agent_api.schemas.policies import (
    ReingestPolicyDocumentArgs as ReingestPolicyDocumentArgs,
)
from helpdesk.agent_api.services.tool_proxy import proxy_tool

router = APIRouter()

POLICIES_PATH = "/admin/policies"
"""SPEC section 14's own route name, identical upstream and browser-side (24eb
decision C): one path is one thing to keep in step."""

LIST_POLICY_DOCUMENTS_TOOL = "list_policy_documents"

REINGEST_PATH = "/admin/policies/reingest"
"""The page's own path with the action appended, identical upstream and
browser-side (24eb decision C, 31a decision I): one path is one thing to keep
in step, and a second verb on `POLICIES_PATH` would put a read and a write of
different resources behind one name."""

REINGEST_POLICY_DOCUMENT_TOOL = "reingest_policy_document"


@router.get(POLICIES_PATH)
async def policy_documents_board(request: Request) -> Mapping[str, Any]:
    """Every policy document this tenant has, with each ingested version.

    Which documents the corpus holds, which versions exist of each, which one
    is live and whether a version landed with no chunk at all: written since
    an earlier change and readable nowhere until this plan's tool. A version with zero
    chunks and a document with no current version are both carried rather than
    hidden (24g criteria 6 and 7), because a half-finished ingest is exactly
    what this page exists to show.

    Nothing is decided here. Whether this caller may read the corpus at all is
    SPEC section 7's matrix, behind the MCP boundary, and an empty corpus is a
    200 carrying an empty list -- never a 404
    (`::test_a_tenant_with_no_corpus_is_a_200_and_an_empty_list`).
    """
    return await proxy_tool(request, LIST_POLICY_DOCUMENTS_TOOL, ListPolicyDocumentsArgs())


@router.post(REINGEST_PATH)
async def reingest_policy_document_route(request: Request) -> Mapping[str, Any]:
    """Re-read one corpus document and make it live.

    The write half of the board above, and the only thing standing between an
    edited policy file and the corpus every access decision cites. Nothing is
    decided here: whether this caller may re-ingest at all is SPEC section 7's
    matrix behind the MCP boundary, a corpus file that is not on the host is
    the tool's own `NOT_FOUND`, and an unchanged source is a 200 carrying
    `created=false` rather than any kind of refusal.

    The caller is verified by `RequireAuthMiddleware` before the route
    runs, so an unauthenticated caller reaches neither the body read nor a
    session (`test_auth_middleware.py`'s no-bearer case, whose caller is
    scripted with nothing at all).

    The body is read and validated here rather than declared as a FastAPI
    parameter, exactly as `killswitch_routes.set_kill_switch_route` reads its
    own: a declared model answers a malformed body with FastAPI's untyped 422,
    and every other refusal an admin can provoke on this route is one of SPEC
    section 8.1's typed codes. An unparseable body is the same `VALIDATION` as
    a body missing a key, because neither carries a slug.

    The tool's structured output crosses unchanged, for the same reason the
    board's rows do: a route that reshaped would be a second contract for row
    31b's control to track.
    """
    try:
        arguments = ReingestPolicyDocumentArgs.model_validate(await request.json())
    except (ValueError, ValidationError):
        raise InvalidBodyError("the re-ingest body did not validate") from None

    return await proxy_tool(request, REINGEST_POLICY_DOCUMENT_TOOL, arguments)
