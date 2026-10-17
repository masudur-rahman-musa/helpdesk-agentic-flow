"""The two arms and their pre-checks (`rag/retrieval.py`, `naming.py`).

Expected implementation surface (the implementer must provide exactly this):

``helpdesk.rag.naming``
    ``MAX_QUERY_CHARS = 1000`` / ``MAX_TOP_K = 50``.
    ``def check_query(value: str) -> str`` — accepts a query of exactly
        ``MAX_QUERY_CHARS``, refuses the empty string and 1001 characters
        with a typed ``VALIDATION`` error.
    ``def check_top_k(value: int) -> int`` — accepts 1 and ``MAX_TOP_K``,
        refuses 0 and 51, same code.

``helpdesk.rag.retrieval``
    ``async def search_policy_chunks(session, client, *, tenant_slug: str,
        query: str, embedder: Embedder, top_k: int = TOP_K_DEFAULT)
        -> list[Citation]`` — both pre-checks BEFORE any request is built,
        then a dense arm (``using=DENSE_VECTOR_NAME``) and a sparse arm
        (``using=SPARSE_VECTOR_NAME``) against the one collection
        ``vectors.collection_name`` names, each filtered ``is_current=true``
        and each asking for ``CANDIDATES_PER_ARM`` candidates, fused by
        ``fuse`` and cut to ``top_k``. The embedder is passed in and never
        constructed here, as `pipeline.py` already does it.
        An unreachable store raises ``VectorStoreUnavailableError``.

Postgres comes from the shared conftest `session_factory`
-- one engine for the whole run, schema created once -- and Qdrant is the
compose instance. Per-test
isolation is a namespace — this file seeds ONE tenant of its own, whose slug
is the collection name, and every case plants its own uuid-suffixed document
slug inside it. The collection is dropped at both ends of the session.

No file in this change loads a real model: the arms are driven with vectors the
case chooses, the way `test_rag_chunking.py` drives the merge.

No pytest-asyncio: each case is a sync function driving one `asyncio.run`.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

import httpx
import pytest
from qdrant_client import AsyncQdrantClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from helpdesk.mcp_server.services.rag.chunking import Chunk
from helpdesk.mcp_server.services.rag.chunks import write_chunks
from helpdesk.mcp_server.services.rag.embedding import DENSE_DIMENSION, SparseVector
from helpdesk.mcp_server.services.rag.retrieval import (
    CANDIDATES_PER_ARM,
    Citation,
    search_policy_chunks,
)
from helpdesk.mcp_server.services.rag.store import register_document_version
from helpdesk.mcp_server.services.rag.vectors import (
    DENSE_VECTOR_NAME,
    SPARSE_VECTOR_NAME,
    VectorStoreUnavailableError,
    collection_name,
    create_client,
    ensure_collection,
    supersede_points,
    upsert_chunks,
)
from helpdesk.shared.core.db import tenant_session
from helpdesk.shared.core.errors import ErrorCode, ToolError
from helpdesk.shared.core.naming import (
    MAX_QUERY_CHARS,
    MAX_TOP_K,
    check_query,
    check_top_k,
)
from tests.mcp_server.services.rag.test_rag_vectors import (
    CLOSED_PORT_URL,
    _settings,
    qdrant_url,  # noqa: F401
)

# Imported, not redefined: one Qdrant connectivity gate for the whole run
# . The database comes from the shared `session_factory`.
from tests.support.seed import seed_tenant

INGESTED_AT = datetime(2026, 5, 4, 9, 30, tzinfo=UTC)


# --- the tenant, its collection and the fakes ---------------------------------


@pytest.fixture(scope="session")
def retrieval_tenant(
    session_factory: async_sessionmaker[AsyncSession],
    qdrant_url: str,  # noqa: F811
) -> Iterator[tuple[str, uuid.UUID]]:
    """One seeded tenant for the whole file, and its collection, dropped twice.

    A tenant of its own on the shared database: the collection is named after
    the tenant slug, so a slug nothing else ingests IS the namespace. Dropped before as well as
    after, so a previous run's points can
    neither hide a filter bug nor invent one.
    """

    async def prepare() -> tuple[str, uuid.UUID]:
        seeded = await seed_tenant(session_factory, "fake-retrieval")
        return seeded.tenant_slug, seeded.tenant_id

    slug, tenant_id = asyncio.run(prepare())
    name = collection_name(slug)
    httpx.delete(f"{qdrant_url}/collections/{name}", timeout=30.0)
    yield slug, tenant_id
    httpx.delete(f"{qdrant_url}/collections/{name}", timeout=30.0)


class RecordingEmbedder:
    """An `Embedder` that records every call and costs nothing.

    Vectors are at the real model's width because they are written into and
    queried against the real collection, which rejects any other. The dense
    vector is the same for every text, so a planted chunk and a query are
    near neighbours by construction and the arms return something whatever
    the case's wording.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def embed_dense(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        self.calls.append("embed_dense")
        return [_dense_vector() for _ in texts]

    def embed_sparse(self, texts: Sequence[str]) -> list[SparseVector]:
        self.calls.append("embed_sparse")
        return [SparseVector(indices=(1, 5, 9), values=(0.4, 0.3, 0.3)) for _ in texts]

    def count_tokens(self, text: str) -> int:
        self.calls.append("count_tokens")
        return len(text.split())


@dataclass(frozen=True)
class StubResponse:
    points: list[Any]


class RecordingStore:
    """A stand-in recording every call a request would go through.

    Used by the cases that must prove NO request was built, and by the one
    that reads back what each arm ASKED for; both arms answer with an empty
    page, so nothing downstream depends on the store's contents.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query_points(self, *args: object, **kwargs: Any) -> StubResponse:
        self.calls.append(("query_points", dict(kwargs)))
        return StubResponse(points=[])

    async def collection_exists(self, *args: object, **kwargs: object) -> bool:
        self.calls.append(("collection_exists", {}))
        return True

    async def create_collection(self, *args: object, **kwargs: object) -> bool:
        self.calls.append(("create_collection", {}))
        return True

    async def search(self, *args: object, **kwargs: Any) -> list[Any]:
        self.calls.append(("search", dict(kwargs)))
        return []

    async def scroll(self, *args: object, **kwargs: object) -> tuple[list[Any], None]:
        self.calls.append(("scroll", {}))
        return [], None


def _as_client(store: RecordingStore) -> AsyncQdrantClient:
    return cast(AsyncQdrantClient, store)


def _dense_vector() -> tuple[float, ...]:
    """One all-positive vector of the model's width, shared by every text.

    Cosine is then high for every planted chunk, so what comes back is
    decided by the FILTERS and the page size, which is what these cases are
    about — never by whether a fake similarity happened to clear a floor.
    """
    return tuple(0.05 + (index % 7) / 100.0 for index in range(DENSE_DIMENSION))


def _chunk(index: int, text: str) -> Chunk:
    return Chunk(
        heading_path=(f"{index + 1}. Purpose",),
        char_start=index * 100,
        char_end=index * 100 + len(text),
        text=text,
        dense=_dense_vector(),
        sparse=SparseVector(indices=(1, 5, 9), values=(0.4, 0.3, 0.3)),
    )


@dataclass(frozen=True)
class Planted:
    """One registered version, in both stores."""

    document_id: uuid.UUID
    slug: str
    version: str
    point_ids: tuple[uuid.UUID, ...]


async def _plant(
    session: AsyncSession,
    client: AsyncQdrantClient,
    *,
    tenant_slug: str,
    tenant_id: uuid.UUID,
    slug: str,
    version: str,
    texts: Sequence[str],
) -> Planted:
    """Register a version, write its points, write its chunk rows."""
    result = await register_document_version(
        session,
        tenant_slug=tenant_slug,
        slug=slug,
        title=f"Fake Policy {slug} {version}",
        version=version,
        source_path=f"policies/{tenant_slug}/{version}/{slug}.md",
        source=f"# Fake Policy\n\n{' '.join(texts)}\n".encode(),
        ingested_at=INGESTED_AT,
    )
    chunks = [_chunk(index, text) for index, text in enumerate(texts)]
    point_ids = await upsert_chunks(
        client,
        tenant_slug,
        tenant_id=tenant_id,
        document_id=result.document.id,
        document_slug=slug,
        version=version,
        chunks=chunks,
    )
    await write_chunks(session, document_id=result.document.id, chunks=chunks, point_ids=point_ids)
    return Planted(
        document_id=result.document.id,
        slug=slug,
        version=version,
        point_ids=tuple(point_ids),
    )


def _drive[T](
    factory: async_sessionmaker[AsyncSession],
    work: Callable[[async_sessionmaker[AsyncSession], AsyncQdrantClient], Awaitable[T]],
) -> T:
    """The shared factory and one Qdrant client, both inside this case's event loop."""

    async def scenario() -> T:
        client = create_client(_settings())
        try:
            return await work(factory, client)
        finally:
            await client.close()

    return asyncio.run(scenario())


def _drive_db[T](
    factory: async_sessionmaker[AsyncSession],
    work: Callable[[async_sessionmaker[AsyncSession]], Awaitable[T]],
) -> T:
    """The same, for the cases that must never reach the store at all."""

    async def scenario() -> T:
        return await work(factory)

    return asyncio.run(scenario())


# --- the caps, pure -----------------------------------------------------------


def test_the_query_and_page_caps_are_exactly_the_planned_values() -> None:
    """Catches a cap of any other size: both bound a value that reaches a
    service with no backstop of its own — a 512-token model input and a
    Qdrant `limit` — so a band would admit the wrong one."""
    assert MAX_QUERY_CHARS == 1000
    assert MAX_TOP_K == 50


@pytest.mark.parametrize(
    "value",
    [pytest.param(1, id="the-smallest-page"), pytest.param(50, id="exactly-the-cap")],
)
def test_check_top_k_accepts_the_inclusive_boundary(value: int) -> None:
    """Catches an exclusive comparison at either end (`> 1`, `< MAX_TOP_K`):
    50 is the largest page the store may be asked for and 1 the smallest
    useful one, and refusing either is a refusal of a legal call."""
    assert check_top_k(value) == value


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(0, id="an-empty-page"),
        pytest.param(-1, id="a-negative-page"),
        pytest.param(51, id="one-over-the-cap"),
    ],
)
def test_check_top_k_refuses_a_page_outside_the_bounds(value: int) -> None:
    """Catches a missing bound in either direction: 0 and -1 reach Qdrant as
    a `limit` it interprets itself, and 51 is an unbounded page size on a
    request nothing else caps."""
    with pytest.raises(ToolError) as excinfo:
        check_top_k(value)
    assert excinfo.value.code is ErrorCode.VALIDATION


def test_check_query_accepts_a_query_of_exactly_the_cap() -> None:
    """Catches a `>=` where the cap is inclusive: 1000 characters is the
    longest question a user may ask, and refusing it turns the boundary into
    a validation error on a legal query."""
    query = "fake question " * 100
    assert check_query(query[:MAX_QUERY_CHARS]) == query[:MAX_QUERY_CHARS]


@pytest.mark.parametrize(
    ("value", "label"),
    [
        pytest.param("", "empty", id="the-empty-string"),
        pytest.param("x" * 1001, "over", id="one-character-over-the-cap"),
    ],
)
def test_check_query_refuses_an_empty_or_oversized_query(value: str, label: str) -> None:
    """Catches a check that only measures length (the empty string then
    embeds to a zero vector that sits near every chunk, so the refusal
    threshold reads a meaningless cosine) and one with no cap at all."""
    with pytest.raises(ToolError) as excinfo:
        check_query(value)
    assert excinfo.value.code is ErrorCode.VALIDATION
    assert label in {"empty", "over"}


# --- the pre-checks build no request ------------------------------------------


@pytest.mark.parametrize(
    ("top_k", "query"),
    [
        pytest.param(0, "fake question", id="top-k-zero"),
        pytest.param(51, "fake question", id="top-k-one-over-the-cap"),
        pytest.param(6, "x" * 1001, id="a-query-one-character-over-the-cap"),
    ],
)
def test_a_refused_argument_reaches_neither_the_store_nor_the_embedder(
    session_factory: async_sessionmaker[AsyncSession],
    retrieval_tenant: tuple[str, uuid.UUID],
    top_k: int,
    query: str,
) -> None:
    """Catches a check placed after the query is embedded or after the arms
    are issued: both stand-ins record every call, so "refused" and "refused
    before any effect" are told apart here rather than inferred from the
    exception. An oversized query would otherwise be sent to a
    model that truncates at 512 tokens and to a store with no cap of its own."""
    tenant_slug, tenant_id = retrieval_tenant
    store = RecordingStore()
    embedder = RecordingEmbedder()

    async def work(factory: async_sessionmaker[AsyncSession]) -> ErrorCode:
        async with tenant_session(factory, tenant_id) as session:
            with pytest.raises(ToolError) as excinfo:
                await search_policy_chunks(
                    session,
                    _as_client(store),
                    tenant_slug=tenant_slug,
                    query=query,
                    embedder=embedder,
                    top_k=top_k,
                )
            return excinfo.value.code

    code = _drive_db(session_factory, work)

    assert code is ErrorCode.VALIDATION
    assert store.calls == [], "the refused call reached the store"
    assert embedder.calls == [], "the refused call reached the embedder"


# --- what each arm asks for ---------------------------------------------------


def test_each_arm_asks_for_the_candidate_count_against_its_own_named_vector(
    session_factory: async_sessionmaker[AsyncSession],
    retrieval_tenant: tuple[str, uuid.UUID],
) -> None:
    """Catches an arm that fetches `top_k` candidates instead of 30 (fusion then becomes a
    no-op and nothing else in this suite can see it)
    and a search that issues ONE request — a dense-only or server-side-fused
    search, which was rejected because its k is not 60."""
    tenant_slug, tenant_id = retrieval_tenant
    store = RecordingStore()

    async def work(factory: async_sessionmaker[AsyncSession]) -> list[tuple[str, dict[str, Any]]]:
        async with tenant_session(factory, tenant_id) as session:
            await search_policy_chunks(
                session,
                _as_client(store),
                tenant_slug=tenant_slug,
                query="fake question about badges",
                embedder=RecordingEmbedder(),
                top_k=6,
            )
        return store.calls

    calls = _drive_db(session_factory, work)

    queries = [kwargs for name, kwargs in calls if name == "query_points"]
    assert len(queries) == 2, calls
    assert {kwargs["using"] for kwargs in queries} == {DENSE_VECTOR_NAME, SPARSE_VECTOR_NAME}
    assert [kwargs["limit"] for kwargs in queries] == [CANDIDATES_PER_ARM] * 2


# --- the real collection ------------------------------------------------------


def test_only_the_current_version_comes_back_after_a_supersede(
    session_factory: async_sessionmaker[AsyncSession],
    retrieval_tenant: tuple[str, uuid.UUID],
) -> None:
    """Catches an arm built without the `is_current` filter: both versions of
    the document are in the collection with identical vectors, so an
    unfiltered search returns the superseded v1 spans beside the v2 ones and
    the agent cites a policy that no longer applies.

    The v1 points are asserted still PRESENT, so this is the filter and not
    a delete."""
    tenant_slug, tenant_id = retrieval_tenant
    slug = f"fake-supersede-{uuid.uuid4().hex[:8]}"

    async def work(
        factory: async_sessionmaker[AsyncSession], client: AsyncQdrantClient
    ) -> tuple[list[Citation], int]:
        await ensure_collection(client, tenant_slug)
        async with tenant_session(factory, tenant_id) as session:
            await _plant(
                session,
                client,
                tenant_slug=tenant_slug,
                tenant_id=tenant_id,
                slug=slug,
                version="v1",
                texts=[
                    "Fake badge loss was reported to reception in the old policy.",
                    "Fake spare badges were kept behind the desk in the old policy.",
                ],
            )
        async with tenant_session(factory, tenant_id) as session:
            new = await _plant(
                session,
                client,
                tenant_slug=tenant_slug,
                tenant_id=tenant_id,
                slug=slug,
                version="v2",
                texts=[
                    "Fake badge loss is reported to reception the same day.",
                    "Fake spare badges are issued by reception and logged.",
                ],
            )
        await supersede_points(
            client, tenant_slug, document_slug=slug, keep_document_id=new.document_id
        )

        points, _ = await client.scroll(collection_name(tenant_slug), limit=1000, with_payload=True)
        stored_v1 = len(
            [
                point
                for point in points
                if point.payload is not None
                and point.payload["document_slug"] == slug
                and point.payload["version"] == "v1"
            ]
        )
        async with tenant_session(factory, tenant_id) as session:
            citations = await search_policy_chunks(
                session,
                client,
                tenant_slug=tenant_slug,
                query="fake badge loss and spare badges",
                embedder=RecordingEmbedder(),
                top_k=20,
            )
        return citations, stored_v1

    citations, stored_v1 = _drive(session_factory, work)

    # The premise: the superseded points are still in the collection, so what
    # follows is the filter doing its job and not a deletion.
    assert stored_v1 == 2

    mine = [citation for citation in citations if citation.document_slug == slug]
    assert len(mine) == 2, [citation.version for citation in mine]
    assert {citation.version for citation in mine} == {"v2"}


def test_the_page_is_never_longer_than_top_k(
    session_factory: async_sessionmaker[AsyncSession],
    retrieval_tenant: tuple[str, uuid.UUID],
) -> None:
    """Catches a search that returns both arms' candidates concatenated, or
    that cuts to `CANDIDATES_PER_ARM` instead of `top_k`: the document holds
    eight matching chunks and three were asked for, so an uncut page reaches
    the agent's prompt at whatever size the store answered with."""
    tenant_slug, tenant_id = retrieval_tenant
    slug = f"fake-page-size-{uuid.uuid4().hex[:8]}"
    texts = [f"Fake clause number {index} about access and badges." for index in range(8)]

    async def work(
        factory: async_sessionmaker[AsyncSession], client: AsyncQdrantClient
    ) -> tuple[list[Citation], int]:
        await ensure_collection(client, tenant_slug)
        async with tenant_session(factory, tenant_id) as session:
            planted = await _plant(
                session,
                client,
                tenant_slug=tenant_slug,
                tenant_id=tenant_id,
                slug=slug,
                version="v1",
                texts=texts,
            )
        async with tenant_session(factory, tenant_id) as session:
            citations = await search_policy_chunks(
                session,
                client,
                tenant_slug=tenant_slug,
                query="fake clause about access and badges",
                embedder=RecordingEmbedder(),
                top_k=3,
            )
        return citations, len(planted.point_ids)

    citations, planted_count = _drive(session_factory, work)

    # The premise: more matching chunks exist than the page asked for.
    assert planted_count == 8
    assert len(citations) == 3


def test_an_unreachable_store_is_typed_internal_without_naming_the_store(
    session_factory: async_sessionmaker[AsyncSession],
    retrieval_tenant: tuple[str, uuid.UUID],
) -> None:
    """Catches arms issued outside `vectors.py`'s `_translated`: the raw
    `qdrant_client` exception carries the store's name and its URL into a
    message a tool boundary forwards to a user, and it arrives under no
    the contract code at all. A partial failure is one typed INTERNAL, never half a
    page returned as a whole one."""
    tenant_slug, tenant_id = retrieval_tenant
    settings = _settings().model_copy(
        update={"qdrant_url": CLOSED_PORT_URL, "qdrant_timeout_seconds": 2.0}
    )

    async def scenario() -> tuple[ErrorCode, str]:
        client = create_client(settings)
        try:
            async with tenant_session(session_factory, tenant_id) as session:
                with pytest.raises(VectorStoreUnavailableError) as excinfo:
                    await search_policy_chunks(
                        session,
                        client,
                        tenant_slug=tenant_slug,
                        query="fake question about badges",
                        embedder=RecordingEmbedder(),
                    )
                return excinfo.value.code, str(excinfo.value)
        finally:
            await client.close()

    code, message = asyncio.run(scenario())

    assert code is ErrorCode.INTERNAL
    lowered = message.lower()
    assert "qdrant" not in lowered
    assert "127.0.0.1" not in message
    assert "http" not in lowered
