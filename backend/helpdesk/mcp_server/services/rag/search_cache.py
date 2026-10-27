"""The per-tenant cache of policy search pages, matched by meaning.

One Qdrant collection per tenant, `{prefix}cache_<slug>`, beside the policy
collection. Slugs hold no underscore, so the two names can never meet. Each
entry is the dense question vector with the fused page it found. The question
text is never stored, and neither is any answer.

A marker point holds a random generation. A search stamps its entry with the
generation it read before searching, and a lookup only matches the current
one. A drop writes a new generation before deleting, so a search that started
before a policy change can still save, but its page never hits.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from qdrant_client import AsyncQdrantClient, models

from helpdesk.mcp_server.services.rag.embedding import DENSE_DIMENSION
from helpdesk.mcp_server.services.rag.retrieval import SearchHit
from helpdesk.mcp_server.services.rag.vectors import _translated
from helpdesk.shared.core.naming import check_cache_collection_name, check_slug, collection_prefix

MIN_SIMILARITY = 0.95
"""The least cosine between two questions for one to answer the other."""

MAX_AGE = timedelta(hours=24)
"""An entry this old or older never hits."""

MIN_QUESTION_CHARS = 16
"""A question shorter than this, spaces trimmed, is neither looked up nor saved."""

MARKER_ID = uuid.UUID("0c7a1d3e-5b2f-4e6a-9d8c-1f2e3a4b5c6d")
"""The fixed id of the point that holds the current generation."""

_ENTRY = "entry"
_MARKER = "marker"
_MARKER_VECTOR = [1.0] + [0.0] * (DENSE_DIMENSION - 1)
"""Any valid vector: the marker is found by id, and lookups filter it out."""


def cache_collection_name(tenant_slug: str) -> str:
    """`{prefix}cache_<slug>`, or `InvalidPolicyNameError`. Builds no request."""
    check_slug(tenant_slug)
    return check_cache_collection_name(f"{collection_prefix()}cache_{tenant_slug}")


async def current_generation(client: AsyncQdrantClient, tenant_slug: str) -> str:
    """The tenant's current generation, making the collection and marker if missing."""
    name = cache_collection_name(tenant_slug)
    with _translated():
        if not await client.collection_exists(name):
            await client.create_collection(
                name,
                vectors_config=models.VectorParams(
                    size=DENSE_DIMENSION, distance=models.Distance.COSINE
                ),
            )
        found = await client.retrieve(name, ids=[str(MARKER_ID)], with_payload=True)
        if found and found[0].payload and "generation" in found[0].payload:
            return str(found[0].payload["generation"])
        return await _write_generation(client, name)


async def lookup(
    client: AsyncQdrantClient,
    tenant_slug: str,
    *,
    tenant_id: uuid.UUID,
    vector: Sequence[float],
    top_k: int,
    generation: str,
    now: datetime,
) -> list[SearchHit] | None:
    """The saved page of a near-identical question, or None for a miss."""
    name = cache_collection_name(tenant_slug)
    cutoff = (now - MAX_AGE).timestamp()
    with _translated():
        response = await client.query_points(
            name,
            query=list(vector),
            score_threshold=MIN_SIMILARITY,
            limit=1,
            query_filter=models.Filter(
                must=[
                    _match("kind", _ENTRY),
                    _match("tenant_id", str(tenant_id)),
                    _match("top_k", top_k),
                    _match("generation", generation),
                    models.FieldCondition(key="saved_at", range=models.Range(gt=cutoff)),
                ]
            ),
            with_payload=True,
        )
    if not response.points:
        return None
    payload = response.points[0].payload or {}
    return [_hit_from(raw) for raw in payload["hits"]]


async def save(
    client: AsyncQdrantClient,
    tenant_slug: str,
    *,
    tenant_id: uuid.UUID,
    vector: Sequence[float],
    top_k: int,
    generation: str,
    now: datetime,
    hits: Sequence[SearchHit],
) -> None:
    """Save one page under the generation read before its search."""
    name = cache_collection_name(tenant_slug)
    point = models.PointStruct(
        id=str(uuid.uuid4()),
        vector=list(vector),
        payload={
            "kind": _ENTRY,
            "tenant_id": str(tenant_id),
            "top_k": top_k,
            "generation": generation,
            "saved_at": now.timestamp(),
            "hits": [_hit_to(hit) for hit in hits],
        },
    )
    with _translated():
        await client.upsert(name, points=[point], wait=True)


async def drop(client: AsyncQdrantClient, tenant_slug: str) -> None:
    """Retire every entry: a new generation first, then delete them all.

    A tenant with no cache has nothing to drop, and none is made.
    """
    name = cache_collection_name(tenant_slug)
    with _translated():
        if not await client.collection_exists(name):
            return
        await _write_generation(client, name)
        await client.delete(
            name,
            points_selector=models.FilterSelector(
                filter=models.Filter(must=[_match("kind", _ENTRY)])
            ),
            wait=True,
        )


async def _write_generation(client: AsyncQdrantClient, name: str) -> str:
    generation = uuid.uuid4().hex
    await client.upsert(
        name,
        points=[
            models.PointStruct(
                id=str(MARKER_ID),
                vector=_MARKER_VECTOR,
                payload={"kind": _MARKER, "generation": generation},
            )
        ],
        wait=True,
    )
    return generation


def _match(key: str, value: str | int) -> models.FieldCondition:
    return models.FieldCondition(key=key, match=models.MatchValue(value=value))


def _hit_to(hit: SearchHit) -> dict[str, Any]:
    return {
        "chunk_id": str(hit.chunk_id),
        "document_id": str(hit.document_id),
        "document_slug": hit.document_slug,
        "version": hit.version,
        "heading_path": list(hit.heading_path),
        "text": hit.text,
        "score": hit.score,
        "dense_score": hit.dense_score,
    }


def _hit_from(raw: dict[str, Any]) -> SearchHit:
    dense_score = raw["dense_score"]
    return SearchHit(
        chunk_id=uuid.UUID(str(raw["chunk_id"])),
        document_id=uuid.UUID(str(raw["document_id"])),
        document_slug=str(raw["document_slug"]),
        version=str(raw["version"]),
        heading_path=tuple(str(part) for part in raw["heading_path"]),
        text=str(raw["text"]),
        score=float(raw["score"]),
        dense_score=None if dense_score is None else float(dense_score),
    )
