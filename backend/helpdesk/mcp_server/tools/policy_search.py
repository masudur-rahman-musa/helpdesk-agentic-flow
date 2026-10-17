"""The `search_policy` tool: hybrid search over the caller's own policy corpus.

The search, ranking and refusal rules live in the retrieval service; this
module turns the token into a collection name and shapes the answer.

The collection comes from the token. One collection per tenant is the whole
isolation, with no second guard behind it, so no argument may name a tenant.

The tool decides the refusal, not its caller. A refused page carries no
citations, so nothing downstream can answer from chunks the threshold
rejected. Titles are looked up only when the page is not refused.
"""

import uuid

from pydantic import BaseModel, ConfigDict, Field

from helpdesk.mcp_server.services.rag import retrieval
from helpdesk.mcp_server.tools.context import ToolContext
from helpdesk.shared.core.errors import ErrorCode, ToolError
from helpdesk.shared.crud.tenants import find_tenant_by_id

_IGNORE_EXTRAS = ConfigDict(extra="ignore")
"""Extra arguments are dropped, never refused: the tenant comes from the token."""


class UnknownTenantError(ToolError):
    """The token's tenant id has no row.

    `INTERNAL`, not a denial: the token verified, so a missing tenant means the
    system's own data is broken, and a denial would hide that.
    """

    code = ErrorCode.INTERNAL


class SearchPolicyInput(BaseModel):
    """A question and a page size. The tenant is not an argument.

    The bounds are checked in the retrieval service before anything runs, so
    there is one cap and one error.
    """

    model_config = _IGNORE_EXTRAS

    query: str = Field(description="The policy question, in the user's own words.")
    top_k: int = Field(
        default=retrieval.TOP_K_DEFAULT,
        description="How many policy chunks to return, at most.",
    )


class PolicyCitation(BaseModel):
    """One cited chunk, with nothing internal.

    `document` is the document slug. The score the refusal reads is not here, so
    it never reaches a prompt.
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

    `citations` is always empty when `refused` is true.
    """

    citations: list[PolicyCitation]
    refused: bool


async def search_policy(ctx: ToolContext, args: SearchPolicyInput) -> SearchPolicyOutput:
    """Search this tenant's policy corpus; refuse when nothing matches well.

    Order: search, judge, then look up titles only for a page worth answering
    from.
    """
    tenant = await find_tenant_by_id(ctx.session, ctx.identity.tenant_id)
    if tenant is None:
        raise UnknownTenantError("the token's tenant no longer exists")

    hits = await retrieval.search_hits(
        ctx.clients.qdrant(),
        tenant_slug=tenant.slug,
        query=args.query,
        embedder=ctx.clients.embedder(),
        top_k=args.top_k,
    )
    if retrieval.should_refuse(hits):
        return SearchPolicyOutput(citations=[], refused=True)

    citations = await retrieval.resolve_citations(ctx.session, hits)
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
