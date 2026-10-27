"""The per-tenant search cache in `helpdesk/mcp_server/services/rag/search_cache.py`.

Expected implementation surface (the implementer must provide exactly this):

``search_cache``
    ``MIN_SIMILARITY = 0.95``, ``MAX_AGE = timedelta(hours=24)``,
    ``MIN_QUESTION_CHARS = 16``.
    ``cache_collection_name(tenant_slug) -> str`` — ``{prefix}cache_{slug}``,
        the slug checked, the whole name capped at the policy cap plus 6.
    ``async current_generation(client, tenant_slug) -> str`` — makes the cache
        collection and its marker point when missing, returns the stored
        random generation.
    ``async lookup(client, tenant_slug, *, tenant_id, vector, top_k,
        generation, now) -> list[SearchHit] | None`` — ``None`` is a miss.
        One ``query_points`` with ``score_threshold`` 0.95 and ``limit`` 1.
    ``async save(client, tenant_slug, *, tenant_id, vector, top_k, generation,
        now, hits) -> None``
    ``async drop(client, tenant_slug) -> None`` — a new generation, then every
        entry deleted; does nothing when the collection does not exist.
    Any Qdrant error is raised as ``VectorStoreUnavailableError``.

The compose Qdrant is shared and never wiped. Each case owns a uuid-suffixed
tenant slug, so its cache collection is its own namespace, and the fixture
deletes it at the end. Vectors are unit vectors of the real width with exact
cosines, so the 0.95 edge is decided by the store and not by rounding.

No pytest-asyncio: each case is a sync function driving one `asyncio.run`.
"""

import asyncio
import math
import uuid
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
import pytest
from qdrant_client import AsyncQdrantClient, models

from helpdesk.mcp_server.services.rag import search_cache
from helpdesk.mcp_server.services.rag.embedding import DENSE_DIMENSION
from helpdesk.mcp_server.services.rag.retrieval import SearchHit
from helpdesk.mcp_server.services.rag.vectors import (
    VectorStoreUnavailableError,
    collection_name,
    create_client,
)
from helpdesk.shared.core.naming import SLUG_LENGTH, InvalidPolicyNameError
from tests.mcp_server.services.rag.test_rag_vectors import (
    CLOSED_PORT_URL,
    _settings,
    qdrant_url,  # noqa: F401
)

pytestmark = pytest.mark.db

SAVED_AT = datetime(2031, 3, 14, 15, 9, 26, tzinfo=UTC)
TOP_K = 6

# One page worth saving: two hits, one reached by the dense arm only, so the
# round trip of an optional `dense_score` of `None` is covered as well.
HITS = [
    SearchHit(
        chunk_id=uuid.UUID("11111111-1111-4111-8111-111111111111"),
        document_id=uuid.UUID("22222222-2222-4222-8222-222222222222"),
        document_slug="fake-badge-policy",
        version="v1",
        heading_path=("1. Purpose", "1.1 Fake badges"),
        text="Fake badges are issued by the fake front desk.",
        score=0.03278688524590164,
        dense_score=0.8123,
    ),
    SearchHit(
        chunk_id=uuid.UUID("33333333-3333-4333-8333-333333333333"),
        document_id=uuid.UUID("22222222-2222-4222-8222-222222222222"),
        document_slug="fake-badge-policy",
        version="v2",
        heading_path=("2. Scope",),
        text="Fake contractors follow the same fake rules.",
        score=0.016129032258064516,
        dense_score=None,
    ),
]
OTHER_HITS = [HITS[1]]


def _unit(cosine: float) -> tuple[float, ...]:
    """A unit vector whose cosine to `_unit(1.0)` is exactly `cosine`."""
    vector = [0.0] * DENSE_DIMENSION
    vector[0] = cosine
    vector[1] = math.sqrt(1.0 - cosine * cosine)
    return tuple(vector)


SAVED_VECTOR = _unit(1.0)


# --- fixtures and drivers -----------------------------------------------------


@pytest.fixture
def tenant_slug(qdrant_url: str) -> Iterator[str]:  # noqa: F811
    """A tenant slug of this case's own; its cache collection is deleted after."""
    slug = f"fake-cache-{uuid.uuid4().hex[:12]}"
    yield slug
    httpx.delete(f"{qdrant_url}/collections/{search_cache.cache_collection_name(slug)}", timeout=30)


@pytest.fixture
def other_slug(qdrant_url: str) -> Iterator[str]:  # noqa: F811
    """A second tenant's slug, for the cases that need two."""
    slug = f"fake-cache-{uuid.uuid4().hex[:12]}"
    yield slug
    httpx.delete(f"{qdrant_url}/collections/{search_cache.cache_collection_name(slug)}", timeout=30)


def _drive[T](work: Callable[[AsyncQdrantClient], Awaitable[T]]) -> T:
    async def scenario() -> T:
        client = create_client(_settings())
        try:
            return await work(client)
        finally:
            await client.close()

    return asyncio.run(scenario())


class ProxyClient:
    """The real client, with every `query_points` call written down.

    Everything else is forwarded untouched, so the store still answers; the
    record is how a case sees the arguments the lookup really sent.
    """

    def __init__(self, real: AsyncQdrantClient) -> None:
        self._real = real
        self.queries: list[tuple[str, dict[str, Any]]] = []

    async def query_points(self, collection_name: str, /, **kwargs: Any) -> Any:
        self.queries.append((collection_name, dict(kwargs)))
        return await self._real.query_points(collection_name, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


async def _save(
    client: AsyncQdrantClient,
    slug: str,
    tenant_id: uuid.UUID,
    *,
    vector: tuple[float, ...] = SAVED_VECTOR,
    top_k: int = TOP_K,
    generation: str | None = None,
    now: datetime = SAVED_AT,
    hits: list[SearchHit] | None = None,
) -> str:
    """Save one entry under the current generation (or the one given); return it."""
    stamp = generation or await search_cache.current_generation(client, slug)
    await search_cache.save(
        client,
        slug,
        tenant_id=tenant_id,
        vector=vector,
        top_k=top_k,
        generation=stamp,
        now=now,
        hits=HITS if hits is None else hits,
    )
    return stamp


async def _lookup(
    client: AsyncQdrantClient,
    slug: str,
    tenant_id: uuid.UUID,
    generation: str,
    *,
    vector: tuple[float, ...] = SAVED_VECTOR,
    top_k: int = TOP_K,
    now: datetime = SAVED_AT + timedelta(minutes=5),
) -> list[SearchHit] | None:
    return await search_cache.lookup(
        client,
        slug,
        tenant_id=tenant_id,
        vector=vector,
        top_k=top_k,
        generation=generation,
        now=now,
    )


async def _entries(client: AsyncQdrantClient, slug: str) -> int:
    """How many cached entries (not the marker) the tenant's collection holds."""
    counted = await client.count(
        search_cache.cache_collection_name(slug),
        count_filter=models.Filter(
            must=[models.FieldCondition(key="kind", match=models.MatchValue(value="entry"))]
        ),
        exact=True,
    )
    return int(counted.count)


# --- the constants ------------------------------------------------------------


def test_the_cache_limits_are_exactly_their_planned_values() -> None:
    """Catches a floor, an age or a question length of any other size: each
    decides whether a stale or unrelated page is served, so a range test would
    admit the wrong number."""
    assert search_cache.MIN_SIMILARITY == 0.95
    assert timedelta(hours=24) == search_cache.MAX_AGE
    assert search_cache.MIN_QUESTION_CHARS == 16


# --- hits and misses ----------------------------------------------------------


def test_a_repeat_question_hits(tenant_slug: str) -> None:
    """Catches a cache that saves nothing, keys on something else than the
    vector, or loses a field in the round trip (the tuple heading path, the
    `None` dense score, the float scores): the saved page must come back with
    every field equal."""
    tenant_id = uuid.uuid4()

    async def work(client: AsyncQdrantClient) -> list[SearchHit] | None:
        generation = await _save(client, tenant_slug, tenant_id)
        return await _lookup(client, tenant_slug, tenant_id, generation)

    assert _drive(work) == HITS


def test_a_reworded_question_at_0_951_hits(tenant_slug: str) -> None:
    """Catches a lookup that wants an exact vector match, or a floor above
    0.951: a reworded question one hair over the floor must still be served the
    saved page. The saved vector is the only thing in the collection, so only
    the score threshold can decide."""
    tenant_id = uuid.uuid4()

    async def work(client: AsyncQdrantClient) -> list[SearchHit] | None:
        generation = await _save(client, tenant_slug, tenant_id)
        return await _lookup(client, tenant_slug, tenant_id, generation, vector=_unit(0.951))

    assert _drive(work) == HITS


def test_a_question_at_0_949_misses(tenant_slug: str) -> None:
    """Catches a floor below 0.95 (0.9, say, or none at all): a question that
    means something else would be answered with the wrong page. Everything
    else matches (tenant, top_k, generation, age), so the threshold alone
    decides."""
    tenant_id = uuid.uuid4()

    async def work(client: AsyncQdrantClient) -> list[SearchHit] | None:
        generation = await _save(client, tenant_slug, tenant_id)
        return await _lookup(client, tenant_slug, tenant_id, generation, vector=_unit(0.949))

    assert _drive(work) is None


def test_the_hit_floor_is_0_95(tenant_slug: str) -> None:
    """Catches a lookup that filters by score itself, or sends another number:
    the store must be asked for exactly one entry above exactly 0.95, in the
    tenant's own cache collection, in one query."""
    tenant_id = uuid.uuid4()

    async def work(client: AsyncQdrantClient) -> ProxyClient:
        generation = await _save(client, tenant_slug, tenant_id)
        proxy = ProxyClient(client)
        await _lookup(cast(AsyncQdrantClient, proxy), tenant_slug, tenant_id, generation)
        return proxy

    proxy = _drive(work)

    assert search_cache.MIN_SIMILARITY == 0.95
    assert len(proxy.queries) == 1
    name, kwargs = proxy.queries[0]
    assert name == search_cache.cache_collection_name(tenant_slug)
    assert kwargs["score_threshold"] == 0.95
    assert kwargs["limit"] == 1


def test_another_tenant_never_hits(tenant_slug: str, other_slug: str) -> None:
    """Catches a lookup that does not keep tenants apart, on both of the two
    walls. Tenant B asking the exact saved vector must miss, and must name only
    its own collection (one collection per tenant is the isolation). Then the
    SAME collection is asked with another tenant id and the right generation,
    top_k and age: only the `tenant_id` filter can turn that into a miss."""
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()

    async def work(
        client: AsyncQdrantClient,
    ) -> tuple[list[SearchHit] | None, list[SearchHit] | None, ProxyClient]:
        generation_a = await _save(client, tenant_slug, tenant_a)
        generation_b = await search_cache.current_generation(client, other_slug)
        proxy = ProxyClient(client)
        across = await _lookup(cast(AsyncQdrantClient, proxy), other_slug, tenant_b, generation_b)
        same_collection = await _lookup(client, tenant_slug, tenant_b, generation_a)
        return across, same_collection, proxy

    across, same_collection, proxy = _drive(work)

    assert across is None
    assert [name for name, _ in proxy.queries] == [search_cache.cache_collection_name(other_slug)]
    assert same_collection is None


def test_an_entry_23h59m_old_hits_and_24h_old_misses(tenant_slug: str) -> None:
    """Catches an age limit that is off by one at the edge: `<` vs `<=` on the
    24 hours, or 23 or 25 hours in place of 24. One minute under the limit
    must hit and exactly the limit must miss, both against the same entry on a
    fake clock."""
    tenant_id = uuid.uuid4()

    async def work(
        client: AsyncQdrantClient,
    ) -> tuple[list[SearchHit] | None, list[SearchHit] | None, list[SearchHit] | None]:
        generation = await _save(client, tenant_slug, tenant_id)
        young = await _lookup(
            client,
            tenant_slug,
            tenant_id,
            generation,
            now=SAVED_AT + timedelta(hours=23, minutes=59),
        )
        edge = await _lookup(
            client, tenant_slug, tenant_id, generation, now=SAVED_AT + timedelta(hours=24)
        )
        old = await _lookup(
            client,
            tenant_slug,
            tenant_id,
            generation,
            now=SAVED_AT + timedelta(hours=24, seconds=1),
        )
        return young, edge, old

    young, edge, old = _drive(work)

    assert young == HITS
    assert edge is None
    assert old is None


def test_a_different_top_k_misses(tenant_slug: str) -> None:
    """Catches a cache that ignores the page size: a page saved for 6 would
    answer a question for 3 with six hits. The same vector asked for 6 is the
    control that proves the miss comes from `top_k` alone."""
    tenant_id = uuid.uuid4()

    async def work(
        client: AsyncQdrantClient,
    ) -> tuple[list[SearchHit] | None, list[SearchHit] | None]:
        generation = await _save(client, tenant_slug, tenant_id, top_k=6)
        same = await _lookup(client, tenant_slug, tenant_id, generation, top_k=6)
        other = await _lookup(client, tenant_slug, tenant_id, generation, top_k=3)
        return same, other

    same, other = _drive(work)

    assert same == HITS
    assert other is None


# --- drop and generation ------------------------------------------------------


def test_a_drop_empties_one_tenant_and_leaves_the_other(tenant_slug: str, other_slug: str) -> None:
    """Catches a drop that leaves entries behind, that serves the old page
    after the drop, or that reaches into another tenant's cache. After the drop
    tenant A holds 0 entries and misses (also on the new generation, where
    only the empty store can explain it); tenant B still hits."""
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()

    async def work(client: AsyncQdrantClient) -> tuple[Any, ...]:
        old_a = await _save(client, tenant_slug, tenant_a)
        generation_b = await _save(client, other_slug, tenant_b, hits=OTHER_HITS)
        before = (await _entries(client, tenant_slug), await _entries(client, other_slug))
        await search_cache.drop(client, tenant_slug)
        new_a = await search_cache.current_generation(client, tenant_slug)
        return (
            before,
            await _entries(client, tenant_slug),
            await _entries(client, other_slug),
            old_a != new_a,
            await _lookup(client, tenant_slug, tenant_a, new_a),
            await _lookup(client, other_slug, tenant_b, generation_b),
        )

    before, a_entries, b_entries, generation_changed, a_lookup, b_lookup = _drive(work)

    assert before == (1, 1)
    assert a_entries == 0
    assert b_entries == 1
    assert generation_changed is True
    assert a_lookup is None
    assert b_lookup == OTHER_HITS


def test_an_entry_stamped_before_a_drop_never_hits(tenant_slug: str) -> None:
    """Catches a cache that only deletes and has no generation: a search that
    read the generation before a policy change and saves AFTER the drop would
    plant an old page that serves for a day. Here the old-stamped write lands
    after the drop with the same vector as a fresh one; the lookup under the
    new generation must miss it, and must return the fresh page once that is
    saved."""
    tenant_id = uuid.uuid4()

    async def work(
        client: AsyncQdrantClient,
    ) -> tuple[list[SearchHit] | None, list[SearchHit] | None]:
        old_generation = await search_cache.current_generation(client, tenant_slug)
        await search_cache.drop(client, tenant_slug)
        new_generation = await search_cache.current_generation(client, tenant_slug)
        await _save(client, tenant_slug, tenant_id, generation=old_generation, hits=HITS)
        stale_only = await _lookup(client, tenant_slug, tenant_id, new_generation)
        await _save(
            client,
            tenant_slug,
            tenant_id,
            generation=new_generation,
            vector=_unit(0.99),
            hits=OTHER_HITS,
        )
        fresh = await _lookup(client, tenant_slug, tenant_id, new_generation)
        return stale_only, fresh

    stale_only, fresh = _drive(work)

    assert stale_only is None
    assert fresh == OTHER_HITS


def test_a_drop_with_no_cache_collection_does_nothing(tenant_slug: str) -> None:
    """Catches a drop that raises on a tenant that never had a cache, which
    would fail the first ingest of every new tenant, or one that creates the
    collection as a side effect."""

    async def work(client: AsyncQdrantClient) -> bool:
        await search_cache.drop(client, tenant_slug)
        return await client.collection_exists(search_cache.cache_collection_name(tenant_slug))

    assert _drive(work) is False


# --- names --------------------------------------------------------------------


def test_a_64_character_slug_gets_a_cache_name_and_65_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a name cap that is too small for the longest legal slug (the
    cache name is six characters longer than the policy name, so reusing the
    policy cap refuses a valid tenant), and a name built from a slug that was
    never checked. The prefix is the default here, so the name is exact."""
    monkeypatch.setenv("QDRANT_COLLECTION_PREFIX", "policy_")
    longest = "a" * SLUG_LENGTH

    assert search_cache.cache_collection_name(longest) == f"policy_cache_{longest}"
    with pytest.raises(InvalidPolicyNameError):
        search_cache.cache_collection_name("a" * (SLUG_LENGTH + 1))


@pytest.mark.parametrize(
    "slug",
    [
        pytest.param("fake-tenant\n", id="trailing-newline"),
        pytest.param("fake-tenant\r\n", id="trailing-crlf"),
        pytest.param("fake-tenant ", id="trailing-space"),
        pytest.param("Fake-Tenant", id="upper-case"),
        pytest.param("fake_tenant", id="underscore"),
        pytest.param("", id="empty"),
    ],
)
def test_a_slug_that_is_not_a_slug_has_no_cache_name(slug: str) -> None:
    """Catches a name built from a slug nobody checked: a slug with an
    underscore could clash with a policy collection name, and one with a
    trailing newline, CRLF or space would name a different collection than the
    tenant's own on the next lookup."""
    with pytest.raises(InvalidPolicyNameError):
        search_cache.cache_collection_name(slug)


# --- errors -------------------------------------------------------------------


def test_the_cache_name_never_equals_a_policy_name() -> None:
    """Catches a cache that shares a policy collection (no `cache_` marker): a
    drop on ingest would then delete policy chunks, or a lookup would read
    them. Slugs hold no underscore, so `cache_` can never be a slug's start."""
    assert search_cache.cache_collection_name("fake-tenant") != collection_name("fake-tenant")
    assert search_cache.cache_collection_name("fake-tenant").endswith("cache_fake-tenant")


def test_a_store_that_cannot_be_reached_raises_the_typed_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a cache module that lets a raw `qdrant_client` error out: the
    tool and the pipeline both catch or translate the typed error, and a raw
    one would reach a caller as an unknown failure (and could carry the
    store's URL). Every one of the four calls is tried on a closed port."""
    monkeypatch.setenv("QDRANT_URL", CLOSED_PORT_URL)
    monkeypatch.setenv("QDRANT_TIMEOUT_SECONDS", "2")
    tenant_id = uuid.uuid4()
    slug = "fake-cache-unreachable"

    async def work(client: AsyncQdrantClient) -> list[str]:
        raised: list[str] = []
        calls: dict[str, Callable[[], Awaitable[Any]]] = {
            "current_generation": lambda: search_cache.current_generation(client, slug),
            "lookup": lambda: _lookup(client, slug, tenant_id, "any-generation"),
            "save": lambda: _save(client, slug, tenant_id, generation="any-generation"),
            "drop": lambda: search_cache.drop(client, slug),
        }
        for label, call in calls.items():
            try:
                await call()
            except VectorStoreUnavailableError:
                raised.append(label)
        return raised

    assert _drive(work) == ["current_generation", "lookup", "save", "drop"]
