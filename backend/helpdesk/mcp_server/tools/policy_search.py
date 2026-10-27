"""The `search_policy` tool: hybrid search over the caller's own policy corpus.

The search, ranking and refusal rules live in the retrieval service; this
module turns the token into a collection name and shapes the answer.

The collection comes from the token. One collection per tenant is the whole
isolation, with no second guard behind it, so no argument may name a tenant.

The tool decides the refusal, not its caller. A refused page carries no
citations, so nothing downstream can answer from chunks the threshold
rejected. Titles are looked up only when the page is not refused.

The search cache sits in front of the search. It holds fused pages
only, so a hit still runs the refusal and the title lookup. Any cache failure
is a miss and is logged without the question; the search answers either way.
"""

import logging
import uuid
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from helpdesk.mcp_server.services.rag import retrieval, search_cache
from helpdesk.mcp_server.services.rag.retrieval import SearchHit
from helpdesk.mcp_server.tools.context import ToolContext
from helpdesk.shared.core.errors import ErrorCode, ToolError
from helpdesk.shared.core.naming import check_query, check_top_k
from helpdesk.shared.core.observability import metrics
from helpdesk.shared.crud.tenants import find_tenant_by_id

logger = logging.getLogger(__name__)

CacheResult = Literal["hit", "miss"]

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
    """The page, whether the tool refused to answer from it, and whether the
    page came from the search cache.

    `citations` is always empty when `refused` is true.
    """

    citations: list[PolicyCitation]
    refused: bool
    cache: CacheResult


async def search_policy(ctx: ToolContext, args: SearchPolicyInput) -> SearchPolicyOutput:
    """Search this tenant's policy corpus; refuse when nothing matches well.

    Order: the cache, else the search; then judge, then look up titles only for
    a page worth answering from. Counted once as a cache hit or miss.
    """
    tenant = await find_tenant_by_id(ctx.session, ctx.identity.tenant_id)
    if tenant is None:
        raise UnknownTenantError("the token's tenant no longer exists")

    hits, cache = await _cached_or_searched(ctx, args, tenant.slug)
    metrics.record_policy_cache(tenant=str(ctx.identity.tenant_id), result=cache)
    if retrieval.should_refuse(hits, floor=ctx.settings.policy_refusal_floor):
        return SearchPolicyOutput(citations=[], refused=True, cache=cache)

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
        cache=cache,
    )


async def _cached_or_searched(
    ctx: ToolContext, args: SearchPolicyInput, tenant_slug: str
) -> tuple[list[SearchHit], CacheResult]:
    """The saved page of a near-identical question, or a fresh search, saved.

    The bounds are checked first, so a refused argument reaches neither the
    cache nor the store. Short questions and a switched-off cache skip it.
    """
    check_top_k(args.top_k)
    check_query(args.query)
    client = ctx.clients.qdrant()
    embedder = ctx.clients.embedder()
    use_cache = (
        ctx.settings.policy_search_cache
        and len(args.query.strip()) >= search_cache.MIN_QUESTION_CHARS
    )
    if not use_cache:
        hits = await retrieval.search_hits(
            client, tenant_slug=tenant_slug, query=args.query, embedder=embedder, top_k=args.top_k
        )
        return hits, "miss"

    vector = embedder.embed_dense([args.query])[0]
    generation: str | None = None
    try:
        generation = await search_cache.current_generation(client, tenant_slug)
        found = await search_cache.lookup(
            client,
            tenant_slug,
            tenant_id=ctx.identity.tenant_id,
            vector=vector,
            top_k=args.top_k,
            generation=generation,
            now=ctx.now,
        )
    except Exception:
        logger.warning("policy search cache lookup failed, searching instead", exc_info=True)
        found = None
    if found is not None:
        return found, "hit"

    hits = await retrieval.search_hits(
        client,
        tenant_slug=tenant_slug,
        query=args.query,
        embedder=embedder,
        top_k=args.top_k,
        dense_vector=vector,
    )
    if generation is not None:
        await _save(
            ctx, tenant_slug, vector=vector, top_k=args.top_k, generation=generation, hits=hits
        )
    return hits, "miss"


async def _save(
    ctx: ToolContext,
    tenant_slug: str,
    *,
    vector: Sequence[float],
    top_k: int,
    generation: str,
    hits: Sequence[SearchHit],
) -> None:
    """Save a fresh page; a failure is logged and never fails the search.

    Only under the generation read BEFORE the search: one read after it could
    stamp an old page with a newer generation.
    """
    try:
        await search_cache.save(
            ctx.clients.qdrant(),
            tenant_slug,
            tenant_id=ctx.identity.tenant_id,
            vector=vector,
            top_k=top_k,
            generation=generation,
            now=ctx.now,
            hits=hits,
        )
    except Exception:
        logger.warning("policy search cache write failed", exc_info=True)
