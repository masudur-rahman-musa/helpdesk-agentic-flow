"""`read_policy_documents`, against Postgres.

Surface under test (written before it exists). The implementer must provide
exactly this:

``helpdesk.rag.documents`` (new module)
    ``PolicyVersion`` - one ingested version, carrying exactly ``version``,
    ``is_current``, ``ingested_at``, ``chunk_count``, ``source_path`` and
    ``sha256``.
    ``PolicyDocumentDetail`` - one document SLUG, carrying
    exactly ``slug``, ``title`` and ``versions``.
    ``async def read_policy_documents(session) -> tuple[PolicyDocumentDetail, ...]``
    - the bound tenant's corpus, documents ordered by ``slug`` ascending and
    versions by ``ingested_at`` descending, in exactly two
    SELECTs whatever the document count.

``helpdesk.core.repository``
    ``count_chunks_by_document(session, document_ids)`` - the grouped count
    behind ``chunk_count``. Exercised here only through the
    helper: its own caller is the helper and nothing else.

The session is tenant-bound (``helpdesk.core.db.tenant_session``), so the
helper takes no tenant argument at all: the corpus is the token's tenant's own
rows and the guard in ``helpdesk/core/db.py`` is the only filter.

Shapes the plan left open, fixed here and owed to 24g's Deviations:
- the VERSION tie-break direction. The contract fixes ``ingested_at``
  descending "tiebroken by `version`" and states no direction for the second
  column, which a total order is only assertable with. This file fixes it as
  ``version`` DESCENDING - the whole order reads newest-first, and the single
  expression that produces it (``sorted(key=(ingested_at, version),
  reverse=True)``) reverses both columns together. An implementation that
  tie-breaks ascending is red on
  `::test_two_versions_ingested_at_one_instant_are_tiebroken_by_version_descending`
  and the plan's Deviations is where that choice is recorded.
- the CARRIER of both field sets (a frozen slotted dataclass, as
  `SodRuleDetail` is, or a Pydantic model, as `ActiveGrant` is).
  `_field_names` below reads either, so the exact field set is still asserted
  while the choice stays the implementer's.

The engine and session factory are the shared ones from
`conftest.py` - one engine for the whole run, schema created once. Per-test
isolation is a namespace, never a rebuild - every case plants a
tenant of its OWN and its own corpus, so no case can read another's documents.
`_seed.seed_tenant` plants no `policy_documents` row at all, which is why a
bare tenant is the empty-corpus fixture and every expectation below is exactly
what the case planted.

No pytest-asyncio: each case is a sync function driving one `asyncio.run`.
"""

import asyncio
import dataclasses
import hashlib
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from helpdesk.mcp_server.services.rag.documents import (
    PolicyDocumentDetail,
    PolicyVersion,
    read_policy_documents,
)
from helpdesk.shared.core.db import tenant_session
from helpdesk.shared.models import PolicyChunk, PolicyDocument, Tenant

DOCUMENT_FIELDS: tuple[str, ...] = ("slug", "title", "versions")
"""The three fields, written out.

A literal rather than a read of the model under test: this row is what the
tool puts on the wire and 24h renders, so a field added or dropped is a red
case here rather than a page that breaks on the first rename."""

VERSION_FIELDS: tuple[str, ...] = (
    "version",
    "is_current",
    "ingested_at",
    "chunk_count",
    "source_path",
    "sha256",
)
"""The six version fields, written out for the same reason."""

EXPECTED_STATEMENTS = 2
"""The number, exactly: the documents, then one
grouped chunk count over exactly the ids just read - whatever the corpus size."""

# A fixed instant the cases offset from, so every planted `ingested_at` is
# deterministic and two of them can be made exactly equal on purpose.
BASE_INSTANT = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)


def _digest(label: str) -> str:
    """A 64-character hex digest that differs per label.

    Not a secret and not a real document's digest: the column is
    `String(SHA256_LENGTH)` and every case needs distinguishable values, so
    the label is hashed rather than a literal repeated.
    """
    return hashlib.sha256(label.encode()).hexdigest()


def _field_names(value: object) -> tuple[str, ...]:
    """The declared field names of a frozen dataclass or a Pydantic model."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return tuple(field.name for field in dataclasses.fields(value))
    fields = getattr(type(value), "model_fields", None)
    assert fields is not None, f"{type(value)!r} is neither a dataclass nor a Pydantic model"
    return tuple(fields)


async def _tenant(factory: async_sessionmaker[AsyncSession], label: str) -> uuid.UUID:
    """A tenant row and nothing else: the per-case namespace.

    `_seed.seed_tenant` writes no `policy_documents` row, so a bare tenant and
    a fully seeded one carry the same (empty) corpus; the cheaper one is used.
    """
    async with factory() as session:
        tenant = Tenant(
            slug=f"policydocs-{label}-{uuid.uuid4().hex[:10]}",
            name=f"Fake Tenant ({label})",
            residency="eu",
            settings={},
        )
        session.add(tenant)
        await session.commit()
        return tenant.id


async def _plant_version(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    *,
    slug: str,
    title: str,
    version: str,
    is_current: bool,
    ingested_at: datetime,
    chunks: int,
    digest_label: str | None = None,
) -> uuid.UUID:
    """One `policy_documents` row plus `chunks` `policy_chunks` rows.

    Written as model rows rather than through
    `helpdesk.rag.store.register_document_version`, which always writes
    `is_current=True` and flips every other version of the slug: the corpus
    states this read exists to show - a document with NO current version, a
    version with no chunk at all, two versions sharing one instant - are
    exactly the ones the writer cannot produce (the plant is
    named here because the shape is not the writer's).
    """
    label = digest_label if digest_label is not None else f"{slug}-{version}-{title}"
    async with tenant_session(factory, tenant_id) as session:
        document = PolicyDocument(
            tenant_id=tenant_id,
            slug=slug,
            title=title,
            version=version,
            is_current=is_current,
            source_path=f"policies/fake/{version}/{slug}.md",
            sha256=_digest(label),
            ingested_at=ingested_at,
        )
        session.add(document)
        await session.flush()
        for index in range(chunks):
            session.add(
                PolicyChunk(
                    tenant_id=tenant_id,
                    document_id=document.id,
                    chunk_index=index,
                    heading_path=["1. Purpose"],
                    char_start=index * 200,
                    char_end=index * 200 + 120,
                    qdrant_point_id=uuid.uuid4(),
                    text=f"Obviously fake policy span {index} of {slug} {version}.",
                )
            )
        await session.flush()
        return document.id


async def _read(factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID) -> Any:
    async with tenant_session(factory, tenant_id) as session:
        return await read_policy_documents(session)


def _one(documents: Sequence[PolicyDocumentDetail], slug: str) -> PolicyDocumentDetail:
    matches = [document for document in documents if document.slug == slug]
    assert len(matches) == 1, f"expected exactly one row for {slug!r}, got {len(matches)}"
    return matches[0]


def _versions(document: PolicyDocumentDetail) -> list[PolicyVersion]:
    return list(document.versions)


# --- the grouped shape and the two field sets ---------------------


def test_the_corpus_comes_back_one_row_per_slug_with_the_exact_two_field_sets(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The grouped shape, as the literals the tool puts on the wire.

    KILL: a read that answers one FLAT row per version, which the tool
    rejects - the two versions planted here under one slug would come back as
    two top-level rows, so the length assertion and the nested version list
    both fail, and 24h would be left grouping in the browser on every paint.
    Also kills a row that drops `sha256` or `source_path` (the
    digest is the only evidence on this surface that two versions differ at
    all) and one that drops `chunk_count`.

    The field sets are asserted EXACTLY, so a row that grew a seventh version
    field - a `created_at`, a `document_id`, a `chunk_ids` list - is as red as
    one short of a field.

    The second slug is planted so "one row per slug" is a statement about
    grouping rather than about a corpus of one.
    """

    async def scenario() -> None:
        factory = session_factory
        tenant_id = await _tenant(factory, "shape")
        await _plant_version(
            factory,
            tenant_id,
            slug="fake-access-policy",
            title="Fake Access Policy",
            version="v1",
            is_current=False,
            ingested_at=BASE_INSTANT,
            chunks=2,
        )
        await _plant_version(
            factory,
            tenant_id,
            slug="fake-access-policy",
            title="Fake Access Policy",
            version="v2",
            is_current=True,
            ingested_at=BASE_INSTANT + timedelta(days=1),
            chunks=3,
        )
        await _plant_version(
            factory,
            tenant_id,
            slug="fake-leave-policy",
            title="Fake Leave Policy",
            version="v1",
            is_current=True,
            ingested_at=BASE_INSTANT,
            chunks=1,
        )

        documents = await _read(factory, tenant_id)

        assert len(documents) == 2, documents
        for document in documents:
            assert _field_names(document) == DOCUMENT_FIELDS
            for version in document.versions:
                assert _field_names(version) == VERSION_FIELDS

        access = _one(documents, "fake-access-policy")
        assert [version.version for version in access.versions] == ["v2", "v1"]
        current, previous = _versions(access)
        assert current.is_current is True
        assert previous.is_current is False
        assert current.chunk_count == 3
        assert previous.chunk_count == 2
        assert current.source_path == "policies/fake/v2/fake-access-policy.md"
        assert previous.source_path == "policies/fake/v1/fake-access-policy.md"
        assert current.sha256 != previous.sha256
        assert len(current.sha256) == 64

        leave = _one(documents, "fake-leave-policy")
        assert [version.version for version in leave.versions] == ["v1"]

    asyncio.run(scenario())


# --- the one total order ------------------------------------------------------


def test_the_documents_are_slug_ascending_and_the_versions_ingested_at_descending(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The total order, asserted as exact sequences.

    KILL: an order left to Postgres (no ORDER BY, so insertion order comes
    back) and, the one the total order exists for, a version order taken from the
    `version` STRING - `v10` sorts before `v2` bytewise, so a string sort puts
    the tenth ingest in the middle of the list. The two versions here are
    planted with `v10` NEWER than `v2`, so a string sort answers `["v2",
    "v10"]` where the instants say `["v10", "v2"]`.

    The documents are planted in reverse slug order and the versions oldest
    first, so insertion order fails on both columns at once, and a version
    order ASCENDING by instant fails too.

    The slug column is the only one the document order may use: the two
    documents' titles sort in the OPPOSITE order to their slugs, so a read
    ordering by title answers a sequence this comparison rejects.
    """

    async def scenario() -> None:
        factory = session_factory
        tenant_id = await _tenant(factory, "order")
        # Planted last-first: the slug that must come SECOND, then the one
        # that must come first. Their titles sort the other way round.
        await _plant_version(
            factory,
            tenant_id,
            slug="fake-zz-retention",
            title="Aaa Fake Retention Standard",
            version="v1",
            is_current=True,
            ingested_at=BASE_INSTANT,
            chunks=1,
        )
        await _plant_version(
            factory,
            tenant_id,
            slug="fake-aa-access",
            title="Zzz Fake Access Standard",
            version="v2",
            is_current=False,
            ingested_at=BASE_INSTANT,
            chunks=1,
        )
        await _plant_version(
            factory,
            tenant_id,
            slug="fake-aa-access",
            title="Zzz Fake Access Standard",
            version="v10",
            is_current=True,
            ingested_at=BASE_INSTANT + timedelta(days=30),
            chunks=1,
        )

        documents = await _read(factory, tenant_id)

        assert [document.slug for document in documents] == [
            "fake-aa-access",
            "fake-zz-retention",
        ]
        assert [version.version for version in documents[0].versions] == ["v10", "v2"]
        assert [version.ingested_at for version in documents[0].versions] == [
            BASE_INSTANT + timedelta(days=30),
            BASE_INSTANT,
        ]

    asyncio.run(scenario())


def test_two_versions_ingested_at_one_instant_are_tiebroken_by_version_descending(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The tie-break, on a real case: two versions
    of one document ingested in a single transaction.

    KILL: an order on `ingested_at` ALONE. Both rows here carry exactly the
    same instant, so that implementation leaves them in whatever order
    Postgres returns - which is insertion order, planted here as the OPPOSITE
    of the expected sequence, so the case flips. Nothing else in this file can
    see it: every other planted version carries a distinct instant.

    DIRECTION: the requirements name
    `version` as the tie-break column and no direction. Fixed here as
    DESCENDING - the order reads newest-first end to end, and the single
    `sorted(key=(ingested_at, version), reverse=True)` that produces it
    reverses both columns together. `v10` before `v2` at one instant is also
    the later cut first, which is what the rest of the order means.
    """

    async def scenario() -> None:
        factory = session_factory
        tenant_id = await _tenant(factory, "tiebreak")
        for version in ("v2", "v3"):
            await _plant_version(
                factory,
                tenant_id,
                slug="fake-tiebreak-policy",
                title="Fake Tiebreak Policy",
                version=version,
                is_current=version == "v3",
                ingested_at=BASE_INSTANT,
                chunks=1,
            )

        documents = await _read(factory, tenant_id)

        tied = _one(documents, "fake-tiebreak-policy")
        assert [version.version for version in tied.versions] == ["v3", "v2"]
        assert {version.ingested_at for version in tied.versions} == {BASE_INSTANT}

    asyncio.run(scenario())


# --- chunk_count, zero included -----------------------------------------------


def test_each_versions_chunk_count_is_exact_and_a_version_with_no_chunk_is_kept(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The failure this page exists to catch.

    KILL: a count taken over the whole DOCUMENT SLUG rather than per version
    (both versions here would then read 3), a count that is really the number
    of versions, and - the one that matters - a grouped count JOINED to the
    documents, which drops the version with no `policy_chunks` row entirely.
    An ingest that registered a document and wrote no span is invisible any
    other way, so a read that hides it reports a broken corpus as clean.

    `0` is asserted as the value AND the row asserted present, because the two
    are different defects: a dropped row and a row reading `3` both pass one
    half of this.

    The second slug carries a single chunked version, so an implementation
    that defaulted every count to `0` is red here too.
    """

    async def scenario() -> None:
        factory = session_factory
        tenant_id = await _tenant(factory, "chunks")
        await _plant_version(
            factory,
            tenant_id,
            slug="fake-half-ingested",
            title="Fake Half Ingested Policy",
            version="v1",
            is_current=False,
            ingested_at=BASE_INSTANT,
            chunks=3,
        )
        await _plant_version(
            factory,
            tenant_id,
            slug="fake-half-ingested",
            title="Fake Half Ingested Policy",
            version="v2",
            is_current=True,
            ingested_at=BASE_INSTANT + timedelta(hours=1),
            chunks=0,
        )
        await _plant_version(
            factory,
            tenant_id,
            slug="fake-whole-policy",
            title="Fake Whole Policy",
            version="v1",
            is_current=True,
            ingested_at=BASE_INSTANT,
            chunks=5,
        )

        documents = await _read(factory, tenant_id)

        half = _one(documents, "fake-half-ingested")
        assert [version.version for version in half.versions] == ["v2", "v1"]
        assert [version.chunk_count for version in half.versions] == [0, 3]
        whole = _one(documents, "fake-whole-policy")
        assert [version.chunk_count for version in whole.versions] == [5]

    asyncio.run(scenario())


# --- which title the document reads as -----------------------------


def test_the_title_is_the_current_versions_even_when_another_version_is_newer(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The first half: a re-titled document reads as its LIVE title.

    KILL: a title taken from the newest-INGESTED version, and a title taken
    from the first row the read happens to see. The current version here is
    the OLDER of the two, so those two rules disagree with the criterion and
    with each other on this corpus - which is the only arrangement that
    separates them (a corpus whose current version is also its newest passes
    under all three rules).

    The state is a real one: `is_current` is per
    (tenant, slug), so a re-ingest that registered a new version and has not
    yet flipped the flag leaves exactly this pair.
    """

    async def scenario() -> None:
        factory = session_factory
        tenant_id = await _tenant(factory, "title-current")
        await _plant_version(
            factory,
            tenant_id,
            slug="fake-retitled-policy",
            title="Fake Live Title",
            version="v1",
            is_current=True,
            ingested_at=BASE_INSTANT,
            chunks=1,
        )
        await _plant_version(
            factory,
            tenant_id,
            slug="fake-retitled-policy",
            title="Fake Newer But Not Current Title",
            version="v2",
            is_current=False,
            ingested_at=BASE_INSTANT + timedelta(days=7),
            chunks=1,
        )

        documents = await _read(factory, tenant_id)

        retitled = _one(documents, "fake-retitled-policy")
        assert retitled.title == "Fake Live Title"
        # The newest-ingested version is still LISTED; only the title
        # comes from the current one.
        assert [version.version for version in retitled.versions] == ["v2", "v1"]
        assert [version.is_current for version in retitled.versions] == [False, True]

    asyncio.run(scenario())


def test_a_document_with_no_current_version_appears_with_every_flag_false(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Criteria 4 (second half) and 6: the half-finished ingest is rendered.

    KILL: a read that FILTERS on `is_current` to pick the title - a
    `.one()` or an index into an empty list raises, and a `WHERE
    is_current` join drops the document altogether, which would report a
    corpus as clean while one of its documents has no live copy. That is the
    exact state this page exists to show.

    Second kill, the title fallback: with no current row the title must come
    from the NEWEST-ingested version. The two versions here carry different
    titles and the newest is planted FIRST, so a fallback that took the first
    row seen, the lowest version string or the oldest instant answers the
    other title.

    A document WITH a current version is planted beside it, so an
    implementation that simply never reads `is_current` at all still fails:
    that one's flags must not all be false.
    """

    async def scenario() -> None:
        factory = session_factory
        tenant_id = await _tenant(factory, "no-current")
        await _plant_version(
            factory,
            tenant_id,
            slug="fake-orphaned-policy",
            title="Fake Newest Orphan Title",
            version="v2",
            is_current=False,
            ingested_at=BASE_INSTANT + timedelta(days=2),
            chunks=1,
        )
        await _plant_version(
            factory,
            tenant_id,
            slug="fake-orphaned-policy",
            title="Fake Older Orphan Title",
            version="v1",
            is_current=False,
            ingested_at=BASE_INSTANT,
            chunks=1,
        )
        await _plant_version(
            factory,
            tenant_id,
            slug="fake-settled-policy",
            title="Fake Settled Policy",
            version="v1",
            is_current=True,
            ingested_at=BASE_INSTANT,
            chunks=1,
        )

        documents = await _read(factory, tenant_id)

        orphaned = _one(documents, "fake-orphaned-policy")
        assert [version.is_current for version in orphaned.versions] == [False, False]
        assert orphaned.title == "Fake Newest Orphan Title"
        assert [version.version for version in orphaned.versions] == ["v2", "v1"]

        settled = _one(documents, "fake-settled-policy")
        assert [version.is_current for version in settled.versions] == [True]

    asyncio.run(scenario())


# --- the tenant guard ----------------------


def test_the_same_slug_in_two_tenants_never_mixes_titles_counts_or_digests(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The tenant guard, said out loud.

    KILL: a read that leaves the tenant-scoped repository - a raw
    `select(PolicyDocument)` on an unbound session, a chunk count written by
    hand without the guard, a join built here instead of two scoped
    statements. Both tenants carry the SAME slug, so a lost filter answers one
    plausible document row with four versions instead of two, and only a
    comparison against the other tenant's titles, counts and digests can see
    it: the row count alone is asserted last, not first.

    The chunk count is the half most likely to leak on its own -
    `count_chunks_by_document` is a second statement taking ids, and an
    unguarded version of it would answer the other tenant's spans for ids that
    are not the reader's at all. The counts here differ per tenant (2 and 7)
    for exactly that reason.

    Both tenants are read through their own binding in the same case, so a
    read that answered ONE tenant's corpus to everybody fails on one side or
    the other.
    """

    async def scenario() -> None:
        factory = session_factory
        reader = await _tenant(factory, "iso-reader")
        other = await _tenant(factory, "iso-other")
        await _plant_version(
            factory,
            reader,
            slug="fake-shared-slug",
            title="Fake Reader Title",
            version="v1",
            is_current=True,
            ingested_at=BASE_INSTANT,
            chunks=2,
            digest_label="reader-v1",
        )
        await _plant_version(
            factory,
            other,
            slug="fake-shared-slug",
            title="Fake Other Tenant Title",
            version="v1",
            is_current=True,
            ingested_at=BASE_INSTANT + timedelta(days=1),
            chunks=7,
            digest_label="other-v1",
        )
        await _plant_version(
            factory,
            other,
            slug="fake-other-only-slug",
            title="Fake Other Only Title",
            version="v1",
            is_current=True,
            ingested_at=BASE_INSTANT,
            chunks=4,
            digest_label="other-only-v1",
        )

        reader_documents = await _read(factory, reader)
        other_documents = await _read(factory, other)

        shared = _one(reader_documents, "fake-shared-slug")
        assert shared.title == "Fake Reader Title"
        assert [version.chunk_count for version in shared.versions] == [2]
        assert [version.sha256 for version in shared.versions] == [_digest("reader-v1")]
        titles = {document.title for document in reader_documents}
        digests = {version.sha256 for document in reader_documents for version in document.versions}
        assert "Fake Other Tenant Title" not in titles
        assert "Fake Other Only Title" not in titles
        assert _digest("other-v1") not in digests
        assert _digest("other-only-v1") not in digests
        assert [document.slug for document in reader_documents] == ["fake-shared-slug"]

        assert [document.slug for document in other_documents] == [
            "fake-other-only-slug",
            "fake-shared-slug",
        ]
        assert _one(other_documents, "fake-shared-slug").title == "Fake Other Tenant Title"

    asyncio.run(scenario())


# --- a tenant with no corpus ---------------------------------------


def test_a_tenant_with_no_corpus_answers_an_empty_tuple(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The first half: an empty corpus is an answer, never a failure.

    KILL: a helper that raises on the empty read - a `.one()` on the document
    query, or an unguarded count over an empty id set (`IN ()` is not valid
    SQL, which is why `Repository.get_many` short-circuits and a hand-written
    `count_chunks_by_document` need not). Either turns a tenant that has never
    been ingested into a broken `/admin/policies` page, and no other case here
    can see it because every other tenant in this file carries a document.
    """

    async def scenario() -> None:
        factory = session_factory
        tenant_id = await _tenant(factory, "empty")

        documents = await _read(factory, tenant_id)

        assert documents == ()

    asyncio.run(scenario())


# --- two statements, whatever the corpus size ---------------------------------


def test_the_whole_read_issues_exactly_two_selects(
    session_factory: async_sessionmaker[AsyncSession],
    shared_engine: AsyncEngine,
) -> None:
    """The count, taken off the engine rather than read off the source.

    KILL: the obvious count - one `SELECT count(*)` per VERSION, which is five
    statements for the four versions here and grows with the corpus. A board
    that is merely slow looks identical from the outside, so nothing else in
    this file goes red for it; this counter fixes the number at two for a
    corpus of three documents spanning four versions, so a later refactor that
    reaches for a per-row count is a failing case rather than a slow page.

    The window is measured around the SECOND call, after a warm-up read on the
    same engine: the first statement on a fresh pool carries the driver's own
    connection setup, which is not the helper's work.
    """

    async def scenario() -> None:
        factory = session_factory
        engine = shared_engine
        tenant_id = await _tenant(factory, "count")
        for slug, version, instant, chunks in (
            ("fake-count-a", "v1", BASE_INSTANT, 2),
            ("fake-count-a", "v2", BASE_INSTANT + timedelta(days=1), 3),
            ("fake-count-b", "v1", BASE_INSTANT, 0),
            ("fake-count-c", "v1", BASE_INSTANT, 1),
        ):
            await _plant_version(
                factory,
                tenant_id,
                slug=slug,
                title=f"Fake Counted Policy {slug}",
                version=version,
                is_current=version != "v1" or slug != "fake-count-a",
                ingested_at=instant,
                chunks=chunks,
            )

        warm_up = await _read(factory, tenant_id)
        assert len(warm_up) == 3, warm_up

        statements: list[str] = []
        _watch(engine, statements)
        documents = await _read(factory, tenant_id)

        assert len(documents) == 3, documents
        assert sum(len(document.versions) for document in documents) == 4
        assert statements, "the statement counter never fired"
        assert len(statements) == EXPECTED_STATEMENTS, statements
        for statement in statements:
            assert statement.lstrip().upper().startswith("SELECT"), statement

    asyncio.run(scenario())


# --- probes around the helper, never through it ----------------------------------


def _watch(engine: AsyncEngine, statements: list[str]) -> None:
    """Record every statement this engine sends to Postgres.

    The shape `test_access_sod_rules.py` and `test_audit_search.py` already use
    for the same question."""

    def _record(
        conn: Connection,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _record)
