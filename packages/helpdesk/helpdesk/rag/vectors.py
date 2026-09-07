"""The per-tenant Qdrant collections (the design, §12.1.5, §12.1.6, §12.4).

The only module in this repo that imports `qdrant_client`. Everything the
vector store can be asked comes through here, so there is exactly one place
that knows the collection naming rule, one that builds a client, and one that
turns the store's own exceptions into the design codes.

**Isolation is the collection name.** One collection per tenant, named
`policy_{tenant_slug}`, so a cross-tenant read requires naming another
tenant's collection rather than getting a filter wrong — the design's claim,
pinned end to end by `test_rag_isolation.py`. The slug goes through 14a's
`check_slug` and the finished name through `check_collection_name` before any
request is built (a project rule: the name is a URL path segment, an unsized
store with no backstop of its own); pinned by
`test_rag_vectors.py::test_a_tenant_slug_one_over_the_slug_cap_builds_no_request`,
which asserts the store was never called at all.

**Two named vectors, always.** `DENSE_VECTOR_NAME` at the dense model's own
width under cosine, `SPARSE_VECTOR_NAME` for bm25. Row 15's hybrid search
needs both and a collection cannot gain a vector later without a re-ingest,
so both are created up front even though nothing reads the sparse one yet.
Pinned by `test_rag_vectors.py::test_ensure_collection_creates_both_named_vectors`
and `::test_every_point_carries_both_named_vectors`.

**Points are never deleted.** `supersede_points` writes `is_current=false`
over the points of the versions a document has moved past (decision D):
the design keeps superseded content readable for audit, and an answer
citing an old version must still resolve. The point count is therefore
identical before and after, pinned by
`test_rag_vectors.py::test_superseding_flips_the_other_version_and_deletes_nothing`.

**Orphan points are the accepted failure mode** (decision C). The upsert runs
before the caller's transaction commits, so a rollback after a successful
upsert leaves points no `policy_chunks` row references. They are unreachable
to retrieval, which filters on `is_current` and joins the document its payload
names, and they are overwritten in place by the next ingest of the same
document version because the ids are deterministic. The reverse order would
leave a stored citation resolving to nothing, which is a wrong answer to a
user rather than garbage in a store; a sweep belongs with the scheduler row.
"""

import uuid
from collections.abc import Sequence

from qdrant_client import AsyncQdrantClient, models
from qdrant_client.http.exceptions import ApiException, ResponseHandlingException

from helpdesk.core.settings import Settings
from helpdesk.errors import ErrorCode, ToolError
from helpdesk.rag.chunking import Chunk
from helpdesk.rag.embedding import DENSE_DIMENSION
from helpdesk.rag.naming import (
    COLLECTION_NAME_PREFIX,
    check_collection_name,
    check_slug,
)

DENSE_VECTOR_NAME = "dense"
"""The named dense vector of every policy collection.

Named rather than default: a collection with one unnamed vector cannot gain
the sparse half without being rebuilt, and rebuilding drops every point.
Pinned by `test_rag_vectors.py::test_ensure_collection_creates_both_named_vectors`.
"""

SPARSE_VECTOR_NAME = "sparse"
"""The named sparse (bm25) vector, for the lexical half of row 15's search."""

POINT_ID_NAMESPACE = uuid.UUID("6f5c0d2e-1a4b-5c8d-9e0f-7a1b2c3d4e5f")
"""The fixed uuid5 namespace point ids are minted in (decision B).

A constant, never regenerated: the id of a chunk must be reproducible from the
`policy_chunks` row it sits on, which is what makes a divergence between the
two stores checkable at all. Pinned by
`test_rag_vectors.py::test_re_upserting_the_same_document_returns_the_same_ids_and_no_new_points`.
"""


class VectorStoreUnavailableError(ToolError):
    """The vector store could not be reached or refused the request.

    `INTERNAL`, not `VALIDATION`: nothing about the caller's arguments is
    wrong — every name they supplied was checked before the request was
    built. The message deliberately names neither the store nor its URL,
    because a tool boundary forwards it to a user and both are internal
    infrastructure; the original exception stays in the chained cause, where
    a log line reads it. Pinned by
    `test_rag_vectors.py::test_an_unreachable_qdrant_is_typed_internal_without_naming_the_store`.
    """

    code = ErrorCode.INTERNAL


def collection_name(tenant_slug: str) -> str:
    """`policy_{tenant_slug}`, or `InvalidPolicyNameError` (`VALIDATION`).

    Pure: builds no request and touches no client, so every caller below can
    put it first and be certain a refused name never reached the store. Both
    gates run — `check_slug` for the shape, `check_collection_name` for the
    length of the whole — and `check_slug` is the narrower of the two (plan
    14c §5), which is why
    `test_rag_vectors.py::test_a_tenant_slug_that_is_not_a_lowercase_hyphen_slug_builds_no_request`
    exists beside the length pair: a space or an uppercase letter is well
    inside every cap and only the pattern can refuse it.
    """
    check_slug(tenant_slug)
    return check_collection_name(f"{COLLECTION_NAME_PREFIX}{tenant_slug}")


def create_client(settings: Settings) -> AsyncQdrantClient:
    """The one place a Qdrant client is constructed, timeout included.

    `qdrant_timeout_seconds` is passed here rather than per call (standing
    rules 11 and 14): a client built anywhere else would carry the library's
    own default and turn an unreachable store into a request that never
    returns. Asserted on the CONSTRUCTED client by
    `test_rag_vectors.py::test_the_constructed_client_carries_the_configured_timeout`,
    so a setting that is read but never passed fails there.
    """
    return AsyncQdrantClient(url=settings.qdrant_url, timeout=settings.qdrant_timeout_seconds)


def point_id(tenant_id: uuid.UUID, document_id: uuid.UUID, chunk_index: int) -> uuid.UUID:
    """The deterministic id of one chunk's point (decision B).

    `uuid5` over `(tenant_id, document_id, chunk_index)`, so re-upserting a
    document version OVERWRITES its own points instead of doubling them, and
    the id stored on a `policy_chunks` row can be recomputed from that row.
    The rejected alternative, `uuid4`, is pinned as a failure by
    `test_rag_vectors.py::test_re_upserting_the_same_document_returns_the_same_ids_and_no_new_points`;
    that the document is part of the key, and not the tenant and index alone,
    by `::test_two_documents_in_one_tenant_get_different_point_ids`.
    """
    return uuid.uuid5(POINT_ID_NAMESPACE, f"{tenant_id}:{document_id}:{chunk_index}")


async def ensure_collection(client: AsyncQdrantClient, tenant_slug: str) -> None:
    """Create this tenant's collection if it does not exist. Idempotent.

    "If it does not exist" rather than `recreate_collection`: re-ingesting a
    corpus must not be a destructive operation, and the difference between the
    two is invisible in a collection listing. Pinned by
    `test_rag_vectors.py::test_ensure_collection_a_second_time_keeps_the_points_already_written`,
    which counts the surviving points rather than the collections.

    The check and the create are not atomic. Two concurrent ingests of one new
    tenant can both see it missing; the loser's `create_collection` is what
    `_translated` turns into a typed refusal rather than a raw 409. Ingestion
    is a single walker today (`scripts/ingest.py`), so nothing races it.
    """
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

    The payload is exactly the design's fields plus `document_slug`, and
    nothing else: an extra key is a field row 15's filters would not know
    about, a missing `is_current` makes the current-version filter match every
    version at once. Pinned as an exact key set by
    `test_rag_vectors.py::test_the_upserted_payload_is_exactly_the_spec_key_set`.

    `document_slug` is in the payload because `supersede_points` has to find
    "every other version of this document" from inside the collection, where
    `document_id` differs per version and nothing else names the document.
    An earlier change §1 fixed the payload as six keys and the supersede lookup as a
    slug in the same section; this is that pair resolved (the project notes
    2026-09-08, a project rule).

    Ids are deterministic (`point_id`), so this is an overwrite for a document
    version already written and the caller can store the returned ids on the
    matching `policy_chunks` rows. Returning them rather than recomputing them
    at the call site keeps one definition of the id.

    An empty `chunks` writes nothing and returns nothing: Qdrant refuses an
    empty upsert, and a document that chunked to nothing is a corpus file of
    headings only, not an error.
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
    """Clear `is_current` on every OTHER version of this document. Deletes nothing.

    Decision D: the design retains superseded content, so this is a payload
    write and never a delete — the collection holds exactly as many points
    afterwards as before, which is what
    `test_rag_vectors.py::test_superseding_flips_the_other_version_and_deletes_nothing`
    asserts.

    The filter is narrowed by `document_slug` as well as by the kept document,
    so another policy in the same tenant keeps its own current points; a
    supersede matching on "not the kept document" alone would clear every
    other document in the collection, pinned by
    `::test_superseding_leaves_another_documents_points_current`.
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

    A context manager rather than a decorator so the pre-checks above it stay
    outside: a refused name must raise `InvalidPolicyNameError` (`VALIDATION`)
    and not be re-typed `INTERNAL` on its way out.

    Both halves of the library's hierarchy are caught — `ApiException` for a
    response the store rejected, `ResponseHandlingException` for one it never
    sent — plus `OSError`, which is what a closed port raises before either
    can wrap it.
    """

    def __enter__(self) -> "_translated":
        return self

    def __exit__(self, kind: object, error: BaseException | None, traceback: object) -> bool:
        if isinstance(error, ApiException | ResponseHandlingException | OSError):
            raise VectorStoreUnavailableError("the policy vector store is unavailable") from error
        return False
