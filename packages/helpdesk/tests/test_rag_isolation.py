"""An earlier change §6, the design: one tenant's corpus is unreachable from another's.

The only file in an earlier change that loads a model. It reuses `test_rag_embedding
.py`'s session-scoped `embedder` fixture rather than
building a second `FastEmbedEmbedder`: both models cost seconds of ONNX setup
even against a warm cache, and the isolation claim needs REAL vectors — a
fake embedder's similarities are chosen by the test, so a dense search that
found nothing would prove nothing about the store.

Expected implementation surface: `helpdesk.rag.pipeline.ingest_document` and
`helpdesk.rag.vectors` exactly as `test_rag_pipeline.py` and
`test_rag_vectors.py` describe them.

Isolation under this shape is the collection NAME (plan §5): one collection
per tenant means a cross-tenant hit requires naming the other tenant's
collection. The case therefore searches tenant A's collection for tenant B's
marker sentence by BOTH routes a retriever has — the lexical one (scroll the
payloads) and the dense one (query with the marker's own embedding) — and
also asserts the marker is absent from tenant A's `policy_chunks`, which is
the Postgres half the same ingest wrote.

The two collections are dropped at both ends of the session, so a previous
run's points can neither hide a leak nor invent one.

No pytest-asyncio: the case is a sync function driving one `asyncio.run`.
"""

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from helpdesk.core.db import create_engine, create_session_factory, tenant_session
from helpdesk.core.repository import Repository, find_tenant_by_slug
from helpdesk.models import PolicyChunk
from helpdesk.rag.embedding import FastEmbedEmbedder
from helpdesk.rag.pipeline import ingest_document
from helpdesk.rag.vectors import DENSE_VECTOR_NAME, collection_name, create_client
from qdrant_client import AsyncQdrantClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Imported, not redefined: one database, one pair of models,
# one Qdrant connectivity gate for the whole run.
from test_policy_models import (
    TENANT_A_SLUG,
    TENANT_B_SLUG,
    policy_db_url,  # noqa: F401
)
from test_rag_embedding import embedder  # noqa: F401
from test_rag_pipeline import FakeRedactor
from test_rag_vectors import (
    _settings,
    qdrant_url,  # noqa: F401
)

INGESTED_AT = datetime(2026, 5, 4, 9, 30, tzinfo=UTC)

# The marker is a sentence no other fixture, seed or corpus file contains, and
# it is obviously fake. It is one sentence so it survives the
# sentence splitter intact and can be searched for as an exact phrase.
MARKER = "Fake tenant B keeps its spare badges in the marmalade cupboard."

TENANT_B_SOURCE = (
    b"# Fake Physical Access Policy\n\n"
    b"## 1. Purpose\n\n" + MARKER.encode("utf-8") + b" "
    b"Fake spare badges are issued by reception and logged on collection.\n"
)
TENANT_A_SOURCE = (
    b"# Fake Access Control Policy\n\n"
    b"## 1. Purpose\n\n"
    b"Fake access to a system is granted by that system's owner. "
    b"Fake spare badges are issued by reception and logged on collection.\n"
)


@pytest.fixture(scope="session")
def isolation_collections(qdrant_url: str) -> Iterator[tuple[str, str]]:  # noqa: F811
    """Both tenants' collections, dropped at both ends of the session.

    Never a wipe of the instance and never a per-test rebuild (standing rule
    7): the namespace is the two seeded tenant slugs, which only this test
    database ever ingests.
    """
    names = (collection_name(TENANT_A_SLUG), collection_name(TENANT_B_SLUG))
    for name in names:
        httpx.delete(f"{qdrant_url}/collections/{name}", timeout=30.0)
    yield names
    for name in names:
        httpx.delete(f"{qdrant_url}/collections/{name}", timeout=30.0)


def _text(point: Any) -> str:
    """One point's payload text, asserted present rather than narrowed away."""
    assert point.payload is not None, f"point {point.id} carries no payload"
    return str(point.payload["text"])


async def _tenant_id(factory: async_sessionmaker[AsyncSession], slug: str) -> uuid.UUID:
    async with factory() as session:
        tenant = await find_tenant_by_slug(session, slug)
    assert tenant is not None, slug
    return tenant.id


async def _ingest(
    factory: async_sessionmaker[AsyncSession],
    client: AsyncQdrantClient,
    model: FastEmbedEmbedder,
    *,
    tenant_slug: str,
    document_slug: str,
    source: bytes,
) -> None:
    tenant_id = await _tenant_id(factory, tenant_slug)
    async with tenant_session(factory, tenant_id) as session:
        await ingest_document(
            session,
            client,
            tenant_slug=tenant_slug,
            slug=document_slug,
            title="Fake Policy",
            version="v1",
            source_path=f"policies/{tenant_slug}/v1/{document_slug}.md",
            source=source,
            redactor=FakeRedactor(),
            embedder=model,
            ingested_at=INGESTED_AT,
        )


def test_tenant_bs_marker_sentence_is_unreachable_from_tenant_a(
    policy_db_url: str,  # noqa: F811
    embedder: FastEmbedEmbedder,  # noqa: F811
    isolation_collections: tuple[str, str],
) -> None:
    """Catches an ingest that writes every tenant's points into one shared
    collection, or that derives the collection name from anything but the
    tenant the session is bound to — the design's whole claim.

    Three independent routes are asserted, because each catches a different
    wrong implementation: the payload scroll catches a shared collection, the
    dense query catches a collection whose points were written under tenant
    A's name with tenant B's content, and the `policy_chunks` read catches the
    Postgres half writing under the wrong tenant while Qdrant looks correct.

    Tenant A is given a document that SHARES a sentence with tenant B's, so
    the dense query is asked against a collection that genuinely holds near
    neighbours: an empty collection would answer "no marker" whether the rule
    held or not.
    """
    a_slug = f"fake-isolation-a-{uuid.uuid4().hex[:8]}"
    b_slug = f"fake-isolation-b-{uuid.uuid4().hex[:8]}"

    async def scenario() -> tuple[list[str], list[str], list[str], int]:
        engine = create_engine(policy_db_url)
        client = create_client(_settings())
        try:
            factory = create_session_factory(engine)
            await _ingest(
                factory,
                client,
                embedder,
                tenant_slug=TENANT_B_SLUG,
                document_slug=b_slug,
                source=TENANT_B_SOURCE,
            )
            await _ingest(
                factory,
                client,
                embedder,
                tenant_slug=TENANT_A_SLUG,
                document_slug=a_slug,
                source=TENANT_A_SOURCE,
            )

            # 1. every payload in tenant A's collection, read exhaustively.
            points, _ = await client.scroll(
                collection_name(TENANT_A_SLUG), limit=1000, with_payload=True
            )
            scrolled = [_text(point) for point in points]

            # 2. the dense route: tenant A's collection queried with the
            #    marker's OWN embedding, which is the best possible query for
            #    it. Whatever comes back must not be it.
            vector = embedder.embed_dense([MARKER])[0]
            response: Any = await client.query_points(
                collection_name(TENANT_A_SLUG),
                query=list(vector),
                using=DENSE_VECTOR_NAME,
                limit=10,
                with_payload=True,
            )
            searched = [_text(point) for point in response.points]

            # 3. tenant B's own collection, as the premise: the marker really
            #    was ingested somewhere, so "absent from A" is isolation and
            #    not a failed ingest.
            b_points, _ = await client.scroll(
                collection_name(TENANT_B_SLUG), limit=1000, with_payload=True
            )
            planted = [_text(point) for point in b_points]

            tenant_a = await _tenant_id(factory, TENANT_A_SLUG)
            async with tenant_session(factory, tenant_a) as session:
                rows = await Repository(session, PolicyChunk).list()
                leaked = len([row for row in rows if MARKER in row.text])
            return scrolled, searched, planted, leaked
        finally:
            await client.close()
            await engine.dispose()

    scrolled, searched, planted, leaked = asyncio.run(scenario())

    # The premise, read off the fixture rather than assumed.
    assert any(MARKER in text for text in planted), "tenant B's marker was never ingested"
    assert searched, "tenant A's collection returned no neighbours at all"

    assert not any(MARKER in text for text in scrolled)
    assert not any(MARKER in text for text in searched)
    assert leaked == 0
