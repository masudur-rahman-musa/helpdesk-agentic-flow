"""An earlier change §6: the two the design policy tables and their tenant isolation.

Expected implementation surface (the implementer must provide exactly this):

``helpdesk.models.policy`` (re-exported from ``helpdesk.models``)
    ``SHA256_LENGTH = 64`` — the hex digest width of `policy_documents.sha256`.
    ``class PolicyDocument(TenantScoped)`` — ``__tablename__ =
        "policy_documents"``; columns ``slug`` (`String(SLUG_LENGTH)`),
        ``title`` (`String(TITLE_LENGTH)`), ``version``
        (`String(VERSION_LENGTH)`), ``is_current`` (`bool`, NOT NULL),
        ``source_path`` (`String(SOURCE_PATH_LENGTH)`), ``sha256``
        (`String(SHA256_LENGTH)`), ``ingested_at`` (timezone-aware datetime).
    ``class PolicyChunk(TenantScoped)`` — ``__tablename__ = "policy_chunks"``;
        columns ``document_id`` (FK to ``policy_documents.id``),
        ``chunk_index`` (int), ``heading_path`` (`ARRAY(Text)`, decision C),
        ``char_start`` / ``char_end`` (int), ``qdrant_point_id``
        (nullable until 14c writes the point), ``text``.

The four `String` widths come from ``helpdesk.rag.naming``'s constants, not
from repeated literals, so a widened check and its column cannot drift apart
(the an earlier change rule). Pinned by
``test_the_document_column_widths_are_the_naming_constants``.

a project rule: this plan EXTENDS the hand-kept `TENANT_SCOPED_TABLES`
allowlist that `test_models.py` reads as the schema literal, so
``test_both_tables_are_in_the_tenant_scoped_allowlist`` fails until
`_seed.py` names both tables — that is the obligation, made mechanical.

a project rule: one session-scoped database for the whole run
(`policy_db_url`, shared with `test_rag_store.py`, which imports it rather
than building a second). Per-test isolation is a namespace: every case
plants documents under its own uuid-suffixed slug.
"""

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import String
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.sqltypes import Text

from helpdesk.shared.core.db import create_engine, create_session_factory, tenant_session
from helpdesk.shared.crud import Repository
from helpdesk.shared.crud.tenants import find_tenant_by_slug
from helpdesk.shared.models import Base, PolicyChunk, PolicyDocument, Tenant
from helpdesk.shared.models.policy import SHA256_LENGTH
from helpdesk.shared.services.policy_docs.naming import (
    SLUG_LENGTH,
    SOURCE_PATH_LENGTH,
    TITLE_LENGTH,
    VERSION_LENGTH,
)
from tests.conftest import TempDatabaseFactory
from tests.support.seed import TENANT_SCOPED_TABLES, seed_tenant

TENANT_A_SLUG = "policy-tenant-a"
TENANT_B_SLUG = "policy-tenant-b"

# The real (suffixed) slugs `seed_tenant` wrote, filled by the `policy_db_url`
# fixture and read by the by-slug lookup.
_SEEDED_SLUGS: dict[str, str] = {}


INGESTED_AT = datetime(2026, 5, 4, 9, 30, tzinfo=UTC)
FAKE_SHA = "0" * SHA256_LENGTH


async def _seed_with_label(factory: async_sessionmaker[AsyncSession], label: str) -> str:
    """Seed one tenant and give it back its plain label as the slug.

    `seed_tenant` adds a uuid ending so tenants on the shared database never
    collide. This database is private to `policy_db_url`, so the
    ending is not needed here, and the rag tests that share this fixture pass
    the label as `tenant_slug`, which the store checks against the stored slug.
    """
    seeded = await seed_tenant(factory, label)
    async with factory() as session:
        tenant = await session.get(Tenant, seeded.tenant_id)
        assert tenant is not None, label
        tenant.slug = label
        await session.commit()
    return label


@pytest.fixture(scope="session")
def policy_db_url(pg: TempDatabaseFactory) -> Iterator[str]:
    """One database, created and schema-built exactly once.

    Two obviously fake tenants are seeded so the isolation case has a real
    second tenant to be invisible from; the engine is per-`asyncio.run`
    because each test drives its own event loop, which is what the rest of
    this suite does.
    """
    url = pg.create()

    async def prepare() -> None:
        engine = create_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            factory = create_session_factory(engine)
            # An earlier change: `seed_tenant` now suffixes the globally-unique tenant
            # slug, so the real slugs are read back here rather than assumed to
            # equal the labels, for the by-slug lookup below.
            _SEEDED_SLUGS["a"] = await _seed_with_label(factory, TENANT_A_SLUG)
            _SEEDED_SLUGS["b"] = await _seed_with_label(factory, TENANT_B_SLUG)
        finally:
            await engine.dispose()

    asyncio.run(prepare())
    yield url
    pg.drop(url)


# --- metadata shape (no database needed) --------------------------------------


def test_both_policy_tables_are_in_the_metadata() -> None:
    """Catches models declared but never imported into `helpdesk.models`, so
    `Base.metadata.create_all` and the Alembic autogenerate comparison both
    miss the two tables entirely."""
    assert "policy_documents" in Base.metadata.tables
    assert "policy_chunks" in Base.metadata.tables
    assert PolicyDocument.__tablename__ == "policy_documents"
    assert PolicyChunk.__tablename__ == "policy_chunks"


def test_both_tables_are_in_the_tenant_scoped_allowlist() -> None:
    """Catches the plan §5 risk directly: models added without extending
    `_seed.py::TENANT_SCOPED_TABLES`, which leaves `test_models.py`'s
    schema-literal sweep red for a later plan to discover (standing rule
    27)."""
    assert "policy_documents" in TENANT_SCOPED_TABLES
    assert "policy_chunks" in TENANT_SCOPED_TABLES


def test_the_document_columns_are_the_spec_6_columns() -> None:
    """Catches a `policy_documents` table missing any the design column — most
    of all `is_current`, without which no version is distinguishable, and
    `sha256`, without which re-ingestion cannot be idempotent."""
    columns = Base.metadata.tables["policy_documents"].c
    for name in ("tenant_id", "slug", "title", "version", "is_current", "source_path", "sha256"):
        assert name in columns, name
        assert not columns[name].nullable, name
    assert "ingested_at" in columns


def test_the_document_column_widths_are_the_naming_constants() -> None:
    """Catches a column sized with its own literal instead of the constant
    the pre-check reads: widening one and not the other is exactly how an
    accepted value becomes a post-write truncation error."""
    columns = Base.metadata.tables["policy_documents"].c
    for name, expected in (
        ("slug", SLUG_LENGTH),
        ("title", TITLE_LENGTH),
        ("version", VERSION_LENGTH),
        ("source_path", SOURCE_PATH_LENGTH),
        ("sha256", SHA256_LENGTH),
    ):
        column_type = columns[name].type
        assert isinstance(column_type, String), name
        assert column_type.length == expected, name


def test_the_chunk_columns_are_the_spec_6_columns() -> None:
    """Catches a `policy_chunks` table missing the offsets or the ordering
    key, which would leave a retrieved chunk unable to point back at the
    span of source it came from."""
    columns = Base.metadata.tables["policy_chunks"].c
    for name in (
        "tenant_id",
        "document_id",
        "chunk_index",
        "heading_path",
        "char_start",
        "char_end",
        "text",
    ):
        assert name in columns, name
    assert "qdrant_point_id" in columns


def test_the_chunk_document_id_is_a_foreign_key_to_policy_documents() -> None:
    """Catches a `document_id` declared as a bare UUID column: chunks would
    then survive their document's deletion and point at nothing."""
    column = Base.metadata.tables["policy_chunks"].c.document_id
    targets = {fk.column.table.name for fk in column.foreign_keys}
    assert targets == {"policy_documents"}
    assert {fk.column.name for fk in column.foreign_keys} == {"id"}


def test_heading_path_is_a_text_array() -> None:
    """Catches decision C being implemented as a joined string or as `jsonb`
    — both of which need a separator or a decode step the Qdrant payload
    does not, and a separator no heading may contain does not exist."""
    column_type = Base.metadata.tables["policy_chunks"].c.heading_path.type
    assert isinstance(column_type, ARRAY)
    assert isinstance(column_type.item_type, Text | String)


# --- tenant isolation through the guard ---------------------------------------


def test_another_tenants_documents_and_chunks_are_invisible(policy_db_url: str) -> None:
    """Catches a model that does not inherit `TenantScoped`: the per-model
    ORM guard in `core/db.py` keys on that base, so a plain `Base` subclass
    with a `tenant_id` column reads every tenant's policy corpus from any
    tenant's session. Both tenants exist and both are asked, so a guard that
    returned nothing at all would fail the same case."""
    namespace = uuid.uuid4().hex[:12]

    async def scenario() -> None:
        engine = create_engine(policy_db_url)
        try:
            factory = create_session_factory(engine)

            async with factory() as session:
                row_a = await find_tenant_by_slug(session, _SEEDED_SLUGS["a"])
                row_b = await find_tenant_by_slug(session, _SEEDED_SLUGS["b"])
            assert row_a is not None and row_b is not None
            tenant_a, tenant_b = row_a.id, row_b.id
            assert tenant_a != tenant_b

            async with tenant_session(factory, tenant_a) as session:
                document = await Repository(session, PolicyDocument).create(
                    slug=f"doc-a-{namespace}",
                    title="Fake Access Control Policy",
                    version="v1",
                    is_current=True,
                    source_path=f"policies/{TENANT_A_SLUG}/v1/doc-a-{namespace}.md",
                    sha256=FAKE_SHA,
                    ingested_at=INGESTED_AT,
                )
                await Repository(session, PolicyChunk).create(
                    document_id=document.id,
                    chunk_index=0,
                    heading_path=["Fake Access Control Policy", "1. Purpose"],
                    char_start=0,
                    char_end=42,
                    qdrant_point_id=None,
                    text="fake chunk text",
                )

            async with tenant_session(factory, tenant_a) as session:
                own_documents = await Repository(session, PolicyDocument).list()
                own_chunks = await Repository(session, PolicyChunk).list()
                assert [row.slug for row in own_documents] == [f"doc-a-{namespace}"]
                assert len(own_chunks) == 1
                assert own_chunks[0].heading_path == [
                    "Fake Access Control Policy",
                    "1. Purpose",
                ]

            async with tenant_session(factory, tenant_b) as session:
                other_documents = await Repository(session, PolicyDocument).list()
                other_chunks = await Repository(session, PolicyChunk).list()
                assert f"doc-a-{namespace}" not in {row.slug for row in other_documents}
                assert document.id not in {row.document_id for row in other_chunks}
                assert await Repository(session, PolicyDocument).get(document.id) is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())
