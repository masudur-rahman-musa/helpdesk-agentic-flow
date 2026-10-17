"""The per-tenant Qdrant collections. The only module that imports
`qdrant_client`.

One collection per tenant, so isolation does not hang on a filter.
Points are never deleted; old versions are marked not current. A rollback
after an upsert can leave orphan points; retrieval never reaches them and
the next ingest overwrites them.
"""

import uuid
from collections.abc import Sequence
from typing import Literal

from qdrant_client import AsyncQdrantClient, models
from qdrant_client.http.exceptions import ApiException, ResponseHandlingException

from helpdesk.mcp_server.services.rag.chunking import Chunk
from helpdesk.mcp_server.services.rag.embedding import DENSE_DIMENSION
from helpdesk.shared.core.config import Settings
from helpdesk.shared.core.errors import ErrorCode, ToolError
from helpdesk.shared.core.naming import (
    COLLECTION_NAME_PREFIX,
    check_collection_name,
    check_slug,
)

DENSE_VECTOR_NAME = "dense"
"""The named dense vector of every policy collection."""

SPARSE_VECTOR_NAME = "sparse"
"""The named sparse (bm25) vector, for keyword search."""

POINT_ID_NAMESPACE = uuid.UUID("6f5c0d2e-1a4b-5c8d-9e0f-7a1b2c3d4e5f")
"""Fixed uuid5 namespace for point ids. Never change it."""


class VectorStoreUnavailableError(ToolError):
    """The vector store could not be reached or refused the request. The
    message never names the store or its URL."""

    code = ErrorCode.INTERNAL


def collection_name(tenant_slug: str) -> str:
    """`policy_{tenant_slug}`, or `InvalidPolicyNameError`. Builds no request."""
    check_slug(tenant_slug)
    return check_collection_name(f"{COLLECTION_NAME_PREFIX}{tenant_slug}")


def create_client(settings: Settings) -> AsyncQdrantClient:
    """The one place a Qdrant client is built, timeout included."""
    # The client types `timeout` as int but takes a float, which allows
    # sub-second values.
    return AsyncQdrantClient(
        url=settings.qdrant_url,
        timeout=settings.qdrant_timeout_seconds,  # type: ignore[arg-type]
    )


def point_id(tenant_id: uuid.UUID, document_id: uuid.UUID, chunk_index: int) -> uuid.UUID:
    """The fixed id of one chunk's point, so a re-upsert overwrites."""
    return uuid.uuid5(POINT_ID_NAMESPACE, f"{tenant_id}:{document_id}:{chunk_index}")


async def ensure_collection(client: AsyncQdrantClient, tenant_slug: str) -> None:
    """Create this tenant's collection if it does not exist. Never recreates."""
    name = collection_name(tenant_slug)
    with _translated():
        if await client.collection_exists(name):
            return
        await client.create_collection(
            name,
            vectors_config={
                DENSE_VECTOR_NAME: models.VectorParams(
                    size=DENSE_DIMENSION, distance=models.Distance.COSINE
                )
            },
            sparse_vectors_config={SPARSE_VECTOR_NAME: models.SparseVectorParams()},
        )


async def upsert_chunks(
    client: AsyncQdrantClient,
    tenant_slug: str,
    *,
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
    document_slug: str,
    version: str,
    chunks: Sequence[Chunk],
) -> list[uuid.UUID]:
    """Write one point per chunk and return their ids, in chunk order.

    `document_slug` is in the payload so other versions can be found later.
    """
    name = collection_name(tenant_slug)
    if not chunks:
        return []

    ids = [point_id(tenant_id, document_id, index) for index in range(len(chunks))]
    points = [
        models.PointStruct(
            id=str(ids[index]),
            vector={
                DENSE_VECTOR_NAME: list(chunk.dense),
                SPARSE_VECTOR_NAME: models.SparseVector(
                    indices=list(chunk.sparse.indices), values=list(chunk.sparse.values)
                ),
            },
            payload={
                "document_id": str(document_id),
                "document_slug": document_slug,
                "version": version,
                "is_current": True,
                "heading_path": list(chunk.heading_path),
                "chunk_index": index,
                "text": chunk.text,
            },
        )
        for index, chunk in enumerate(chunks)
    ]

    with _translated():
        await client.upsert(name, points=points, wait=True)
    return ids


async def supersede_points(
    client: AsyncQdrantClient,
    tenant_slug: str,
    *,
    document_slug: str,
    keep_document_id: uuid.UUID,
) -> None:
    """Clear `is_current` on every other version of this document. Deletes nothing.

    Filtered by `document_slug` too, so other policies keep their points.
    """
    name = collection_name(tenant_slug)
    with _translated():
        await client.set_payload(
            name,
            payload={"is_current": False},
            points=models.Filter(
                must=[
                    models.FieldCondition(
                        key="document_slug", match=models.MatchValue(value=document_slug)
                    )
                ],
                must_not=[
                    models.FieldCondition(
                        key="document_id",
                        match=models.MatchValue(value=str(keep_document_id)),
                    )
                ],
            ),
            wait=True,
        )


class _translated:
    """Turn any `qdrant_client` failure into `VectorStoreUnavailableError`.
    Name checks sit outside it, so they keep their `VALIDATION` code."""

    def __enter__(self) -> "_translated":
        return self

    def __exit__(
        self, kind: object, error: BaseException | None, traceback: object
    ) -> Literal[False]:
        if isinstance(error, ApiException | ResponseHandlingException | OSError):
            raise VectorStoreUnavailableError("the policy vector store is unavailable") from error
        return False
