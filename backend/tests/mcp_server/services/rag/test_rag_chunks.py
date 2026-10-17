"""The `policy_chunks` half, in `helpdesk/rag/chunks.py`.

Expected implementation surface (the implementer must provide exactly this):

``helpdesk.rag.chunks``
    ``async def write_chunks(session, *, document_id: uuid.UUID,
        chunks: Sequence[Chunk], point_ids: Sequence[uuid.UUID]) -> None`` —
        one ``policy_chunks`` row per chunk, in chunk order, written through
        ``core.repository.Repository`` (never ``session.execute``), carrying
        ``qdrant_point_id``, ``chunk_index``, ``heading_path``, ``char_start``,
        ``char_end`` and ``text``. The tenant is stamped by the session
; it is never an argument.
    ``async def replace_chunks(session, *, document_id, chunks, point_ids)
        -> None`` — the rows of that document version become exactly the new
        set, rather than a second set appended beside the old one.

    Both refuse a typed ``VALIDATION`` before any write when `chunks` and
    `point_ids` are not the same length: a row whose `qdrant_point_id` came
    from a different chunk is a citation that resolves to the wrong span, and
    the rule "no chunk row without its point" would be false while
    every row still looked well-formed.

The database is `test_policy_models.py`'s session-scoped
`policy_db_url`, imported rather than rebuilt, and the two tenants seeded
there are the two this file uses. Per-test isolation is a namespace: every
case registers its own uuid-suffixed document slug, so no two cases share a
document.

No pytest-asyncio: each case is a sync function driving one `asyncio.run`.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from helpdesk.mcp_server.services.rag.chunking import Chunk
from helpdesk.mcp_server.services.rag.chunks import replace_chunks, write_chunks
from helpdesk.mcp_server.services.rag.embedding import SparseVector
from helpdesk.mcp_server.services.rag.store import register_document_version
from helpdesk.shared.core.db import create_engine, create_session_factory, tenant_session
from helpdesk.shared.core.errors import ErrorCode, ToolError
from helpdesk.shared.crud import Repository
from helpdesk.shared.crud.tenants import find_tenant_by_slug
from helpdesk.shared.models import PolicyChunk

# Imported, not redefined: one database for the whole run.
from tests.shared.models.test_policy_models import (
    TENANT_A_SLUG,
    TENANT_B_SLUG,
    policy_db_url,  # noqa: F401
)

INGESTED_AT = datetime(2026, 5, 4, 9, 30, tzinfo=UTC)
V1_SOURCE = b"# Fake Policy\n\n## 1. Purpose\n\nFake v1 body.\n"
V2_SOURCE = b"# Fake Policy\n\n## 1. Purpose\n\nFake v2 body, rewritten.\n"

MARKER = "Fake marker sentence that only this test writes."


@dataclass(frozen=True)
class Row:
    """The chunk fields a case compares; enough to see any write."""

    chunk_index: int
    heading_path: tuple[str, ...]
    char_start: int
    char_end: int
    qdrant_point_id: uuid.UUID | None
    text: str


def _row(chunk: PolicyChunk) -> Row:
    return Row(
        chunk_index=chunk.chunk_index,
        heading_path=tuple(chunk.heading_path),
        char_start=chunk.char_start,
        char_end=chunk.char_end,
        qdrant_point_id=chunk.qdrant_point_id,
        text=chunk.text,
    )


def _chunk(index: int, text: str, *, heading: str = "1. Purpose") -> Chunk:
    return Chunk(
        heading_path=(heading,),
        char_start=index * 100,
        char_end=index * 100 + len(text),
        text=text,
        dense=(0.1, 0.2, 0.3),
        sparse=SparseVector(indices=(1, 2), values=(0.5, 0.5)),
    )


FIRST_CUT: tuple[Chunk, ...] = (
    _chunk(0, "Fake access is granted by the system owner."),
    _chunk(1, "Fake access is reviewed every quarter."),
)
SECOND_CUT: tuple[Chunk, ...] = (_chunk(0, MARKER, heading="2. Scope"),)


def _point_ids(count: int) -> list[uuid.UUID]:
    return [uuid.uuid4() for _ in range(count)]


def _drive[T](url: str, work: Callable[[async_sessionmaker[AsyncSession]], Awaitable[T]]) -> T:
    """Run one async scenario on its own engine and event loop."""

    async def scenario() -> T:
        engine = create_engine(url)
        try:
            return await work(create_session_factory(engine))
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


async def _tenant_id(factory: async_sessionmaker[AsyncSession], slug: str) -> uuid.UUID:
    async with factory() as session:
        tenant = await find_tenant_by_slug(session, slug)
    assert tenant is not None, slug
    return tenant.id


async def _register(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    tenant_slug: str,
    document_slug: str,
    *,
    source: bytes = V1_SOURCE,
    version: str = "v1",
) -> uuid.UUID:
    """One `policy_documents` row for the chunks to hang off."""
    async with tenant_session(factory, tenant_id) as session:
        result = await register_document_version(
            session,
            tenant_slug=tenant_slug,
            slug=document_slug,
            title="Fake Policy",
            version=version,
            source_path=f"policies/{tenant_slug}/{version}/{document_slug}.md",
            source=source,
            ingested_at=INGESTED_AT,
        )
        return result.document.id


async def _chunk_rows(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    document_id: uuid.UUID | None = None,
) -> list[Row]:
    async with tenant_session(factory, tenant_id) as session:
        rows = [
            _row(row)
            for row in await Repository(session, PolicyChunk).list()
            if document_id is None or row.document_id == document_id
        ]
    return sorted(rows, key=lambda row: (row.text, row.chunk_index))


def _namespace_slug() -> str:
    return f"fake-chunked-{uuid.uuid4().hex[:12]}"


# --- the happy paths ----------------------------------------------------------


def test_every_written_row_carries_its_point_id_and_its_offsets(
    policy_db_url: str,  # noqa: F811
) -> None:
    """Catches a `write_chunks` that leaves `qdrant_point_id` NULL (the
    whole point: a chunk row without its point is a stored
    citation retrieval can never resolve), and one that drops `char_start` /
    `char_end` so the row cannot be located in the document it came from."""
    document_slug = _namespace_slug()
    ids = _point_ids(len(FIRST_CUT))

    async def work(factory: async_sessionmaker[AsyncSession]) -> list[Row]:
        tenant_id = await _tenant_id(factory, TENANT_A_SLUG)
        document_id = await _register(factory, tenant_id, TENANT_A_SLUG, document_slug)
        async with tenant_session(factory, tenant_id) as session:
            await write_chunks(session, document_id=document_id, chunks=FIRST_CUT, point_ids=ids)
        return await _chunk_rows(factory, tenant_id, document_id)

    rows = _drive(policy_db_url, work)

    assert len(rows) == len(FIRST_CUT)
    by_index = {row.chunk_index: row for row in rows}
    assert sorted(by_index) == [0, 1]
    for index, row in by_index.items():
        assert row.qdrant_point_id == ids[index]
        assert row.text == FIRST_CUT[index].text
        assert row.heading_path == FIRST_CUT[index].heading_path
        assert row.char_start == FIRST_CUT[index].char_start
        assert row.char_end == FIRST_CUT[index].char_end


def test_another_tenant_cannot_see_the_written_chunks(policy_db_url: str) -> None:  # noqa: F811
    """Catches a `write_chunks` that bypasses the tenant-scoped repository —
    a raw `session.execute` insert, or a `tenant_id` taken from an argument
    instead of the bound session. Both tenants exist and both
    are asked, so a guard that simply returned nothing would fail the first
    half of this case rather than pass it."""
    document_slug = _namespace_slug()
    ids = _point_ids(len(FIRST_CUT))

    async def work(factory: async_sessionmaker[AsyncSession]) -> tuple[list[Row], list[Row]]:
        tenant_a = await _tenant_id(factory, TENANT_A_SLUG)
        tenant_b = await _tenant_id(factory, TENANT_B_SLUG)
        assert tenant_a != tenant_b
        document_id = await _register(factory, tenant_a, TENANT_A_SLUG, document_slug)
        async with tenant_session(factory, tenant_a) as session:
            await write_chunks(session, document_id=document_id, chunks=FIRST_CUT, point_ids=ids)
        return (
            await _chunk_rows(factory, tenant_a, document_id),
            await _chunk_rows(factory, tenant_b, document_id),
        )

    mine, theirs = _drive(policy_db_url, work)
    assert len(mine) == len(FIRST_CUT)
    assert theirs == []


def test_replacing_a_versions_chunks_leaves_only_the_new_cut(
    policy_db_url: str,  # noqa: F811
) -> None:
    """Catches a `replace_chunks` implemented as a second `write_chunks`: the
    old cut's rows survive beside the new one, so one document version has two
    chunk 0 rows and retrieval can cite a span the current version no longer
    contains."""
    document_slug = _namespace_slug()

    async def work(factory: async_sessionmaker[AsyncSession]) -> list[Row]:
        tenant_id = await _tenant_id(factory, TENANT_A_SLUG)
        document_id = await _register(factory, tenant_id, TENANT_A_SLUG, document_slug)
        async with tenant_session(factory, tenant_id) as session:
            await write_chunks(
                session,
                document_id=document_id,
                chunks=FIRST_CUT,
                point_ids=_point_ids(len(FIRST_CUT)),
            )
        async with tenant_session(factory, tenant_id) as session:
            await replace_chunks(
                session,
                document_id=document_id,
                chunks=SECOND_CUT,
                point_ids=_point_ids(len(SECOND_CUT)),
            )
        return await _chunk_rows(factory, tenant_id, document_id)

    rows = _drive(policy_db_url, work)
    assert [row.text for row in rows] == [MARKER]
    assert [row.chunk_index for row in rows] == [0]


def test_replacing_one_document_leaves_another_documents_chunks_alone(
    policy_db_url: str,  # noqa: F811
) -> None:
    """Catches a `replace_chunks` that deletes by TENANT rather than by
    document version — a delete wide enough to pass the case above while
    quietly emptying every other policy of the same tenant, which the next
    ingest would not restore because those documents are unchanged and get
    skipped."""
    kept_slug = _namespace_slug()
    replaced_slug = _namespace_slug()

    async def work(factory: async_sessionmaker[AsyncSession]) -> list[Row]:
        tenant_id = await _tenant_id(factory, TENANT_A_SLUG)
        kept_id = await _register(factory, tenant_id, TENANT_A_SLUG, kept_slug)
        replaced_id = await _register(factory, tenant_id, TENANT_A_SLUG, replaced_slug)
        for document_id in (kept_id, replaced_id):
            async with tenant_session(factory, tenant_id) as session:
                await write_chunks(
                    session,
                    document_id=document_id,
                    chunks=FIRST_CUT,
                    point_ids=_point_ids(len(FIRST_CUT)),
                )
        async with tenant_session(factory, tenant_id) as session:
            await replace_chunks(
                session,
                document_id=replaced_id,
                chunks=SECOND_CUT,
                point_ids=_point_ids(len(SECOND_CUT)),
            )
        return await _chunk_rows(factory, tenant_id, kept_id)

    rows = _drive(policy_db_url, work)
    assert [row.text for row in rows] == sorted(chunk.text for chunk in FIRST_CUT)


# --- the refusal, proven against a planted row --------------------------------


def test_a_point_id_count_that_does_not_match_the_chunks_writes_nothing(
    policy_db_url: str,  # noqa: F811
) -> None:
    """Catches a `write_chunks` that zips chunks and point ids without
    checking their lengths: `zip` would silently drop the last chunk, leaving
    a document whose final span has no row at all, and `zip(strict=True)`
    would raise mid-transaction rather than as a typed VALIDATION before any
    write. The comparison is against a document that already HAS rows, so
    "unchanged" is two populated snapshots, not two empty ones."""
    planted_slug = _namespace_slug()
    document_slug = _namespace_slug()

    async def work(factory: async_sessionmaker[AsyncSession]) -> tuple[list[Row], list[Row]]:
        tenant_id = await _tenant_id(factory, TENANT_A_SLUG)
        planted_id = await _register(factory, tenant_id, TENANT_A_SLUG, planted_slug)
        async with tenant_session(factory, tenant_id) as session:
            await write_chunks(
                session,
                document_id=planted_id,
                chunks=FIRST_CUT,
                point_ids=_point_ids(len(FIRST_CUT)),
            )
        before = await _chunk_rows(factory, tenant_id)

        document_id = await _register(factory, tenant_id, TENANT_A_SLUG, document_slug)
        with pytest.raises(ToolError) as excinfo:
            async with tenant_session(factory, tenant_id) as session:
                await write_chunks(
                    session,
                    document_id=document_id,
                    chunks=FIRST_CUT,
                    point_ids=_point_ids(len(FIRST_CUT) - 1),
                )
        assert excinfo.value.code is ErrorCode.VALIDATION

        after = await _chunk_rows(factory, tenant_id)
        return before, after

    before, after = _drive(policy_db_url, work)
    assert before == after, "the refused call changed policy_chunks"


def test_an_empty_chunk_list_writes_no_rows(policy_db_url: str) -> None:  # noqa: F811
    """Catches a `write_chunks` that writes a placeholder row for a document
    that chunked to nothing (a corpus file of headings only): a row with no
    text embeds to a vector that sits near every query, which is exactly the
    pollution `chunk_document` refuses to create upstream."""
    document_slug = _namespace_slug()
    empty_chunks: Sequence[Chunk] = ()

    async def work(factory: async_sessionmaker[AsyncSession]) -> list[Row]:
        tenant_id = await _tenant_id(factory, TENANT_A_SLUG)
        document_id = await _register(factory, tenant_id, TENANT_A_SLUG, document_slug)
        async with tenant_session(factory, tenant_id) as session:
            await write_chunks(session, document_id=document_id, chunks=empty_chunks, point_ids=[])
        return await _chunk_rows(factory, tenant_id, document_id)

    assert _drive(policy_db_url, work) == []


def test_a_second_version_of_one_document_keeps_the_first_versions_chunks(
    policy_db_url: str,  # noqa: F811
) -> None:
    """Catches a `write_chunks` that keys its rows on (tenant, document slug)
    instead of on the document VERSION row: the contract retains the
    superseded version's spans for audit, and a write that overwrote them
    would leave an audited answer with nothing behind it."""
    document_slug = _namespace_slug()

    async def work(factory: async_sessionmaker[AsyncSession]) -> tuple[list[Row], list[Row]]:
        tenant_id = await _tenant_id(factory, TENANT_A_SLUG)
        v1_id = await _register(factory, tenant_id, TENANT_A_SLUG, document_slug)
        async with tenant_session(factory, tenant_id) as session:
            await write_chunks(
                session,
                document_id=v1_id,
                chunks=FIRST_CUT,
                point_ids=_point_ids(len(FIRST_CUT)),
            )
        v2_id = await _register(
            factory,
            tenant_id,
            TENANT_A_SLUG,
            document_slug,
            source=V2_SOURCE,
            version="v2",
        )
        async with tenant_session(factory, tenant_id) as session:
            await write_chunks(
                session,
                document_id=v2_id,
                chunks=SECOND_CUT,
                point_ids=_point_ids(len(SECOND_CUT)),
            )
        return (
            await _chunk_rows(factory, tenant_id, v1_id),
            await _chunk_rows(factory, tenant_id, v2_id),
        )

    first, second = _drive(policy_db_url, work)
    assert [row.text for row in first] == sorted(chunk.text for chunk in FIRST_CUT)
    assert [row.text for row in second] == [MARKER]
