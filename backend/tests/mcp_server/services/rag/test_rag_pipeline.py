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

An earlier change extends this file with `ingest_one_document`, the single-file twin of
`ingest_version`; its own expected surface is written out above its cases,
at the end of the file.

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
from qdrant_client import AsyncQdrantClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from helpdesk.mcp_server.services.rag import pipeline
from helpdesk.mcp_server.services.rag.embedding import DENSE_DIMENSION, SparseVector
from helpdesk.mcp_server.services.rag.pipeline import (
    CorpusDocumentNotFoundError,
    ingest_document,
    ingest_one_document,
    ingest_version,
)
from helpdesk.mcp_server.services.rag.redaction_client import RedactedDocument
from helpdesk.mcp_server.services.rag.vectors import (
    VectorStoreUnavailableError,
    collection_name,
    create_client,
    ensure_collection,
)
from helpdesk.shared.core.db import create_engine, create_session_factory, tenant_session
from helpdesk.shared.core.errors import ErrorCode, ToolError
from helpdesk.shared.core.naming import SLUG_LENGTH, InvalidPolicyNameError
from helpdesk.shared.crud import Repository
from helpdesk.shared.crud.tenants import find_tenant_by_slug
from helpdesk.shared.models import PolicyChunk, PolicyDocument
from tests.mcp_server.services.rag.test_rag_vectors import (
    RecordingClient,
    _as_client,
    _settings,
    qdrant_url,  # noqa: F401
)

# Imported, not redefined: one database and one Qdrant
# namespace for the whole run.
from tests.shared.models.test_policy_models import (
    TENANT_A_SLUG,
    policy_db_url,  # noqa: F401
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
INGESTED_LAST = datetime(2026, 7, 2, 7, 15, tzinfo=UTC)

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


# --- an earlier change: one document, addressed by slug and version --------------------
#
# Expected implementation surface (written before it exists):
#
# ``helpdesk.rag.pipeline``
#     ``class CorpusDocumentNotFoundError(ToolError)`` - ``code =
#     ErrorCode.NOT_FOUND``, raised before any effect when the corpus file is
#     not on disk (an earlier change decision C: the FILE is the source of truth, so a
#     missing `policy_documents` row is not the absence).
#     ``async def ingest_one_document(session, client, *, corpus_root: Path,
#     tenant_slug: str, slug: str, version: str, redactor: Redactor,
#     embedder: Embedder, ingested_at: datetime) -> IngestResult`` - the
#     single-file twin of ``ingest_version`` (decision H). It owns the same two
#     things that function owns and nothing else: WHERE a corpus file lives
#     (``{corpus_root}/{tenant_slug}/{version}/{slug}.md``) and what its
#     ``source_path`` string is (``{tenant_slug}/{version}/{slug}.md``,
#     corpus-relative). A session rather than a factory: the write runs in the
#     CALLER's transaction, which is the MCP pipeline's.
#
# Both name checks run before the path is composed, so a traversing slug is a
# typed refusal and never a read; the cases below assert that with the
# filesystem, the redaction gateway, the embedder and the vector store all
# observed at zero calls, never merely that an error was raised.

TRAVERSING_SLUGS: tuple[str, ...] = ("..", "a/b", "../../etc/passwd", "policy\n")
"""a project rule's rejection table for the one argument that is a PATH
segment: the two dot forms, a separator, and a trailing newline."""

REFUSED_VERSIONS: tuple[str, ...] = ("v0", "1.0", "v1\n")
"""`VERSION_PATTERN` is the narrower gate, not `VERSION_LENGTH` (standing rule
32): all three fit the 8-character column and none matches `v[1-9][0-9]{0,3}`.
`1.0` is the front-matter spelling 14a decision G rejects, and `v1\\n` is the
trailing character a project rule asks for."""

SOURCE_ONE = (
    b"# Fake Single Document Policy\n\n"
    b"## 1. Purpose\n\n"
    b"Fake single-document marker THE-CORRECT-FILE. Access is granted by the "
    b"system owner and reviewed every quarter.\n"
)
SOURCE_ONE_CHANGED = SOURCE_ONE.replace(b"every quarter", b"every month")
DECOY_SOURCE = (
    b"# Fake Decoy Policy\n\n"
    b"## 1. Purpose\n\n"
    b"Fake decoy marker THE-WRONG-FILE. Nothing here may ever reach a chunk "
    b"row, because no correct path composition can read it.\n"
)
CORRECT_MARKER = "THE-CORRECT-FILE"
WRONG_MARKER = "THE-WRONG-FILE"


class CountingEmbedder(FakeEmbedder):
    """`FakeEmbedder`, plus the call record a "before any effect" case needs.

    The pipeline chunks through the embedder (`count_tokens`) and vectorises
    through it, so any refusal that had already reached the chunker shows up
    here as a non-empty list.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def embed_dense(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        self.calls.append("embed_dense")
        return super().embed_dense(texts)

    def embed_sparse(self, texts: Sequence[str]) -> list[SparseVector]:
        self.calls.append("embed_sparse")
        return super().embed_sparse(texts)

    def count_tokens(self, text: str) -> int:
        self.calls.append("count_tokens")
        return super().count_tokens(text)


def _plant(path: Path, source: bytes) -> bool:
    """Write one corpus file, reporting whether this filesystem took the name.

    A decoy named for a traversing slug (`policy\\n.md`) is legal on POSIX and
    refused by some filesystems. Planting it is what makes the refusal case a
    KILL rather than an existence check - without the decoy, deleting
    `check_slug` would answer `NOT_FOUND` instead of `VALIDATION` and the case
    would still be red for the wrong reason - so the return value is read by
    the caller as a premise rather than swallowed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_bytes(source)
    except OSError:
        return False
    return True


def _corpus(root: Path, tenant_slug: str, version: str, slug: str) -> Path:
    """The one path an earlier change fixes: `{root}/{tenant}/{version}/{slug}.md`."""
    return root / tenant_slug / version / f"{slug}.md"


def _sixty_four_character_slug() -> str:
    """A slug of EXACTLY `SLUG_LENGTH` characters that `SLUG_PATTERN` admits.

    Namespaced by a uuid4 stem like every other slug in this file (standing
    rule 7: the database and the collection are shared and never rebuilt), then
    padded to the cap with lowercase letters the pattern accepts.
    """
    stem = f"fake-boundary-{uuid.uuid4().hex[:12]}-"
    return stem + "a" * (SLUG_LENGTH - len(stem))


async def _document_rows(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, document_slug: str
) -> list[tuple[str, bool, datetime]]:
    """(version, is_current, ingested_at) for one slug, oldest ingest first."""
    async with tenant_session(factory, tenant_id) as session:
        rows = [
            row
            for row in await Repository(session, PolicyDocument).list()
            if row.slug == document_slug
        ]
    return [
        (row.version, row.is_current, row.ingested_at)
        for row in sorted(rows, key=lambda row: row.ingested_at)
    ]


def _refuse_one(
    url: str,
    *,
    corpus_root: Path,
    slug: str,
    version: str,
    error: type[ToolError],
    code: ErrorCode,
) -> None:
    """Call `ingest_one_document` and prove nothing was asked of anything.

    The same shape as `_refuse_version` above, with the embedder added: a
    refusal that had already chunked would have paid the model pass the name
    checks exist to skip, and one that had already redacted would have sent a
    document to the gateway.
    """
    recording = RecordingClient()
    redactor = FakeRedactor()
    embedder = CountingEmbedder()

    async def scenario() -> None:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            tenant_id = await _tenant_id(factory, TENANT_A_SLUG)
            with pytest.raises(error) as excinfo:
                async with tenant_session(factory, tenant_id) as session:
                    await ingest_one_document(
                        session,
                        _as_client(recording),
                        corpus_root=corpus_root,
                        tenant_slug=TENANT_A_SLUG,
                        slug=slug,
                        version=version,
                        redactor=redactor,
                        embedder=embedder,
                        ingested_at=INGESTED_AT,
                    )
            assert excinfo.value.code is code
        finally:
            await engine.dispose()

    asyncio.run(scenario())
    assert recording.calls == [], "the refused call reached the vector store"
    assert redactor.calls == [], "the refused call reached the redaction gateway"
    assert embedder.calls == [], "the refused call reached the embedder"


def test_ingest_one_document_reads_the_version_directorys_file(
    policy_db_url: str,  # noqa: F811
    pipeline_collection: str,
    tmp_path: Path,
) -> None:
    """Catches any path composition but
    `{root}/{tenant}/{version}/{slug}.md` and any `source_path` but
    `{tenant}/{version}/{slug}.md`.

    Three decoys carrying a DIFFERENT marker sit at the three layouts a tool
    composing its own path would plausibly reach - the corpus root, the
    tenant directory and a version directory under the slug rather than the
    slug under the version - so a wrong composition does not merely fail to
    find a file: it ingests the wrong document, and the marker in
    `policy_chunks.text` says which one. The `source_path` is asserted as the
    EXACT corpus-relative string `ingest_version` already composes for a walk,
    so an absolute path (which would carry the ingest host's directory layout
    into the tenant's rows) and a `policies/`-prefixed one both fail.
    """
    document_slug = _namespace_slug()
    assert _plant(_corpus(tmp_path, TENANT_A_SLUG, "v1", document_slug), SOURCE_ONE)
    assert _plant(tmp_path / f"{document_slug}.md", DECOY_SOURCE)
    assert _plant(tmp_path / TENANT_A_SLUG / f"{document_slug}.md", DECOY_SOURCE)
    assert _plant(tmp_path / TENANT_A_SLUG / document_slug / "v1.md", DECOY_SOURCE)

    async def work(
        factory: async_sessionmaker[AsyncSession], client: AsyncQdrantClient
    ) -> tuple[str, int, list[str]]:
        tenant_id = await _tenant_id(factory, TENANT_A_SLUG)
        async with tenant_session(factory, tenant_id) as session:
            result = await ingest_one_document(
                session,
                client,
                corpus_root=tmp_path,
                tenant_slug=TENANT_A_SLUG,
                slug=document_slug,
                version="v1",
                redactor=FakeRedactor(),
                embedder=FakeEmbedder(),
                ingested_at=INGESTED_AT,
            )
            document_id = result.document.id
            source_path = result.document.source_path
            chunk_count = result.chunk_count
        return source_path, chunk_count, await _chunk_texts(factory, tenant_id, document_id)

    source_path, chunk_count, texts = _drive(policy_db_url, work)
    joined = "\n".join(texts)

    assert source_path == f"{TENANT_A_SLUG}/v1/{document_slug}.md"
    assert chunk_count > 0
    assert CORRECT_MARKER in joined
    assert WRONG_MARKER not in joined


def test_a_missing_corpus_file_is_not_found_before_any_effect(
    policy_db_url: str,  # noqa: F811
    tmp_path: Path,
) -> None:
    """Catches a tool that answers a missing corpus file as a SUCCESS
    reporting zero documents - the shape a walk of an empty directory has, and
    the one that would let an admin read "re-ingested, 0 chunks" for a file
    that is not there at all - and one that lets the read raise
    `FileNotFoundError`, which crosses the boundary as an untyped `INTERNAL`
    telling the admin the platform broke.

    The version DIRECTORY exists and both names are valid, so neither name
    check can decide this row and only the file's own absence can (standing
    rule 16). `NOT_FOUND` is asserted as the code rather than the class alone,
    and all three stand-ins at zero calls, which is decision C's other half:
    the file is the source of truth, so nothing may be written or sent before
    it has been found.
    """
    (tmp_path / TENANT_A_SLUG / "v1").mkdir(parents=True)

    _refuse_one(
        policy_db_url,
        corpus_root=tmp_path,
        slug=_namespace_slug(),
        version="v1",
        error=CorpusDocumentNotFoundError,
        code=ErrorCode.NOT_FOUND,
    )


def test_a_slug_of_exactly_sixty_four_characters_is_accepted(
    policy_db_url: str,  # noqa: F811
    pipeline_collection: str,
    tmp_path: Path,
) -> None:
    """The inclusive half of the boundary at `naming.SLUG_LENGTH`.

    Catches a pre-check written `len(slug) >= SLUG_LENGTH` or `> 63`: the
    refusal below would still pass, and the cap would silently be 63 - one
    character narrower than the `policy_documents.slug` column the constant
    IS, so a corpus file a walk ingests happily could never be re-ingested
    from the admin page. Asserted by a SUCCESS that reaches both stores, not
    by "no exception": a 64-character slug that was accepted and then
    truncated on the way into the column would answer here as a row this case
    could not find again.
    """
    document_slug = _sixty_four_character_slug()
    assert len(document_slug) == SLUG_LENGTH
    assert _plant(_corpus(tmp_path, TENANT_A_SLUG, "v1", document_slug), SOURCE_ONE)

    async def work(
        factory: async_sessionmaker[AsyncSession], client: AsyncQdrantClient
    ) -> tuple[bool, int, tuple[int, int, int]]:
        tenant_id = await _tenant_id(factory, TENANT_A_SLUG)
        async with tenant_session(factory, tenant_id) as session:
            result = await ingest_one_document(
                session,
                client,
                corpus_root=tmp_path,
                tenant_slug=TENANT_A_SLUG,
                slug=document_slug,
                version="v1",
                redactor=FakeRedactor(),
                embedder=FakeEmbedder(),
                ingested_at=INGESTED_AT,
            )
        counts = await _counts(factory, client, tenant_id, TENANT_A_SLUG, document_slug)
        return result.created, result.chunk_count, counts

    created, chunk_count, counts = _drive(policy_db_url, work)

    assert created is True
    assert chunk_count > 0
    assert counts[0] == 1
    assert counts[1] == chunk_count


def test_a_sixty_five_character_slug_is_refused(
    policy_db_url: str,  # noqa: F811
    tmp_path: Path,
) -> None:
    """The exclusive half of the same boundary, one character out.

    Catches a pre-check written `> SLUG_LENGTH + 1`, or no pre-check at all:
    without it the 65th character reaches the `varchar(64)` column and the
    ingest fails half-way through a registering transaction with a database
    error, which crosses as `INTERNAL` rather than as the `VALIDATION` a
    caller can act on. The corpus file EXISTS at the over-long name, so the
    missing-file guard cannot answer this row and only the length check can.
    """
    document_slug = _sixty_four_character_slug() + "a"
    assert len(document_slug) == SLUG_LENGTH + 1
    assert _plant(_corpus(tmp_path, TENANT_A_SLUG, "v1", document_slug), SOURCE_ONE)

    _refuse_one(
        policy_db_url,
        corpus_root=tmp_path,
        slug=document_slug,
        version="v1",
        error=InvalidPolicyNameError,
        code=ErrorCode.VALIDATION,
    )


@pytest.mark.parametrize("slug", TRAVERSING_SLUGS, ids=["dotdot", "separator", "escape", "newline"])
def test_a_traversing_slug_is_refused_before_the_filesystem_is_touched(
    policy_db_url: str,  # noqa: F811
    tmp_path: Path,
    slug: str,
) -> None:
    """The plan's second risk: the slug is the one argument that shapes a
    filesystem PATH, and `check_slug` is what keeps it inside the corpus.

    KILL: deleting the `check_slug` call from `ingest_one_document`. Every row
    plants a REAL file at exactly the path an unchecked composition would
    reach - `a/b` under the version directory, `passwd.md` two levels above
    the corpus root, the dotted and newlined names in the version directory
    itself - so without the check these calls SUCCEED and register a document
    whose source is a file outside the tenant's corpus. With it, each is
    `InvalidPolicyNameError` / `VALIDATION`, and the filesystem, the gateway,
    the embedder and the vector store are all asserted untouched: "refused" and
    "refused before any effect" are different claims.

    A row whose decoy this filesystem refuses to name still asserts the
    refusal; what it loses is only the escape it would otherwise demonstrate,
    which is why `_plant` reports rather than raises.
    """
    (tmp_path / TENANT_A_SLUG / "v1").mkdir(parents=True)
    _plant(tmp_path / TENANT_A_SLUG / "v1" / f"{slug}.md", DECOY_SOURCE)

    _refuse_one(
        policy_db_url,
        corpus_root=tmp_path,
        slug=slug,
        version="v1",
        error=InvalidPolicyNameError,
        code=ErrorCode.VALIDATION,
    )


@pytest.mark.parametrize("version", REFUSED_VERSIONS, ids=["v0", "dotted", "newline"])
def test_a_version_that_is_not_vn_is_refused(
    policy_db_url: str,  # noqa: F811
    tmp_path: Path,
    version: str,
) -> None:
    """a project rule: `VERSION_PATTERN` is the narrower gate, and all three
    of these fit `VERSION_LENGTH` (8) comfortably.

    KILL: deleting the `check_version` call. Each row plants the version
    directory AND the file inside it, so the path resolves and the
    missing-file guard cannot answer - without the pattern check every one of
    these would be ingested and registered under a version string the Qdrant
    payload filter can never match, which is a document that is stored, is
    `is_current`, and can never be retrieved. `v0` is the row the pattern's
    `[1-9]` lead decides and a `v\\d+` spelling would admit; `1.0` is 14a
    decision G's front-matter spelling; `v1\\n` is a project rule's trailing
    character, which `re.match` would accept and `re.fullmatch` refuses.
    """
    document_slug = _namespace_slug()
    _plant(_corpus(tmp_path, TENANT_A_SLUG, version, document_slug), SOURCE_ONE)

    _refuse_one(
        policy_db_url,
        corpus_root=tmp_path,
        slug=document_slug,
        version=version,
        error=InvalidPolicyNameError,
        code=ErrorCode.VALIDATION,
    )


def test_re_ingesting_one_unchanged_document_writes_nothing_to_either_store(
    policy_db_url: str,  # noqa: F811
    pipeline_collection: str,
    tmp_path: Path,
) -> None:
    """Criterion 8, on BOTH stores and on the store's CALLS.

    Catches a tool that re-registers an unchanged source (a second
    `policy_documents` row and a second set of chunk rows for one file, so the
    board grows a version per click) and one that upserts the points again
    "just in case": deterministic point ids keep Qdrant's COUNT flat either
    way, so only the call record can see the second write, which is why the
    re-run is driven through the recording stand-in.

    The first ingest is real and is the premise: a case where nothing was
    written the first time would pass this assertion with the short-circuit
    deleted.
    """
    document_slug = _namespace_slug()
    assert _plant(_corpus(tmp_path, TENANT_A_SLUG, "v1", document_slug), SOURCE_ONE)
    recording = RecordingClient()

    async def work(
        factory: async_sessionmaker[AsyncSession], client: AsyncQdrantClient
    ) -> tuple[tuple[int, int, int], tuple[int, int, int], bool, int]:
        tenant_id = await _tenant_id(factory, TENANT_A_SLUG)
        async with tenant_session(factory, tenant_id) as session:
            first = await ingest_one_document(
                session,
                client,
                corpus_root=tmp_path,
                tenant_slug=TENANT_A_SLUG,
                slug=document_slug,
                version="v1",
                redactor=FakeRedactor(),
                embedder=FakeEmbedder(),
                ingested_at=INGESTED_AT,
            )
        assert first.created is True, "premise: the first ingest must actually write"
        before = await _counts(factory, client, tenant_id, TENANT_A_SLUG, document_slug)

        async with tenant_session(factory, tenant_id) as session:
            again = await ingest_one_document(
                session,
                _as_client(recording),
                corpus_root=tmp_path,
                tenant_slug=TENANT_A_SLUG,
                slug=document_slug,
                version="v1",
                redactor=FakeRedactor(),
                embedder=FakeEmbedder(),
                ingested_at=INGESTED_AGAIN,
            )
        after = await _counts(factory, client, tenant_id, TENANT_A_SLUG, document_slug)
        return before, after, again.created, again.chunk_count

    before, after, created, chunk_count = _drive(policy_db_url, work)

    assert created is False
    assert chunk_count == 0
    assert before == after
    assert before[0] == 1
    assert before[1] > 0
    assert recording.calls == [], "an unchanged source reached the vector store"


def test_re_ingesting_a_changed_source_makes_the_new_row_current(
    policy_db_url: str,  # noqa: F811
    pipeline_collection: str,
    tmp_path: Path,
) -> None:
    """Criterion 9 and decision E: re-ingesting an OLDER version makes it live
    again, through the pipeline's own `supersede_points` and
    `register_document_version`.

    Two versions are ingested in order, so `v2` is current before the call
    under test; then `v1`'s file is CHANGED on disk and re-ingested. Catches a
    tool that answers `created=true` while leaving `is_current` on the version
    it superseded (the board would then show two live versions and every
    retrieval filter would match the stale one), and one that skips the
    supersede entirely. The chunk count is asserted positive and equal to the
    rows written, so a `created=true` carrying the unchanged path's `0` fails
    here too.

    No single-version case can see this: with one version in the tenant the
    superseded row does not exist, and `is_current` is true either way.
    """
    document_slug = _namespace_slug()
    assert _plant(_corpus(tmp_path, TENANT_A_SLUG, "v1", document_slug), SOURCE_ONE)
    assert _plant(_corpus(tmp_path, TENANT_A_SLUG, "v2", document_slug), SOURCE_V1)

    async def work(
        factory: async_sessionmaker[AsyncSession], client: AsyncQdrantClient
    ) -> tuple[bool, int, list[tuple[str, bool, datetime]], int]:
        tenant_id = await _tenant_id(factory, TENANT_A_SLUG)
        for version, when in (("v1", INGESTED_AT), ("v2", INGESTED_AGAIN)):
            async with tenant_session(factory, tenant_id) as session:
                planted = await ingest_one_document(
                    session,
                    client,
                    corpus_root=tmp_path,
                    tenant_slug=TENANT_A_SLUG,
                    slug=document_slug,
                    version=version,
                    redactor=FakeRedactor(),
                    embedder=FakeEmbedder(),
                    ingested_at=when,
                )
            assert planted.created is True, f"premise: {version} must land"
        current_before = [
            row for row in await _document_rows(factory, tenant_id, document_slug) if row[1]
        ]
        assert current_before == [("v2", True, INGESTED_AGAIN)], current_before

        _corpus(tmp_path, TENANT_A_SLUG, "v1", document_slug).write_bytes(SOURCE_ONE_CHANGED)
        async with tenant_session(factory, tenant_id) as session:
            result = await ingest_one_document(
                session,
                client,
                corpus_root=tmp_path,
                tenant_slug=TENANT_A_SLUG,
                slug=document_slug,
                version="v1",
                redactor=FakeRedactor(),
                embedder=FakeEmbedder(),
                ingested_at=INGESTED_LAST,
            )
        rows = await _document_rows(factory, tenant_id, document_slug)
        chunks = len(await _chunk_texts(factory, tenant_id, result.document.id))
        return result.created, result.chunk_count, rows, chunks

    created, chunk_count, rows, chunks = _drive(policy_db_url, work)

    assert created is True
    assert chunk_count > 0
    assert chunk_count == chunks
    assert [(version, is_current) for version, is_current, _ in rows] == [
        ("v1", False),
        ("v2", False),
        ("v1", True),
    ]
