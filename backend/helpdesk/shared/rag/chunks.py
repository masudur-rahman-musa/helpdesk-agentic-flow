"""The `policy_chunks` half of ingestion (the design, §12.1.3).

`vectors.py` writes the points; this module writes the rows that reference
them. The two are written together by `pipeline.py`, points first, in the
caller's one transaction (decision C), which is what makes 14a decision F's
rule — a chunk row never exists without its point — hold under failure.

Every write goes through `core.repository.Repository`, so `tenant_id` is
stamped from the session the caller bound and is never an argument: hard rule
6, pinned by
`test_rag_chunks.py::test_another_tenant_cannot_see_the_written_chunks`, which
asks both seeded tenants so a guard that simply returned nothing fails its
first half rather than passing.

Nothing here commits. `write_chunks` and `replace_chunks` run inside the
caller's transaction, so the rows and the `policy_documents` row they hang off
land together or not at all.

The one `select` below is against this package's own table, for the reason
`store.py` gives: the package that owns a table owns the statements against
it, and no service builds one.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.core.repository import Repository
from helpdesk.shared.errors import ErrorCode, ToolError
from helpdesk.shared.models import PolicyChunk
from helpdesk.shared.rag.chunking import Chunk


class ChunkPointMismatchError(ToolError):
    """`chunks` and `point_ids` are not the same length.

    Raised before any write, so a mismatch never half-lands. The alternative
    is what a bare `zip` does: silently drop the last chunk, leaving a
    document whose final span has no row at all, or pair a row with the point
    id of a different chunk — a citation that resolves to the wrong text while
    every row still looks well-formed. `VALIDATION`, not `INTERNAL`: the two
    arguments the caller passed disagree with each other. Pinned, against a
    populated table rather than an empty one, by
    `test_rag_chunks.py::test_a_point_id_count_that_does_not_match_the_chunks_writes_nothing`.
    """

    code = ErrorCode.VALIDATION


async def write_chunks(
    session: AsyncSession,
    *,
    document_id: uuid.UUID,
    chunks: Sequence[Chunk],
    point_ids: Sequence[uuid.UUID],
) -> None:
    """Write one `policy_chunks` row per chunk, in chunk order.

    `point_ids[i]` is the id of `chunks[i]`'s point, as `upsert_chunks`
    returned them, so `qdrant_point_id` is never NULL on a row this writes —
    14a decision F's whole point, pinned by
    `test_rag_chunks.py::test_every_written_row_carries_its_point_id_and_its_offsets`.

    An empty `chunks` writes nothing rather than a placeholder row: a corpus
    file of headings only chunks to nothing, and a row with no text embeds to
    a vector that sits near every query. Pinned by
    `test_rag_chunks.py::test_an_empty_chunk_list_writes_no_rows`.
    """
    if len(chunks) != len(point_ids):
        raise ChunkPointMismatchError(f"{len(chunks)} chunks were given {len(point_ids)} point ids")

    repository = Repository(session, PolicyChunk)
    for index, chunk in enumerate(chunks):
        await repository.create(
            document_id=document_id,
            chunk_index=index,
            heading_path=list(chunk.heading_path),
            char_start=chunk.char_start,
            char_end=chunk.char_end,
            qdrant_point_id=point_ids[index],
            text=chunk.text,
        )


async def replace_chunks(
    session: AsyncSession,
    *,
    document_id: uuid.UUID,
    chunks: Sequence[Chunk],
    point_ids: Sequence[uuid.UUID],
) -> None:
    """Make this document version's rows exactly `chunks`, deleting the rest.

    Scoped to ONE `policy_documents` row, never to the tenant and never to the
    document slug: another policy of the same tenant keeps its rows, pinned by
    `test_rag_chunks.py::test_replacing_one_document_leaves_another_documents_chunks_alone`,
    and an older version keeps its own, pinned by
    `::test_a_second_version_of_one_document_keeps_the_first_versions_chunks`.
    the design retains superseded spans, so a wider delete would take an
    audited answer's evidence with it, and the next ingest would not restore
    it — those documents are unchanged and get skipped.

    The length pre-check runs first, so a mismatched call does not delete the
    old rows before refusing to write the new ones.
    """
    if len(chunks) != len(point_ids):
        raise ChunkPointMismatchError(f"{len(chunks)} chunks were given {len(point_ids)} point ids")

    repository = Repository(session, PolicyChunk)
    existing = await session.execute(
        select(PolicyChunk.id).where(PolicyChunk.document_id == document_id)
    )
    for row_id in existing.scalars().all():
        await repository.delete(row_id)

    await write_chunks(session, document_id=document_id, chunks=chunks, point_ids=point_ids)
