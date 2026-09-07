"""An earlier change §6: the ingestion composition in `helpdesk/rag/pipeline.py`.

Expected implementation surface (the implementer must provide exactly this):

``helpdesk.rag.pipeline``
    ``class Redactor(Protocol)`` — ``async def redact_document(self, text: str,
        tenant_slug: str) -> RedactedDocument``; satisfied by 14b's
        ``rag.gateway.RedactionClient`` and by this file's fake (decision E:
        the pipeline constructs neither a gateway client nor a model).
    ``@dataclass(frozen=True) class IngestResult`` — ``document:
        PolicyDocument``, ``created: bool``, ``chunk_count: int``.
    ``async def ingest_document(session, client, *, tenant_slug: str,
        slug: str, title: str, version: str, source_path: str, source: bytes,
        redactor: Redactor, embedder: Embedder,
        ingested_at: datetime) -> IngestResult``
    ``async def ingest_version(factory, client, *, tenant_id: uuid.UUID,
        corpus_root: Path, tenant_slug: str, version: str, redactor: Redactor,
        embedder: Embedder, ingested_at: datetime) -> list[IngestResult]``

    ``ingest_document`` composes the modules by NAME in its own namespace —
    ``split_sections``, ``chunk_document``, ``register_document_version``,
    ``ensure_collection``, ``upsert_chunks``, ``write_chunks``,
    ``supersede_points`` — so the order case below can wrap each one. The
    order is ``EXPECTED_ORDER``, which is an earlier change §1's with the registration
    one place earlier; the comment there says why, and the plan's Deviations
    records it. Qdrant still runs before the chunk ROWS (decision C), and all
    three writes share the caller's one transaction.

a project rule: Postgres is `test_policy_models.py`'s session-scoped
`policy_db_url`; Qdrant is the compose instance, isolated by
`test_rag_vectors.py`'s per-run collection NAME prefix. Neither is rebuilt.

The redactor and the embedder are FAKES in every case here (decision E): no
gateway is reachable in a test run and loading the real models costs seconds
per session for behaviour `test_rag_embedding.py` already pins. The fake
embedder still returns vectors of the real model's width, because the real
Qdrant collection this suite writes to rejects any other.

No pytest-asyncio: each case is a sync function driving one `asyncio.run`.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from helpdesk.core.db import create_engine, create_session_factory, tenant_session
from helpdesk.core.repository import Repository, find_tenant_by_slug
from helpdesk.errors import ErrorCode, ToolError
from helpdesk.models import PolicyChunk, PolicyDocument
from helpdesk.rag import pipeline
from helpdesk.rag.embedding import DENSE_DIMENSION, SparseVector
from helpdesk.rag.gateway import RedactedDocument
from helpdesk.rag.naming import InvalidPolicyNameError
from helpdesk.rag.pipeline import ingest_document, ingest_version
from helpdesk.rag.vectors import (
    VectorStoreUnavailableError,
    collection_name,
    create_client,
    ensure_collection,
)
from qdrant_client import AsyncQdrantClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Imported, not redefined: one database and one Qdrant
# namespace for the whole run.
from test_policy_models import (
    TENANT_A_SLUG,
    policy_db_url,  # noqa: F401
)
from test_rag_vectors import (
    RecordingClient,
    _as_client,
    _settings,
    qdrant_url,  # noqa: F401
)


@pytest.fixture(scope="session")
def pipeline_collection(qdrant_url: str) -> Iterator[str]:  # noqa: F811
    """Tenant A's collection, dropped at both ends of the session.

    This suite's tenant slug is fixed by the seed (`register_document_version`
    cross-checks it against the bound tenant), so it cannot sit inside
    `test_rag_vectors.py`'s per-run name prefix. The namespace is instead the
    seeded tenant's own slug, which nothing but this test database ever
    ingests; the collection is dropped once per session rather than per test, and the cases assert deltas rather than absolutes.
    """
    name = collection_name(TENANT_A_SLUG)
    httpx.delete(f"{qdrant_url}/collections/{name}", timeout=30.0)
    yield name
    httpx.delete(f"{qdrant_url}/collections/{name}", timeout=30.0)


INGESTED_AT = datetime(2026, 5, 4, 9, 30, tzinfo=UTC)
INGESTED_AGAIN = datetime(2026, 6, 1, 8, 0, tzinfo=UTC)

SOURCE_V1 = (
    b"# Fake Access Control Policy\n\n"
    b"## 1. Purpose\n\n"
    b"Fake access to a system is granted by that system's owner. "
    b"Fake requests are recorded and reviewed every quarter.\n\n"
    b"## 2. Scope\n\n"
    b"Fake contractors follow the same rules as employees.\n"
)
SOURCE_V1_CHANGED = SOURCE_V1.replace(b"every quarter", b"every month")

# The order, as one list. The names are the pipeline attributes each step goes
# through, so a step that moved shows up as a reordered list rather than as a
# downstream symptom.
#
# `register_document_version` sits one place EARLIER than an earlier change §1 states
# it, and the plan's Deviations records why: §1 also fixes a payload carrying
# `document_id` and a point id derived from it (decision B), and neither
# exists until the `policy_documents` row does, so §1's own three criteria
# cannot all hold. What decision C forbids is unaffected — the upsert still
# runs before the chunk ROWS, and all three writes share one transaction, so
# `test_a_failing_upsert_leaves_no_document_and_no_chunk_row` below asserts
# zero documents as well as zero chunk rows.
EXPECTED_ORDER: tuple[str, ...] = (
    "redact_document",
    "split_sections",
    "chunk_document",
    "register_document_version",
    "ensure_collection",
    "upsert_chunks",
    "write_chunks",
    "supersede_points",
)


# --- the fakes (decision E) ---------------------------------------------------


class FakeRedactor:
    """A `Redactor` that replaces one known string and files a fixed map id.

    Deterministic and offline: the point of decision E's seam is that the
    pipeline suite needs no gateway. The substitution is real, so a pipeline
    that split the RAW source instead of the redacted one produces chunk text
    this file can see (`test_the_chunks_are_cut_from_the_redacted_text`).
    """

    def __init__(self, recorder: list[str] | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self._recorder = recorder

    async def redact_document(self, text: str, tenant_slug: str) -> RedactedDocument:
        if self._recorder is not None:
            self._recorder.append("redact_document")
        self.calls.append((text, tenant_slug))
        return RedactedDocument(
            text=text.replace("contractors", "<PERSON_1>"),
            redaction_map_id="fake-redaction-map-0001",
        )


class FakeEmbedder:
    """A deterministic `Embedder` at the real model's width, with no model.

    `DENSE_DIMENSION` rather than a short vector: these cases write into the
    real compose Qdrant, whose collection is created at the model's width and
    rejects anything else. Token counting is whitespace words, as in
    `test_rag_chunking.py`.
    """

    def embed_dense(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        return [self._vector(text) for text in texts]

    def embed_sparse(self, texts: Sequence[str]) -> list[SparseVector]:
        return [SparseVector(indices=(1, 4), values=(0.6, 0.4)) for _ in texts]

    def count_tokens(self, text: str) -> int:
        return len(text.split())

    @staticmethod
    def _vector(text: str) -> tuple[float, ...]:
        seed = sum(text.encode("utf-8")) % 97
        return tuple(((seed + index) % 13) / 13.0 + 0.01 for index in range(DENSE_DIMENSION))


# --- drivers ------------------------------------------------------------------


def _drive[T](
    url: str,
    work: Callable[[async_sessionmaker[AsyncSession], AsyncQdrantClient], Awaitable[T]],
) -> T:
    """One engine and one Qdrant client, both inside this case's event loop."""

    async def scenario() -> T:
        engine = create_engine(url)
        client = create_client(_settings())
        try:
            return await work(create_session_factory(engine), client)
        finally:
            await client.close()
            await engine.dispose()

    return asyncio.run(scenario())


async def _tenant_id(factory: async_sessionmaker[AsyncSession], slug: str) -> uuid.UUID:
    async with factory() as session:
        tenant = await find_tenant_by_slug(session, slug)
    assert tenant is not None, slug
    return tenant.id


async def _counts(
    factory: async_sessionmaker[AsyncSession],
    client: AsyncQdrantClient,
    tenant_id: uuid.UUID,
    tenant_slug: str,
    document_slug: str,
) -> tuple[int, int, int]:
    """(document rows, chunk rows, points) for one document — both stores."""
    async with tenant_session(factory, tenant_id) as session:
        documents = [
            row
            for row in await Repository(session, PolicyDocument).list()
            if row.slug == document_slug
        ]
        ids = {row.id for row in documents}
        chunks = [
            row for row in await Repository(session, PolicyChunk).list() if row.document_id in ids
        ]
    counted = await client.count(collection_name(tenant_slug), exact=True)
    return len(documents), len(chunks), int(counted.count)


async def _chunk_texts(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, document_id: uuid.UUID
) -> list[str]:
    async with tenant_session(factory, tenant_id) as session:
        rows = [
            row
            for row in await Repository(session, PolicyChunk).list()
            if row.document_id == document_id
        ]
    return [row.text for row in sorted(rows, key=lambda row: row.chunk_index)]


def _namespace_slug() -> str:
    return f"fake-pipeline-{uuid.uuid4().hex[:12]}"


def _tenant_slug(namespace: str, suffix: str) -> str:
    return f"{namespace}-{suffix}"


# --- the effect order ---------------------------------------------------------


def test_the_effects_run_in_the_order_section_one_states(
    policy_db_url: str,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches decision C reversed — the chunk rows written before the points
    exist, which leaves a stored citation resolving to nothing when the upsert
    then fails — and catches a supersede that runs before the new version's
    rows land, which would leave the tenant with no current version for the
    length of the transaction. The whole order is asserted as one list, so any
    step that moves fails here rather than as a downstream symptom."""
    document_slug = _namespace_slug()
    order: list[str] = []

    def _wrap(name: str) -> None:
        original = getattr(pipeline, name)

        async def recorded(*args: Any, **kwargs: Any) -> Any:
            order.append(name)
            return await original(*args, **kwargs)

        monkeypatch.setattr(pipeline, name, recorded)

    def _wrap_sync(name: str) -> None:
        original = getattr(pipeline, name)

        def recorded(*args: Any, **kwargs: Any) -> Any:
            order.append(name)
            return original(*args, **kwargs)

        monkeypatch.setattr(pipeline, name, recorded)

    for name in ("split_sections", "chunk_document"):
        _wrap_sync(name)
    for name in (
        "ensure_collection",
        "upsert_chunks",
        "register_document_version",
        "write_chunks",
        "supersede_points",
    ):
        _wrap(name)

    async def work(factory: async_sessionmaker[AsyncSession], client: AsyncQdrantClient) -> None:
        tenant_id = await _tenant_id(factory, TENANT_A_SLUG)
        async with tenant_session(factory, tenant_id) as session:
            await ingest_document(
                session,
                client,
                tenant_slug=TENANT_A_SLUG,
                slug=document_slug,
                title="Fake Access Control Policy",
                version="v1",
                source_path=f"policies/{TENANT_A_SLUG}/v1/{document_slug}.md",
                source=SOURCE_V1,
                redactor=FakeRedactor(order),
                embedder=FakeEmbedder(),
                ingested_at=INGESTED_AT,
            )

    _drive(policy_db_url, work)
    assert tuple(order) == EXPECTED_ORDER


def test_the_chunks_are_cut_from_the_redacted_text(policy_db_url: str) -> None:  # noqa: F811
    """Catches a pipeline that splits and chunks the RAW source and redacts
    only what it sends onward (or redacts nothing at all): the word the fake
    redactor removes would then be stored in `policy_chunks.text`, which is
    precisely the leak 14b decision G's "redact first" order exists to
    prevent."""
    document_slug = _namespace_slug()

    async def work(
        factory: async_sessionmaker[AsyncSession], client: AsyncQdrantClient
    ) -> list[str]:
        tenant_id = await _tenant_id(factory, TENANT_A_SLUG)
        async with tenant_session(factory, tenant_id) as session:
            result = await ingest_document(
                session,
                client,
                tenant_slug=TENANT_A_SLUG,
                slug=document_slug,
                title="Fake Access Control Policy",
                version="v1",
                source_path=f"policies/{TENANT_A_SLUG}/v1/{document_slug}.md",
                source=SOURCE_V1,
                redactor=FakeRedactor(),
                embedder=FakeEmbedder(),
                ingested_at=INGESTED_AT,
            )
            document_id = result.document.id
        return await _chunk_texts(factory, tenant_id, document_id)

    texts = _drive(policy_db_url, work)
    joined = "\n".join(texts)
    assert joined, "the document produced no chunks at all"
    assert "contractors" not in joined
    assert "<PERSON_1>" in joined


# --- idempotence, on BOTH stores ----------------------------------------------


def test_re_ingesting_an_unchanged_source_writes_nothing_to_either_store(
    policy_db_url: str,  # noqa: F811
    pipeline_collection: str,
) -> None:
    """Catches a pipeline that ignores 14a's sha256 short-circuit: the second
    run appends a second set of chunk rows for the same version (Postgres
    count doubles) even where deterministic point ids keep Qdrant's count
    flat, so counting only the points would miss it. Both counts are taken
    before and after."""
    document_slug = _namespace_slug()

    async def work(
        factory: async_sessionmaker[AsyncSession], client: AsyncQdrantClient
    ) -> tuple[tuple[int, int, int], tuple[int, int, int], bool]:
        tenant_id = await _tenant_id(factory, TENANT_A_SLUG)
        async with tenant_session(factory, tenant_id) as session:
            await ingest_document(
                session,
                client,
                tenant_slug=TENANT_A_SLUG,
                slug=document_slug,
                title="Fake Access Control Policy",
                version="v1",
                source_path=f"policies/{TENANT_A_SLUG}/v1/{document_slug}.md",
                source=SOURCE_V1,
                redactor=FakeRedactor(),
                embedder=FakeEmbedder(),
                ingested_at=INGESTED_AT,
            )
        before = await _counts(factory, client, tenant_id, TENANT_A_SLUG, document_slug)

        async with tenant_session(factory, tenant_id) as session:
            again = await ingest_document(
                session,
                client,
                tenant_slug=TENANT_A_SLUG,
                slug=document_slug,
                title="Fake Access Control Policy",
                version="v1",
                source_path=f"policies/{TENANT_A_SLUG}/v1/{document_slug}.md",
                source=SOURCE_V1,
                redactor=FakeRedactor(),
                embedder=FakeEmbedder(),
                ingested_at=INGESTED_AGAIN,
            )
        after = await _counts(factory, client, tenant_id, TENANT_A_SLUG, document_slug)
        return before, after, again.created

    before, after, created = _drive(policy_db_url, work)
    assert created is False
    assert before == after
    assert before[0] == 1
    assert before[1] > 0


def test_re_ingesting_a_changed_source_replaces_the_rows_rather_than_appending(
    policy_db_url: str,  # noqa: F811
) -> None:
    """Catches a pipeline that calls `write_chunks` where the source changed
    under the same version directory: the version's old spans stay beside the
    new ones, so one version has two chunk 0 rows and half its text is a cut
    the file no longer contains."""
    document_slug = _namespace_slug()

    async def work(
        factory: async_sessionmaker[AsyncSession], client: AsyncQdrantClient
    ) -> tuple[int, list[str]]:
        tenant_id = await _tenant_id(factory, TENANT_A_SLUG)
        for source in (SOURCE_V1, SOURCE_V1_CHANGED):
            async with tenant_session(factory, tenant_id) as session:
                result = await ingest_document(
                    session,
                    client,
                    tenant_slug=TENANT_A_SLUG,
                    slug=document_slug,
                    title="Fake Access Control Policy",
                    version="v1",
                    source_path=f"policies/{TENANT_A_SLUG}/v1/{document_slug}.md",
                    source=source,
                    redactor=FakeRedactor(),
                    embedder=FakeEmbedder(),
                    ingested_at=INGESTED_AT,
                )
                document_id = result.document.id
        texts = await _chunk_texts(factory, tenant_id, document_id)
        return len(texts), texts

    count, texts = _drive(policy_db_url, work)
    joined = "\n".join(texts)
    assert count > 0
    assert "every month" in joined
    assert "every quarter" not in joined


# --- decision C: a failing upsert leaves no chunk row -------------------------


def test_a_failing_upsert_leaves_no_document_and_no_chunk_row(
    policy_db_url: str,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches decision C's order reversed: with Postgres written first, a
    Qdrant failure leaves `policy_chunks` rows whose `qdrant_point_id` names
    a point that does not exist — 14a decision F's forbidden state. Deleting
    only the "upsert before the writes" ordering flips this case; the
    transaction's rollback alone cannot, because a pipeline that wrote first
    and committed before upserting would still leave the rows."""
    document_slug = _namespace_slug()

    async def failing_upsert(*args: Any, **kwargs: Any) -> list[uuid.UUID]:
        raise VectorStoreUnavailableError("the policy vector store is unavailable")

    monkeypatch.setattr(pipeline, "upsert_chunks", failing_upsert)

    async def work(
        factory: async_sessionmaker[AsyncSession], client: AsyncQdrantClient
    ) -> tuple[int, int]:
        tenant_id = await _tenant_id(factory, TENANT_A_SLUG)
        with pytest.raises(VectorStoreUnavailableError) as excinfo:
            async with tenant_session(factory, tenant_id) as session:
                await ingest_document(
                    session,
                    client,
                    tenant_slug=TENANT_A_SLUG,
                    slug=document_slug,
                    title="Fake Access Control Policy",
                    version="v1",
                    source_path=f"policies/{TENANT_A_SLUG}/v1/{document_slug}.md",
                    source=SOURCE_V1,
                    redactor=FakeRedactor(),
                    embedder=FakeEmbedder(),
                    ingested_at=INGESTED_AT,
                )
        assert excinfo.value.code is ErrorCode.INTERNAL
        documents, chunks, _ = await _counts(
            factory, client, tenant_id, TENANT_A_SLUG, document_slug
        )
        return documents, chunks

    documents, chunks = _drive(policy_db_url, work)
    assert (documents, chunks) == (0, 0)


# --- the corpus-walk refusals, each before any effect -------------------------


def _refuse_version(
    url: str,
    *,
    corpus_root: Path,
    tenant_slug: str,
    version: str,
    error: type[ToolError],
) -> None:
    """Call `ingest_version` and prove nothing was asked of either store.

    The Qdrant client is the recording stand-in, so "before any effect" is an
    assertion about calls made rather than about rows seen; the redactor
    records too, because a pipeline that redacted first and refused second
    would have already sent a document to the gateway.
    """
    recording = RecordingClient()
    redactor = FakeRedactor()

    async def scenario() -> None:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            tenant_id = await _tenant_id(factory, TENANT_A_SLUG)
            with pytest.raises(error) as excinfo:
                await ingest_version(
                    factory,
                    _as_client(recording),
                    tenant_id=tenant_id,
                    corpus_root=corpus_root,
                    tenant_slug=tenant_slug,
                    version=version,
                    redactor=redactor,
                    embedder=FakeEmbedder(),
                    ingested_at=INGESTED_AT,
                )
            assert excinfo.value.code is ErrorCode.VALIDATION
        finally:
            await engine.dispose()

    asyncio.run(scenario())
    assert recording.calls == [], "the refused call reached the vector store"
    assert redactor.calls == [], "the refused call reached the redaction gateway"


def test_a_tenant_with_no_corpus_directory_is_refused_before_any_effect(
    policy_db_url: str,  # noqa: F811
    tmp_path: Path,
) -> None:
    """Catches an `ingest_version` that walks a missing directory as an empty
    one: a typo'd tenant then reports "0 documents ingested" and exits 0,
    which is the failure mode where a tenant silently has no policy corpus
    and every answer refuses for lack of evidence."""
    (tmp_path / TENANT_A_SLUG / "v1").mkdir(parents=True)
    _refuse_version(
        policy_db_url,
        corpus_root=tmp_path,
        tenant_slug=f"no-such-tenant-{uuid.uuid4().hex[:8]}",
        version="v1",
        error=ToolError,
    )


def test_a_version_directory_that_is_not_vn_is_refused_before_any_effect(
    policy_db_url: str,  # noqa: F811
    tmp_path: Path,
) -> None:
    """Catches an `ingest_version` that skips 14a's `check_version`: `1.0` is
    the corpus front-matter spelling decision G rejects, and a run that
    accepted it would register rows carrying a version string the Qdrant
    payload filter can never match. The directory EXISTS here, so only the
    version pre-check can refuse this call — the missing-directory guard
    above cannot decide it."""
    (tmp_path / TENANT_A_SLUG / "1.0").mkdir(parents=True)
    (tmp_path / TENANT_A_SLUG / "1.0" / "fake-policy.md").write_bytes(SOURCE_V1)
    _refuse_version(
        policy_db_url,
        corpus_root=tmp_path,
        tenant_slug=TENANT_A_SLUG,
        version="1.0",
        error=InvalidPolicyNameError,
    )


# --- the corpus walk, happy path ----------------------------------------------


def test_ingest_version_ingests_every_markdown_file_in_the_directory(
    policy_db_url: str,  # noqa: F811
    pipeline_collection: str,
    tmp_path: Path,
) -> None:
    """Catches an `ingest_version` that stops at the first file (a `return`
    where a loop was meant) or that picks up non-markdown siblings: the
    directory holds two `.md` files and one `.txt`, so both a short walk and
    a greedy one fail, and neither could be seen with a single-file
    fixture."""
    version_dir = tmp_path / TENANT_A_SLUG / "v1"
    version_dir.mkdir(parents=True)
    slugs = sorted(_namespace_slug() for _ in range(2))
    for slug in slugs:
        (version_dir / f"{slug}.md").write_bytes(SOURCE_V1)
    (version_dir / "notes.txt").write_bytes(b"Fake notes, not a policy.\n")

    async def work(
        factory: async_sessionmaker[AsyncSession], client: AsyncQdrantClient
    ) -> list[str]:
        tenant_id = await _tenant_id(factory, TENANT_A_SLUG)
        await ensure_collection(client, TENANT_A_SLUG)
        results = await ingest_version(
            factory,
            client,
            tenant_id=tenant_id,
            corpus_root=tmp_path,
            tenant_slug=TENANT_A_SLUG,
            version="v1",
            redactor=FakeRedactor(),
            embedder=FakeEmbedder(),
            ingested_at=INGESTED_AT,
        )
        return sorted(result.document.slug for result in results)

    assert _drive(policy_db_url, work) == slugs
