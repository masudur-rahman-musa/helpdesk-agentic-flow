"""The citation shape and its single title query.

Expected implementation surface (the implementer must provide exactly this):

``helpdesk.rag.retrieval``
    ``@dataclass(frozen=True) class Citation`` — EXACTLY ``document_slug``,
        ``title``, ``version``, ``heading_path``, ``chunk_id``, ``text``,
        ``score``. ``chunk_id`` is the Qdrant point id, so it joins one
        ``policy_chunks`` row through ``qdrant_point_id``.
    ``async def search_policy_chunks(session, client, *, tenant_slug: str,
        query: str, embedder: Embedder, top_k: int = TOP_K_DEFAULT)
        -> list[Citation]`` — the two arms, fused, with ``title`` resolved
        from ``policy_documents`` in ONE query for the whole page and never
        from the point payload (which carries no title).
    A hit whose ``document_id`` has no ``policy_documents`` row raises a
    typed ``ToolError`` rather than returning a blank title. The code is
    the implementer's to choose: the contract fixes "raises", not which of
    the contract's codes, so nothing here pins one.

    The store call these cases stand in for is
    ``client.query_points(name, query=..., using=..., limit=...,
    query_filter=..., with_payload=True)``, the shape
    ``test_rag_isolation.py`` already drives the real client with. Point ids
    come back as STRINGS, as the real store returns them for the string ids
    ``vectors.upsert_chunks`` writes.

Postgres only: the store is a stub whose two arms are canned, so
the title resolution is exercised with the page the case chose and no
collection is touched. The embedder is a fake for the same reason
`test_rag_chunking.py` uses one — no file in this change loads a model.

The database is `test_policy_models.py`'s session-scoped
`policy_db_url`, imported rather than rebuilt, and the tenant is the one it
seeds. Per-test isolation is a namespace: every case registers its own
uuid-suffixed document slugs.

No pytest-asyncio: each case is a sync function driving one `asyncio.run`.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from qdrant_client import AsyncQdrantClient
from sqlalchemy import event
from sqlalchemy.engine import Connection
from sqlalchemy.engine.interfaces import DBAPICursor, ExecutionContext
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from helpdesk.mcp_server.services.rag.chunking import Chunk
from helpdesk.mcp_server.services.rag.chunks import write_chunks
from helpdesk.mcp_server.services.rag.embedding import SparseVector
from helpdesk.mcp_server.services.rag.retrieval import Citation, search_policy_chunks
from helpdesk.mcp_server.services.rag.store import register_document_version
from helpdesk.mcp_server.services.rag.vectors import DENSE_VECTOR_NAME
from helpdesk.shared.core.db import create_engine, create_session_factory, tenant_session
from helpdesk.shared.core.errors import ToolError
from helpdesk.shared.crud import Repository
from helpdesk.shared.crud.tenants import find_tenant_by_slug
from helpdesk.shared.models import PolicyChunk, PolicyDocument

# Imported, not redefined: one database for the whole run.
from tests.shared.models.test_policy_models import (
    TENANT_A_SLUG,
    policy_db_url,  # noqa: F401
)

INGESTED_AT = datetime(2026, 5, 4, 9, 30, tzinfo=UTC)

# The contract's citation fields, as an exact set: an eighth field is the internal dense
# score leaking to the model, a missing one is a citation that cannot be
# rendered.
CITATION_FIELDS = frozenset(
    {"document_slug", "title", "version", "heading_path", "chunk_id", "text", "score"}
)


# --- the stubs ----------------------------------------------------------------


@dataclass(frozen=True)
class StubPoint:
    """One scored point, in the shape `query_points` returns them."""

    id: str
    score: float
    payload: dict[str, Any]


@dataclass(frozen=True)
class StubResponse:
    points: list[StubPoint]


class StubClient:
    """A store whose two arms answer with the page the case chose.

    Keyed on `using`, so the dense and the sparse arm can differ; every call
    is recorded, which is what lets a case assert how many requests were
    built as well as what came back.
    """

    def __init__(self, dense: Sequence[StubPoint], sparse: Sequence[StubPoint] = ()) -> None:
        self.calls: list[dict[str, Any]] = []
        self._dense = list(dense)
        self._sparse = list(sparse)

    async def query_points(self, *args: object, **kwargs: Any) -> StubResponse:
        self.calls.append(dict(kwargs))
        using = kwargs.get("using")
        return StubResponse(points=self._dense if using == DENSE_VECTOR_NAME else self._sparse)


def _as_client(stub: StubClient) -> AsyncQdrantClient:
    return cast(AsyncQdrantClient, stub)


class FakeEmbedder:
    """A deterministic `Embedder` with no model behind it.

    The vectors are never sent anywhere here — the stub arms ignore them —
    so their width is arbitrary; `test_rag_retrieval.py`, which writes into
    the real collection, uses the model's own width instead.
    """

    def embed_dense(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        return [(0.1, 0.2, 0.3) for _ in texts]

    def embed_sparse(self, texts: Sequence[str]) -> list[SparseVector]:
        return [SparseVector(indices=(1, 4), values=(0.6, 0.4)) for _ in texts]

    def count_tokens(self, text: str) -> int:
        return len(text.split())


class QueryRecorder:
    """Every SQL statement issued on one engine, from `start()` onwards.

    Counting statements rather than trusting the implementation's own shape
    is what makes "ONE title query for the whole page" assertable at all: a
    per-hit resolution and a single `IN` read return the same citations.
    """

    def __init__(self) -> None:
        self.statements: list[str] = []
        self._recording = False

    def attach(self, engine: AsyncEngine) -> None:
        @event.listens_for(engine.sync_engine, "before_cursor_execute")
        def _record(
            conn: Connection,
            cursor: DBAPICursor,
            statement: str,
            parameters: Any,
            context: ExecutionContext | None,
            executemany: bool,
        ) -> None:
            if self._recording:
                self.statements.append(statement)

    def start(self) -> None:
        self._recording = True
        self.statements.clear()

    def against(self, table: str) -> list[str]:
        return [
            statement
            for statement in self.statements
            if f"from {table}" in " ".join(statement.lower().split())
        ]


# --- planting the corpus rows -------------------------------------------------


def _chunk(index: int, text: str, *, heading: str) -> Chunk:
    return Chunk(
        heading_path=(heading,),
        char_start=index * 100,
        char_end=index * 100 + len(text),
        text=text,
        dense=(0.1, 0.2, 0.3),
        sparse=SparseVector(indices=(1, 2), values=(0.5, 0.5)),
    )


@dataclass(frozen=True)
class Planted:
    """One registered document version and the point ids of its chunk rows."""

    document_id: uuid.UUID
    slug: str
    title: str
    version: str
    point_ids: tuple[uuid.UUID, ...]
    chunks: tuple[Chunk, ...]


async def _plant(
    session: AsyncSession, *, title: str, texts: Sequence[str], version: str = "v1"
) -> Planted:
    """Register a document version and write one chunk row per text."""
    slug = f"fake-citations-{uuid.uuid4().hex[:8]}"
    result = await register_document_version(
        session,
        tenant_slug=TENANT_A_SLUG,
        slug=slug,
        title=title,
        version=version,
        source_path=f"policies/{TENANT_A_SLUG}/{version}/{slug}.md",
        source=f"# {title}\n\n{' '.join(texts)}\n".encode(),
        ingested_at=INGESTED_AT,
    )
    chunks = tuple(
        _chunk(index, text, heading=f"{index + 1}. Purpose") for index, text in enumerate(texts)
    )
    point_ids = tuple(uuid.uuid4() for _ in chunks)
    await write_chunks(
        session, document_id=result.document.id, chunks=chunks, point_ids=list(point_ids)
    )
    return Planted(
        document_id=result.document.id,
        slug=slug,
        title=title,
        version=version,
        point_ids=point_ids,
        chunks=chunks,
    )


def _point(planted: Planted, index: int, *, score: float) -> StubPoint:
    """The store's own view of one planted chunk, payload and all."""
    chunk = planted.chunks[index]
    return StubPoint(
        id=str(planted.point_ids[index]),
        score=score,
        payload={
            "document_id": str(planted.document_id),
            "document_slug": planted.slug,
            "version": planted.version,
            "is_current": True,
            "heading_path": list(chunk.heading_path),
            "chunk_index": index,
            "text": chunk.text,
        },
    )


def _drive[T](
    url: str,
    work: Callable[[async_sessionmaker[AsyncSession], QueryRecorder], Awaitable[T]],
) -> T:
    """One engine, one recorder and one event loop for this case."""

    async def scenario() -> T:
        engine = create_engine(url)
        recorder = QueryRecorder()
        recorder.attach(engine)
        try:
            return await work(create_session_factory(engine), recorder)
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


async def _tenant_id(factory: async_sessionmaker[AsyncSession], slug: str) -> uuid.UUID:
    async with factory() as session:
        tenant = await find_tenant_by_slug(session, slug)
    assert tenant is not None, slug
    return tenant.id


# --- the citation shape (no database) -----------------------------------------


def test_the_citation_carries_exactly_the_seven_spec_fields() -> None:
    """Catches a `Citation` that forwards the internal dense cosine or the
    `document_id` to the model, and one that drops `title` back onto the
    payload: the contract fixes the set exactly, so both directions fail here."""
    assert frozenset(field.name for field in fields(Citation)) == CITATION_FIELDS


# --- the title, resolved from policy_documents --------------------------------


def test_the_title_is_the_documents_own_row(policy_db_url: str) -> None:  # noqa: F811
    """Catches a title read from the point payload (there is no
    title key there, so it arrives blank) or synthesised from the document
    slug: the planted row's title is prose the slug cannot produce."""
    title = "Fake Physical Access Policy"

    async def work(
        factory: async_sessionmaker[AsyncSession], recorder: QueryRecorder
    ) -> tuple[list[Citation], Planted]:
        tenant_id = await _tenant_id(factory, TENANT_A_SLUG)
        async with tenant_session(factory, tenant_id) as session:
            planted = await _plant(
                session,
                title=title,
                texts=["Fake spare badges are issued by reception."],
            )
        async with tenant_session(factory, tenant_id) as session:
            client = StubClient(dense=[_point(planted, 0, score=0.81)])
            citations = await search_policy_chunks(
                session,
                _as_client(client),
                tenant_slug=TENANT_A_SLUG,
                query="fake spare badges",
                embedder=FakeEmbedder(),
            )
        return citations, planted

    citations, planted = _drive(policy_db_url, work)

    assert len(citations) == 1
    assert citations[0].title == planted.title
    assert citations[0].document_slug == planted.slug
    assert citations[0].version == planted.version
    assert citations[0].chunk_id == planted.point_ids[0]
    assert citations[0].text == planted.chunks[0].text
    assert citations[0].heading_path == planted.chunks[0].heading_path


def test_every_returned_chunk_id_selects_exactly_one_chunk_row(
    policy_db_url: str,  # noqa: F811
) -> None:
    """Catches a `chunk_id` that is anything but the Qdrant point id — the
    chunk index, a re-minted uuid, the document id — each of which joins zero
    `policy_chunks` rows and makes a stored citation unresolvable later."""

    async def work(
        factory: async_sessionmaker[AsyncSession], recorder: QueryRecorder
    ) -> tuple[list[Citation], list[uuid.UUID]]:
        tenant_id = await _tenant_id(factory, TENANT_A_SLUG)
        async with tenant_session(factory, tenant_id) as session:
            planted = await _plant(
                session,
                title="Fake Access Control Policy",
                texts=[
                    "Fake access is granted by the system owner.",
                    "Fake access is reviewed every quarter.",
                ],
            )
        async with tenant_session(factory, tenant_id) as session:
            client = StubClient(
                dense=[_point(planted, 0, score=0.8), _point(planted, 1, score=0.7)]
            )
            citations = await search_policy_chunks(
                session,
                _as_client(client),
                tenant_slug=TENANT_A_SLUG,
                query="fake access",
                embedder=FakeEmbedder(),
            )
        async with tenant_session(factory, tenant_id) as session:
            rows = await Repository(session, PolicyChunk).list()
            matched = [
                row.qdrant_point_id
                for row in rows
                if row.qdrant_point_id in {citation.chunk_id for citation in citations}
            ]
        return citations, [point_id for point_id in matched if point_id is not None]

    citations, matched = _drive(policy_db_url, work)

    assert len(citations) == 2
    for citation in citations:
        assert matched.count(citation.chunk_id) == 1, citation.chunk_id


def test_a_page_over_three_documents_issues_one_title_query(
    policy_db_url: str,  # noqa: F811
) -> None:
    """Catches a title resolved per hit: six hits over three
    documents would then issue three to six reads of `policy_documents` and
    return exactly the same citations, so only the statement count separates
    the two. Recording starts after the planting, so the registrations'
    own reads cannot be mistaken for the page's."""

    async def work(
        factory: async_sessionmaker[AsyncSession], recorder: QueryRecorder
    ) -> tuple[list[Citation], list[str]]:
        tenant_id = await _tenant_id(factory, TENANT_A_SLUG)
        async with tenant_session(factory, tenant_id) as session:
            documents = [
                await _plant(
                    session,
                    title=f"Fake Policy Number {index}",
                    texts=[
                        f"Fake clause {index} first span about access.",
                        f"Fake clause {index} second span about review.",
                    ],
                )
                for index in range(3)
            ]
        dense = [
            _point(document, index, score=0.9 - 0.05 * (position * 2 + index))
            for position, document in enumerate(documents)
            for index in (0, 1)
        ]
        async with tenant_session(factory, tenant_id) as session:
            recorder.start()
            citations = await search_policy_chunks(
                session,
                _as_client(StubClient(dense=dense)),
                tenant_slug=TENANT_A_SLUG,
                query="fake clause",
                embedder=FakeEmbedder(),
                top_k=6,
            )
        return citations, recorder.against("policy_documents")

    citations, title_queries = _drive(policy_db_url, work)

    # The premise, read off the page rather than assumed:
    # six hits really did span three documents, so one query is a batch and
    # not an artefact of a shorter page.
    assert len(citations) == 6
    assert len({citation.document_slug for citation in citations}) == 3
    assert len(title_queries) == 1, title_queries


def test_the_citations_keep_the_fused_order(policy_db_url: str) -> None:  # noqa: F811
    """Catches a page re-ordered by the title resolution — grouped by
    document, or emitted in the order the `policy_documents` rows came back —
    which the previous case cannot see because it only counts statements. The
    arm interleaves two documents, so document order and fused order differ."""

    async def work(
        factory: async_sessionmaker[AsyncSession], recorder: QueryRecorder
    ) -> tuple[list[Citation], list[uuid.UUID]]:
        tenant_id = await _tenant_id(factory, TENANT_A_SLUG)
        async with tenant_session(factory, tenant_id) as session:
            first = await _plant(
                session,
                title="Fake Joiner Mover Leaver",
                texts=["Fake joiner clause one.", "Fake joiner clause two."],
            )
            second = await _plant(
                session,
                title="Fake Acceptable Use",
                texts=["Fake use clause one.", "Fake use clause two."],
            )
        dense = [
            _point(second, 1, score=0.90),
            _point(first, 0, score=0.80),
            _point(second, 0, score=0.70),
            _point(first, 1, score=0.60),
        ]
        expected = [uuid.UUID(point.id) for point in dense]
        async with tenant_session(factory, tenant_id) as session:
            citations = await search_policy_chunks(
                session,
                _as_client(StubClient(dense=dense)),
                tenant_slug=TENANT_A_SLUG,
                query="fake clause",
                embedder=FakeEmbedder(),
                top_k=4,
            )
        return citations, expected

    citations, expected = _drive(policy_db_url, work)

    assert [citation.chunk_id for citation in citations] == expected


# --- the unhappy path ---------------------------------------------------------


def test_a_hit_whose_document_row_is_gone_raises_instead_of_a_blank_title(
    policy_db_url: str,  # noqa: F811
) -> None:
    """Catches `titles.get(document_id, "")` and any `LEFT JOIN` shape: a
    citation the agent cannot render is a wrong answer, not a cosmetic gap.

    The hit's `chunk_id` is a chunk row that DOES exist, planted under
    another document, so a missing-chunk guard cannot be what refuses this
    page — only the title lookup can (the arranged case that is needed)."""

    async def work(
        factory: async_sessionmaker[AsyncSession], recorder: QueryRecorder
    ) -> tuple[type[BaseException] | None, str, int]:
        tenant_id = await _tenant_id(factory, TENANT_A_SLUG)
        async with tenant_session(factory, tenant_id) as session:
            live = await _plant(
                session,
                title="Fake Live Policy",
                texts=["Fake live clause that keeps its chunk row."],
            )
            doomed = await _plant(session, title="Fake Doomed Policy", texts=[])
        async with tenant_session(factory, tenant_id) as session:
            deleted = await Repository(session, PolicyDocument).delete(doomed.document_id)
        assert deleted, "the doomed document row was not deleted"

        # A point whose payload names the deleted document while its id is the
        # surviving chunk's: exactly the contract's "document_id has no row".
        orphan = StubPoint(
            id=str(live.point_ids[0]),
            score=0.88,
            payload={
                "document_id": str(doomed.document_id),
                "document_slug": doomed.slug,
                "version": doomed.version,
                "is_current": True,
                "heading_path": ["1. Purpose"],
                "chunk_index": 0,
                "text": "Fake orphaned span.",
            },
        )
        async with tenant_session(factory, tenant_id) as session:
            rows = await Repository(session, PolicyChunk).list()
            surviving = len([row for row in rows if row.qdrant_point_id == live.point_ids[0]])
            with pytest.raises(ToolError) as excinfo:
                await search_policy_chunks(
                    session,
                    _as_client(StubClient(dense=[orphan])),
                    tenant_slug=TENANT_A_SLUG,
                    query="fake orphaned span",
                    embedder=FakeEmbedder(),
                )
            return type(excinfo.value), str(excinfo.value), surviving

    raised, message, surviving = _drive(policy_db_url, work)

    # The premise: the chunk row the hit names is present, so the refusal is
    # the missing TITLE and nothing else.
    assert surviving == 1
    assert raised is not None
    assert message != ""
