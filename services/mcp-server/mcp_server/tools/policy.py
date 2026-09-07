"""The `search_policy` tool: hybrid retrieval over the caller's own corpus.

A read, and the only tool that reaches Qdrant. Everything it does with the
corpus is 15a's — the two arms, the RRF fusion, the refusal predicate and the
citation rendering — so this module is a boundary and not a second copy of any
of that: it turns a token into a collection name, calls the three functions in
the one order the plan fixes, and renders the result as the wire shape.

**The collection comes from the TOKEN** (a project rule, an earlier change decision A).
`identity.tenant_id` is read back through `find_tenant_by_id` to get the slug
`vectors.collection_name` needs; no argument names a tenant or a collection,
and `SearchPolicyInput` ignores extras, so a `tenant_id` argument is dropped
rather than honoured and rather than rejected. This matters more here than for
the self-scoped reads: one collection per tenant IS the isolation (SPEC
§12.4), so there is no second guard behind a wrong collection name. Pinned at
the argument by
`test_mcp_search_policy.py::test_the_collection_searched_is_the_tokens_and_an_extra_tenant_id_is_dropped`
and end to end, against the real store, by
`test_mcp_policy_isolation.py::test_tenant_bs_marker_is_returned_to_tenant_b_and_never_to_tenant_a`.

**The tool decides the refusal, not its caller** (decision C). `should_refuse`
has one home (15a decision D), and a refused page carries NO citations, so row
17 cannot answer from chunks the threshold rejected — the exact failure
requirement #14 exists to prevent. It is also why the two halves of 15a are
called separately: a refusing turn never pays the title query, pinned by
`::test_a_refused_page_never_pays_the_title_query`, whose points name a
document row that is not there so an implementation resolving first could only
fail.

**`document` carries the slug** (decision B). the design's six citation fields
are a strict subset of the seven here; the seventh is `title`, which exists
because a citation the UI cannot render is a wrong answer (15a decision E).
The internal dense cosine `should_refuse` reads is NOT on this model and never
reaches a prompt, pinned as an exact field set by
`::test_a_page_carries_exactly_the_seven_citation_fields_in_fused_order`.

No session and no client is opened here: the pipeline binds the session to the
token's tenant, and `rag_clients` owns the two process-level clients.
"""

import uuid

from helpdesk.core.repository import find_tenant_by_id
from helpdesk.core.settings import load_settings
from helpdesk.errors import ErrorCode, ToolError
from helpdesk.rag import retrieval
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_server import rag_clients
from mcp_server.identity import Identity

_IGNORE_EXTRAS = ConfigDict(extra="ignore")
"""Extra arguments are dropped, never refused.

The tenant arrives on the token, so an argument naming one is data to discard,
not a reason to fail the call — the same rule the self-scoped reads follow
(`tools/read.py`)."""


class UnknownTenantError(ToolError):
    """The token's tenant id has no row.

    `INTERNAL`, not `FORBIDDEN` or `NOT_FOUND`: the token verified, so nothing
    about the caller is wrong and nothing they asked for is missing — a signed
    `tenant_id` with no tenant behind it means this repo's own invariant is
    broken. Reading it as a permission decision would hide that behind a
    legitimate-looking denial, the same reasoning as `registry.lookup`'s.

    No test reaches this branch and none can without deleting a tenant row
    out from under a live token: the pipeline binds the session to the same
    id, so a token whose tenant is gone fails before the handler runs. It is
    a guard against `find_tenant_by_id` returning None, not a behaviour with
    a kill.
    """

    code = ErrorCode.INTERNAL


class SearchPolicyInput(BaseModel):
    """A question and a page size. The tenant is NOT an argument.

    `top_k`'s default is the corpus default 15a fixes, imported rather than
    written out so the number a client sees in the advertised schema cannot
    drift from the one the library uses; read off that schema by
    `test_mcp_search_policy.py::test_search_policy_is_advertised_with_a_default_page_of_six`.

    Neither field declares its bound here: `retrieval.search_hits` runs
    `check_query` and `check_top_k` before any effect, so the cap has one home
    and one error, and a refused argument reaches neither the embedder nor the
    store (`::test_a_refused_argument_reaches_neither_the_embedder_nor_the_store`).
    """

    model_config = _IGNORE_EXTRAS

    query: str = Field(description="The policy question, in the user's own words.")
    top_k: int = Field(
        default=retrieval.TOP_K_DEFAULT,
        description="How many policy chunks to return, at most.",
    )


class PolicyCitation(BaseModel):
    """One cited chunk: exactly the seven fields, and nothing internal.

    `document` is the document SLUG, which is what the design names; 15a's
    dataclass calls the same value `document_slug`, so this model is the
    rename and not a passthrough.
    """

    document: str
    title: str
    version: str
    heading_path: list[str]
    chunk_id: uuid.UUID
    text: str
    score: float


class SearchPolicyOutput(BaseModel):
    """The page, and whether the tool refused to answer from it.

    `citations` is empty whenever `refused` is true. The two are not
    independent: a caller must never be handed chunks the threshold rejected.
    """

    citations: list[PolicyCitation]
    refused: bool


async def search_policy(
    session: AsyncSession, identity: Identity, args: SearchPolicyInput
) -> SearchPolicyOutput:
    """Search this tenant's policy corpus; refuse when nothing matches well.

    The order is fixed and is what the acceptance criteria assert: retrieve
    and fuse (`search_hits`), judge (`should_refuse`), and resolve titles
    (`resolve_citations`) ONLY when the page is worth answering from.
    """
    tenant = await find_tenant_by_id(session, identity.tenant_id)
    if tenant is None:
        raise UnknownTenantError("the token's tenant no longer exists")

    hits = await retrieval.search_hits(
        rag_clients.qdrant(load_settings()),
        tenant_slug=tenant.slug,
        query=args.query,
        embedder=rag_clients.embedder(),
        top_k=args.top_k,
    )
    if retrieval.should_refuse(hits):
        return SearchPolicyOutput(citations=[], refused=True)

    citations = await retrieval.resolve_citations(session, hits)
    return SearchPolicyOutput(
        citations=[
            PolicyCitation(
                document=citation.document_slug,
                title=citation.title,
                version=citation.version,
                heading_path=list(citation.heading_path),
                chunk_id=citation.chunk_id,
                text=citation.text,
                score=citation.score,
            )
            for citation in citations
        ],
        refused=False,
    )
