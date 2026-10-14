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
`helpdesk.rag.documents.read_policy_documents` fixed. The rows cross in the
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
`TOOL_DISABLED`, four of the same five, so `deps.STATUS_FOR_CODE` gains no row
for it either.

No lifespan resource is touched: neither route reads anything off `app.state`,
which is why their tests enter no lifespan and open no socket.
"""

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from helpdesk.agent_api.deps import authenticated, call_tool_route, typed_refusal, unauthenticated
from helpdesk.shared.errors import ErrorCode

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


class ListPolicyDocumentsArgs(BaseModel):
    """No arguments at all: the corpus listed is the token tenant's own.

    The zero-argument form `rules_routes.ListSodRulesArgs` and the kill-switch
    board already use. `ListPolicyDocumentsInput` declares no field and forbids
    extras, so a key riding along from here -- a tenant, a slug, a version --
    would answer `VALIDATION` on every load of the board rather than filtering
    anything.
    """


class ReingestPolicyDocumentArgs(BaseModel):
    """Exactly the three keys `ReingestPolicyDocumentInput` declares.

    `extra="forbid"` here as well as there, so a `tenant_id` the browser sent
    is refused on this side of the boundary rather than crossing it to be
    refused on the other: the tenant whose corpus is rewritten is the token's
    and is decided behind the MCP boundary. Neither name carries
    its bound here -- `check_slug` and `check_version` run in the pipeline
    before the corpus path is composed, so the caps have one home and one
    error.

    `idempotency_key` is forwarded, never minted here: a key this route
    invented would turn every retry of a re-ingest into a second ingest
    instead of a replay. Pinned as the exact argument dict by
    `test_policies_routes.py::test_the_reingest_route_sends_its_exact_three_key_argument_set`.
    """

    model_config = ConfigDict(extra="forbid")

    slug: str = Field(min_length=1)
    version: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)


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


@router.post(REINGEST_PATH)
async def reingest_policy_document_route(
    request: Request, authorization: str | None = Header(default=None)
) -> JSONResponse:
    """Re-read one corpus document and make it live.

    The write half of the board above, and the only thing standing between an
    edited policy file and the corpus every access decision cites. Nothing is
    decided here: whether this caller may re-ingest at all is SPEC section 7's
    matrix behind the MCP boundary, a corpus file that is not on the host is
    the tool's own `NOT_FOUND`, and an unchanged source is a 200 carrying
    `created=false` rather than any kind of refusal.

    The bearer is verified BEFORE the body is read and before any session is
    opened, so an unauthenticated caller reaches neither
    (`::test_an_absent_or_malformed_bearer_on_the_reingest_route_is_a_typed_401`,
    whose caller is scripted with nothing at all). It verifies twice on this
    path, here and inside `call_tool_route`, which is what a project rule
    wants of a guard this sharp: neither copy can be deleted on its own.

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
    verified = authenticated(authorization)
    if verified is None:
        return unauthenticated()

    try:
        arguments = ReingestPolicyDocumentArgs.model_validate(await request.json())
    except (ValueError, ValidationError):
        return typed_refusal(ErrorCode.VALIDATION)

    return await call_tool_route(authorization, REINGEST_POLICY_DOCUMENT_TOOL, arguments)
