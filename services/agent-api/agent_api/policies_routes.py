"""GET /admin/policies: this tenant's policy corpus, as documents.

`list_policy_documents` is this plan's read and nothing in the browser could
reach it. This module is the wire and nothing else: the call crosses the MCP
boundary, so RBAC, the kill switch and the audit event apply to an admin
reading the corpus from a page exactly as they do to one reading it in a turn
(23a decision A, unchanged). Everything here is a READ, so a project rule has
nothing to bite on.

**Identity comes from the token and from nowhere else**. The
caller's own bearer is forwarded verbatim by `deps.call_tool_route`, and this
route takes no query parameter, no body and no argument of any kind, so there
is nothing here for a caller to trust: the tenant whose corpus is read is the
token's, decided behind the MCP boundary. Pinned by
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
`helpdesk.rag.documents.read_policy_documents` fixed. The rows cross in the
tool's own sequence, unfiltered and unreordered, which
`::test_the_rows_cross_in_the_tools_own_order` asserts as the exact list of
slugs.

No new row in `deps.STATUS_FOR_CODE`: its five are 24cb's and this route
answers with `FORBIDDEN` as 403 and `TOOL_DISABLED` as 503 (pinned by
`::test_a_tool_refusal_answers_its_own_typed_code`), while `VALIDATION`,
`NOT_FOUND` and `POLICY_BLOCKED` are unreachable from a route with no input at
all.

No lifespan resource is touched: this route reads nothing off `app.state`,
which is why its tests enter no lifespan and open no socket.
"""

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from agent_api.deps import call_tool_route

router = APIRouter()

POLICIES_PATH = "/admin/policies"
"""SPEC section 14's own route name, identical upstream and browser-side (24eb
decision C): one path is one thing to keep in step."""

LIST_POLICY_DOCUMENTS_TOOL = "list_policy_documents"


class ListPolicyDocumentsArgs(BaseModel):
    """No arguments at all: the corpus listed is the token tenant's own.

    The zero-argument form `rules_routes.ListSodRulesArgs` and the kill-switch
    board already use. `ListPolicyDocumentsInput` declares no field and forbids
    extras, so a key riding along from here -- a tenant, a slug, a version --
    would answer `VALIDATION` on every load of the board rather than filtering
    anything.
    """


@router.get(POLICIES_PATH)
async def policy_documents_board(authorization: str | None = Header(default=None)) -> JSONResponse:
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
    return await call_tool_route(
        authorization, LIST_POLICY_DOCUMENTS_TOOL, ListPolicyDocumentsArgs()
    )
