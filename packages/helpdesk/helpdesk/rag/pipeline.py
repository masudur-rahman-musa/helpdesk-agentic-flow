"""Ingestion, composed: one corpus document into both stores (the design).

This is the only module that knows the ORDER the ingest effects run in, and
it is deliberately the thinnest one in the package: every step is a call into
a module that owns it, so the order is readable in one screen and a test can
wrap each name (`test_rag_pipeline.py::test_the_effects_run_in_the_order_section_one_states`
asserts the whole sequence as one list).

The order, and why each step is where it is:

1. `redact_document` — the WHOLE document, before anything is split, stored or
   embedded (14b decision G). Splitting first would mean the raw text reached
   a chunk row, which is the leak the gateway exists to prevent; pinned by
   `test_rag_pipeline.py::test_the_chunks_are_cut_from_the_redacted_text`.
2. `split_sections` and 3. `chunk_document` — pure, over the redacted text.
4. `register_document_version` — 14a's sha256 short-circuit answers "is this
   source already registered", and the row it returns is what gives the chunks
   and the points a `document_id` at all. See DEVIATION below.
5. `ensure_collection` and 6. `upsert_chunks` — Qdrant before the chunk ROWS
   (decision C).
7. `write_chunks` — the rows, carrying the ids step 6 returned.
8. `supersede_points` — the other versions' points lose `is_current` last, so
   the tenant is never left with no current version mid-transaction.

**DEVIATION from an earlier change §1's stated order.** §1 puts
`register_document_version` after `upsert_chunks`, and that is not
implementable beside the other two things §1 fixes: the point payload carries
`document_id` and the point id is derived from it (decision B), and neither
exists until the `policy_documents` row does. Registering first costs nothing
that decision C buys — the two Postgres writes and the upsert share the
caller's ONE transaction, so a failing upsert rolls the document row back with
the chunk rows, pinned by
`test_rag_pipeline.py::test_a_failing_upsert_leaves_no_document_and_no_chunk_row`,
which asserts zero documents as well as zero chunks. What decision C actually
forbids — a chunk ROW without its point — still cannot happen.

**Everything runs in the caller's transaction.** Nothing here commits: the
caller's `tenant_session` does, so a document lands whole or not at all.
`ingest_version` opens one transaction per document, which is what lets a
walk stop on the first refusal without leaving that document half-written.

**The redactor and the embedder are protocols** (decision E), constructed by
`scripts/ingest.py` and by nothing else, so this suite runs against fakes with
no gateway reachable and no model loaded.

**Re-ingest cost.** An unchanged source is recognised at step 4, after it has
already been redacted and embedded: §1 fixes redaction as the first effect, so
the gateway call and the model pass are paid per file per run even when
nothing is written. The idempotence the design asks for is about EFFECTS,
and no row and no point is written; making it cheap as well would mean reading
the digest before redacting, which is a different order than the one this plan
fixed.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from qdrant_client import AsyncQdrantClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from helpdesk.core.db import tenant_session
from helpdesk.errors import ErrorCode, ToolError
from helpdesk.models import PolicyDocument
from helpdesk.rag.chunking import chunk_document
from helpdesk.rag.chunks import write_chunks
from helpdesk.rag.embedding import Embedder
from helpdesk.rag.gateway import RedactedDocument
from helpdesk.rag.markdown import split_sections
from helpdesk.rag.naming import check_slug, check_version
from helpdesk.rag.store import register_document_version
from helpdesk.rag.vectors import ensure_collection, supersede_points, upsert_chunks

MARKDOWN_SUFFIX = ".md"
"""The only files a version directory is walked for.

A version directory holds notes and drafts beside its policies; ingesting a
`.txt` sibling would put text nobody wrote as policy behind a citation.
Pinned by
`test_rag_pipeline.py::test_ingest_version_ingests_every_markdown_file_in_the_directory`,
whose fixture holds two `.md` files and one `.txt`.
"""


class Redactor(Protocol):
    """What ingestion needs from the redaction gateway, and nothing more.

    Satisfied by 14b's `rag.gateway.RedactionClient`. A protocol rather than
    that class (decision E, the same seam as `Embedder`): the pipeline suite
    has no gateway to reach, and the one place that builds the real client is
    `scripts/ingest.py`.
    """

    async def redact_document(self, text: str, tenant_slug: str) -> RedactedDocument: ...


class CorpusDirectoryNotFoundError(ToolError):
    """The tenant or version directory this walk was pointed at does not exist.

    `VALIDATION`, and raised before any effect: a typo'd tenant slug that was
    walked as an empty directory would report "0 documents ingested", exit 0,
    and leave a tenant with no policy corpus at all — after which every answer
    refuses for lack of evidence and nothing says why. Pinned, with the vector
    store and the gateway both asserted untouched, by
    `test_rag_pipeline.py::test_a_tenant_with_no_corpus_directory_is_refused_before_any_effect`.
    """

    code = ErrorCode.VALIDATION


@dataclass(frozen=True)
class IngestResult:
    """What one document's ingest did.

    `created` is False when 14a's sha256 short-circuit recognised the source,
    in which case `chunk_count` is 0 because nothing was written — not because
    the document has no chunks.
    """

    document: PolicyDocument
    created: bool
    chunk_count: int


async def ingest_document(
    session: AsyncSession,
    client: AsyncQdrantClient,
    *,
    tenant_slug: str,
    slug: str,
    title: str,
    version: str,
    source_path: str,
    source: bytes,
    redactor: Redactor,
    embedder: Embedder,
    ingested_at: datetime,
) -> IngestResult:
    """Ingest one version of one document into Qdrant and Postgres.

    The eight effects run in the order this module's docstring lists, in the
    caller's transaction. An unchanged source stops after step 4 and writes
    nothing to either store, pinned on BOTH counts by
    `test_rag_pipeline.py::test_re_ingesting_an_unchanged_source_writes_nothing_to_either_store`
    — Postgres alone would not catch it, because deterministic point ids keep
    Qdrant's count flat whether the short-circuit fires or not.

    A source changed under the same version directory becomes a second
    `policy_documents` row (14a), so its chunks are written against that new
    row and the old row keeps its own spans for audit; pinned by
    `::test_re_ingesting_a_changed_source_replaces_the_rows_rather_than_appending`.
    """
    redacted = await redactor.redact_document(source.decode("utf-8"), tenant_slug)
    sections = split_sections(redacted.text)
    chunks = chunk_document(sections, embedder)

    registration = await register_document_version(
        session,
        tenant_slug=tenant_slug,
        slug=slug,
        title=title,
        version=version,
        source_path=source_path,
        source=source,
        ingested_at=ingested_at,
    )
    if not registration.created:
        return IngestResult(document=registration.document, created=False, chunk_count=0)

    document = registration.document
    await ensure_collection(client, tenant_slug)
    point_ids = await upsert_chunks(
        client,
        tenant_slug,
        tenant_id=document.tenant_id,
        document_id=document.id,
        document_slug=slug,
        version=version,
        chunks=chunks,
    )
    await write_chunks(session, document_id=document.id, chunks=chunks, point_ids=point_ids)
    await supersede_points(client, tenant_slug, document_slug=slug, keep_document_id=document.id)
    return IngestResult(document=document, created=True, chunk_count=len(chunks))


async def ingest_version(
    factory: async_sessionmaker[AsyncSession],
    client: AsyncQdrantClient,
    *,
    tenant_id: uuid.UUID,
    corpus_root: Path,
    tenant_slug: str,
    version: str,
    redactor: Redactor,
    embedder: Embedder,
    ingested_at: datetime,
) -> list[IngestResult]:
    """Ingest every markdown file of one version directory, in name order.

    One transaction per document, not one for the walk: a refusal on the
    fourth file leaves the first three ingested and the fourth untouched,
    which is what lets `scripts/ingest.py` exit non-zero without leaving a
    document half-committed.

    Both names are checked before the filesystem is touched, so a malformed
    version (`1.0`, the front-matter spelling 14a decision G rejects) is a
    typed refusal rather than rows carrying a version string no payload filter
    can match. Pinned, with the directory present so only the name check can
    decide it, by
    `test_rag_pipeline.py::test_a_version_directory_that_is_not_vn_is_refused_before_any_effect`.

    `ingested_at` is one instant for the whole run, passed down rather than
    read per file, so every document of one walk carries the same time.
    """
    check_slug(tenant_slug)
    check_version(version)

    directory = corpus_root / tenant_slug / version
    if not directory.is_dir():
        raise CorpusDirectoryNotFoundError(
            f"no corpus directory for tenant {tenant_slug!r} version {version!r}"
        )

    results: list[IngestResult] = []
    for path in sorted(directory.glob(f"*{MARKDOWN_SUFFIX}")):
        source = path.read_bytes()
        async with tenant_session(factory, tenant_id) as session:
            results.append(
                await ingest_document(
                    session,
                    client,
                    tenant_slug=tenant_slug,
                    slug=path.stem,
                    title=_title_of(source, fallback=path.stem),
                    version=version,
                    source_path=f"{tenant_slug}/{version}/{path.name}",
                    source=source,
                    redactor=redactor,
                    embedder=embedder,
                    ingested_at=ingested_at,
                )
            )
    return results


def _title_of(source: bytes, *, fallback: str) -> str:
    """The document's first heading, or the file's own name.

    Read off the source rather than configured: the design's title is what the
    document calls itself, and a corpus file that opens with no heading is
    still ingestable under its slug rather than refused for a missing field.
    """
    for section in split_sections(source.decode("utf-8")):
        if section.heading_path:
            return section.heading_path[0]
    return fallback
