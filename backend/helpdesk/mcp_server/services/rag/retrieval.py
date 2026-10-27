"""Hybrid retrieval over one tenant's collection: dense and sparse arms,
merged by Reciprocal Rank Fusion, current versions only.

The refusal reads the best dense cosine, not the fused score: fused
scores top out near 0.033, and a rank says nothing about closeness.
"""

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from qdrant_client import AsyncQdrantClient, models
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.mcp_server.services.rag.embedding import Embedder
from helpdesk.mcp_server.services.rag.vectors import (
    DENSE_VECTOR_NAME,
    SPARSE_VECTOR_NAME,
    _translated,
    collection_name,
)
from helpdesk.shared.core.errors import ErrorCode, ToolError
from helpdesk.shared.core.naming import check_query, check_top_k
from helpdesk.shared.crud.rag import fetch_document_titles

RRF_K = 60
"""The k of Reciprocal Rank Fusion, `1 / (k + rank)`, ranks counted from 1."""

TOP_K_DEFAULT = 6
"""The default page size: how many chunks an answer is built from."""

CANDIDATES_PER_ARM = 30
"""How many candidates each arm fetches before fusion. Only `top_k` per
arm would let one arm's leader never reach the page."""

REFUSAL_THRESHOLD = 0.35
"""The floor on the page's best dense cosine. Exactly 0.35 answers."""


class CitationUnresolvableError(ToolError):
    """A retrieved chunk names a document row that is not there. The stores
    disagree, so `INTERNAL`, not a blank title."""

    code = ErrorCode.INTERNAL


@dataclass(frozen=True)
class SearchHit:
    """One retrieved chunk, as an arm returned it or as fusion left it.

    `dense_score` is the dense cosine, kept through fusion for the refusal,
    and `None` when only the sparse arm found it.
    """

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_slug: str
    version: str
    heading_path: tuple[str, ...]
    text: str
    score: float
    dense_score: float | None


@dataclass(frozen=True)
class Citation:
    """What an answer cites. No internal score, so it never reaches a prompt."""

    document_slug: str
    title: str
    version: str
    heading_path: tuple[str, ...]
    chunk_id: uuid.UUID
    text: str
    score: float


def fuse(
    dense: Sequence[SearchHit],
    sparse: Sequence[SearchHit],
    *,
    top_k: int = TOP_K_DEFAULT,
) -> list[SearchHit]:
    """The two arms' orders, fused by RRF and cut to `top_k`.

    A chunk in both arms appears once. An empty arm adds nothing.
    """
    scores: dict[uuid.UUID, float] = {}
    hits: dict[uuid.UUID, SearchHit] = {}
    cosines: dict[uuid.UUID, float | None] = {}

    for arm in (dense, sparse):
        for rank, hit in enumerate(arm, start=1):
            scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + 1.0 / (RRF_K + rank)
            hits.setdefault(hit.chunk_id, hit)
            # Dense comes first, so the sparse `None` never overwrites a cosine.
            if cosines.get(hit.chunk_id) is None:
                cosines[hit.chunk_id] = hit.dense_score

    ordered = sorted(hits, key=lambda chunk_id: scores[chunk_id], reverse=True)
    return [
        _replace_scores(hits[chunk_id], score=scores[chunk_id], dense_score=cosines[chunk_id])
        for chunk_id in ordered[:top_k]
    ]


def should_refuse(hits: Sequence[SearchHit], *, floor: float = REFUSAL_THRESHOLD) -> bool:
    """True when nothing on this page is a dense match worth answering from.
    An empty page refuses."""
    cosines = [hit.dense_score for hit in hits if hit.dense_score is not None]
    if not cosines:
        return True
    return max(cosines) < floor


async def search_hits(
    client: AsyncQdrantClient,
    *,
    tenant_slug: str,
    query: str,
    embedder: Embedder,
    top_k: int = TOP_K_DEFAULT,
    dense_vector: Sequence[float] | None = None,
) -> list[SearchHit]:
    """Both arms against this tenant's collection, fused and cut to `top_k`.
    Reads no titles, so a caller that refuses pays nothing extra. A caller that
    already embedded the question passes `dense_vector`, so it is not made twice."""
    check_top_k(top_k)
    check_query(query)
    name = collection_name(tenant_slug)

    if dense_vector is None:
        dense_vector = embedder.embed_dense([query])[0]
    sparse_vector = embedder.embed_sparse([query])[0]

    with _translated():
        dense_response = await client.query_points(
            name,
            query=list(dense_vector),
            using=DENSE_VECTOR_NAME,
            limit=CANDIDATES_PER_ARM,
            query_filter=_current_only(),
            with_payload=True,
        )
        sparse_response = await client.query_points(
            name,
            query=models.SparseVector(
                indices=list(sparse_vector.indices), values=list(sparse_vector.values)
            ),
            using=SPARSE_VECTOR_NAME,
            limit=CANDIDATES_PER_ARM,
            query_filter=_current_only(),
            with_payload=True,
        )

    return fuse(
        _hits(dense_response.points, dense=True),
        _hits(sparse_response.points, dense=False),
        top_k=top_k,
    )


async def resolve_citations(session: AsyncSession, hits: Sequence[SearchHit]) -> list[Citation]:
    """Render a fused page as citations, titles read in one query, order kept."""
    titles = await fetch_document_titles(session, {hit.document_id for hit in hits})
    citations = []
    for hit in hits:
        title = titles.get(hit.document_id)
        if title is None:
            raise CitationUnresolvableError(
                f"the document of chunk {hit.chunk_id} is no longer registered"
            )
        citations.append(
            Citation(
                document_slug=hit.document_slug,
                title=title,
                version=hit.version,
                heading_path=hit.heading_path,
                chunk_id=hit.chunk_id,
                text=hit.text,
                score=hit.score,
            )
        )
    return citations


async def search_policy_chunks(
    session: AsyncSession,
    client: AsyncQdrantClient,
    *,
    tenant_slug: str,
    query: str,
    embedder: Embedder,
    top_k: int = TOP_K_DEFAULT,
) -> list[Citation]:
    """The whole search: retrieve, fuse, resolve titles. At most `top_k` hits."""
    hits = await search_hits(
        client, tenant_slug=tenant_slug, query=query, embedder=embedder, top_k=top_k
    )
    return await resolve_citations(session, hits)


def _current_only() -> models.Filter:
    """The filter both arms carry: only the current version of a document."""
    return models.Filter(
        must=[models.FieldCondition(key="is_current", match=models.MatchValue(value=True))]
    )


def _hits(points: Iterable[Any], *, dense: bool) -> list[SearchHit]:
    """One arm's response as `SearchHit`s, in the store's order."""
    hits = []
    for point in points:
        payload = point.payload or {}
        score = float(point.score)
        hits.append(
            SearchHit(
                chunk_id=uuid.UUID(str(point.id)),
                document_id=uuid.UUID(str(payload["document_id"])),
                document_slug=str(payload["document_slug"]),
                version=str(payload["version"]),
                heading_path=tuple(payload["heading_path"]),
                text=str(payload["text"]),
                score=score,
                dense_score=score if dense else None,
            )
        )
    return hits


def _replace_scores(hit: SearchHit, *, score: float, dense_score: float | None) -> SearchHit:
    """The same chunk with its fused score and its carried cosine."""
    return SearchHit(
        chunk_id=hit.chunk_id,
        document_id=hit.document_id,
        document_slug=hit.document_slug,
        version=hit.version,
        heading_path=hit.heading_path,
        text=hit.text,
        score=score,
        dense_score=dense_score,
    )
