"""`search_policy` with the search cache in front of it, over the real transport.

Expected implementation surface (the implementer must provide exactly this):

``helpdesk.mcp_server.services.rag.search_cache`` — read by the tool as a
    MODULE (``search_cache.lookup(...)``), so this file replaces
    ``current_generation``, ``lookup``, ``save`` and ``drop`` with fakes on the
    module object and no case here touches Qdrant for the cache.
``SearchPolicyOutput.cache`` — ``"hit"`` or ``"miss"``.
``Settings.policy_search_cache`` — ``bool``, on when ``POLICY_SEARCH_CACHE`` is missing.
``metrics.record_policy_cache(tenant=..., result=...)`` moving
    ``helpdesk_policy_cache_total{tenant,result}`` once per call that reaches
    the search.

The policy store is a `RecordingQdrant` (no compose Qdrant), the embedder a
`RecordingEmbedder`, the clock a fixed one threaded through `dispatch`, and the
metrics a registry of the case's own. The suite runs with the cache OFF, so
each case that wants it switches it on through the environment.

No pytest-asyncio: each case is a sync function driving one `asyncio.run`.
"""

import uuid
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from typing import Any

import pytest
from prometheus_client import CollectorRegistry

from helpdesk.mcp_server.services.rag import search_cache
from helpdesk.mcp_server.services.rag.retrieval import SearchHit
from helpdesk.mcp_server.services.rag.vectors import VectorStoreUnavailableError
from helpdesk.mcp_server.tools import dispatch as pipeline
from helpdesk.shared.core.config import load_settings
from helpdesk.shared.core.observability import metrics
from tests.mcp_server.test_mcp_search_policy import QUERY, SEARCH, _install, _plant, _structured
from tests.support.mcp import (
    PlantedDocument,
    RecordingQdrant,
    SeededMcp,
    bearer,
    call_tool,
    dense_vector,
    stub_point,
    token_with_claims,
)

FIXED_NOW = datetime(2031, 3, 14, 15, 9, 26, tzinfo=UTC)
GENERATION = "fake-generation-0001"
COUNTER = "helpdesk_policy_cache_total"


class _FakeClock:
    """A `datetime` whose `now()` is fixed; the rest proxies to the real one."""

    def now(self, tz: Any = None) -> datetime:
        return FIXED_NOW

    def __getattr__(self, name: str) -> Any:
        return getattr(datetime, name)


class FakeCache:
    """The four cache calls, recorded, with failures and answers programmed."""

    def __init__(
        self,
        *,
        found: list[SearchHit] | None = None,
        failing: Mapping[str, Exception] | None = None,
    ) -> None:
        self.found = found
        self.failing = dict(failing or {})
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def _record(self, name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        self.calls.append((name, args, kwargs))
        if name in self.failing:
            raise self.failing[name]

    async def current_generation(self, *args: Any, **kwargs: Any) -> str:
        self._record("current_generation", args, kwargs)
        return GENERATION

    async def lookup(self, *args: Any, **kwargs: Any) -> list[SearchHit] | None:
        self._record("lookup", args, kwargs)
        return self.found

    async def save(self, *args: Any, **kwargs: Any) -> None:
        self._record("save", args, kwargs)

    async def drop(self, *args: Any, **kwargs: Any) -> None:
        self._record("drop", args, kwargs)

    def names(self) -> list[str]:
        return [name for name, _args, _kwargs in self.calls]

    def only(self, name: str) -> tuple[tuple[Any, ...], dict[str, Any]]:
        matching = [(args, kwargs) for call, args, kwargs in self.calls if call == name]
        assert len(matching) == 1, f"{name} was called {len(matching)} times"
        return matching[0]

    def install(self, monkeypatch: pytest.MonkeyPatch) -> "FakeCache":
        for name in ("current_generation", "lookup", "save", "drop"):
            monkeypatch.setattr(search_cache, name, getattr(self, name))
        return self


@pytest.fixture
def cache_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cache switched on for this case (the suite default is off)."""
    monkeypatch.setenv("POLICY_SEARCH_CACHE", "true")


@pytest.fixture
def fresh_registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[CollectorRegistry]:
    """A metrics registry of this case's own, installed over the module pair."""
    registry = CollectorRegistry()
    monkeypatch.setattr(metrics, "REGISTRY", registry)
    monkeypatch.setattr(metrics, "METRICS", metrics.build_metrics(registry))
    yield registry


def _search(
    env: SeededMcp, monkeypatch: pytest.MonkeyPatch, query: str = QUERY, top_k: int | None = None
) -> dict[str, Any]:
    """One `search_policy` call by tenant A at the fixed instant."""
    monkeypatch.setattr(pipeline, "datetime", _FakeClock())
    token = token_with_claims(env.tenant_a, {"sid": str(uuid.uuid4())}, now=FIXED_NOW)
    arguments: dict[str, Any] = {"query": query}
    if top_k is not None:
        arguments["top_k"] = top_k
    return _structured(call_tool(bearer(token), SEARCH, arguments))


def _planted(env: SeededMcp) -> tuple[PlantedDocument, RecordingQdrant]:
    """A policy document in Postgres and a store that finds its chunks."""
    planted = _plant(
        env,
        env.tenant_a,
        ("Fake chunk zero about badges.", "Fake chunk one."),
        title="Fake Badge Policy",
    )
    points = [
        stub_point(
            chunk_id=planted.chunk_ids[index],
            document_id=planted.document_id,
            document_slug=planted.slug,
            text=planted.texts[index],
            score=score,
        )
        for index, score in enumerate((0.9, 0.8))
    ]
    return planted, RecordingQdrant(dense=points, sparse=[points[1], points[0]])


def _saved_page(planted: PlantedDocument) -> list[SearchHit]:
    """A page as the cache would hand it back, with scores nothing else makes."""
    return [
        SearchHit(
            chunk_id=planted.chunk_ids[1],
            document_id=planted.document_id,
            document_slug=planted.slug,
            version=planted.version,
            heading_path=("1. Purpose",),
            text=planted.texts[1],
            score=0.0123,
            dense_score=0.9,
        )
    ]


def _total(registry: CollectorRegistry) -> float:
    """Every sample of the counter, added up across label sets."""
    return sum(
        sample.value
        for family in registry.collect()
        for sample in family.samples
        if sample.name == COUNTER
    )


# --- hit and miss -------------------------------------------------------------


@pytest.mark.usefixtures("cache_on")
def test_a_hit_returns_the_saved_page_without_searching_policies(
    mcp_env: SeededMcp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a tool that looks in the cache and then searches anyway, that
    builds the sparse vector before it knows it needs it, or that embeds the
    question twice. A hit must report `hit`, answer from the SAVED page (its
    chunk and its score, titles still read from Postgres), touch no policy
    collection, and embed dense once and sparse never. It also pins what the
    lookup is asked: the tenant from the token, the dense vector, `top_k`, the
    generation read first and the tool's own clock."""
    planted, store = _planted(mcp_env)
    model = _install(monkeypatch, store)
    cache = FakeCache(found=_saved_page(planted)).install(monkeypatch)

    payload = _search(mcp_env, monkeypatch)

    assert payload["cache"] == "hit"
    assert payload["refused"] is False
    assert [(c["text"], c["title"], c["score"]) for c in payload["citations"]] == [
        (planted.texts[1], planted.title, 0.0123)
    ]
    assert store.calls == []
    assert model.calls == ["embed_dense"]
    assert cache.names() == ["current_generation", "lookup"]
    args, kwargs = cache.only("lookup")
    assert args[1] == mcp_env.tenant_a.tenant_slug
    assert kwargs["tenant_id"] == mcp_env.tenant_a.tenant_id
    assert list(kwargs["vector"]) == list(dense_vector())
    assert kwargs["top_k"] == 6
    assert kwargs["generation"] == GENERATION
    assert kwargs["now"] == FIXED_NOW


@pytest.mark.usefixtures("cache_on")
def test_a_miss_searches_saves_and_reports_miss(
    mcp_env: SeededMcp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a tool that does not save what it found, saves a different page
    than it returns, searches with a second dense embedding, or stamps the entry
    with something other than the generation it read before searching. The
    page is the one a search gives today; the single save carries that same
    page in the same order, the question's vector, `top_k`, the tenant and the
    clock."""
    planted, store = _planted(mcp_env)
    model = _install(monkeypatch, store)
    cache = FakeCache(found=None).install(monkeypatch)

    payload = _search(mcp_env, monkeypatch, top_k=4)

    assert payload["cache"] == "miss"
    assert [c["chunk_id"] for c in payload["citations"]] == [
        str(planted.chunk_ids[0]),
        str(planted.chunk_ids[1]),
    ]
    assert model.calls == ["embed_dense", "embed_sparse"]
    assert len(store.calls) == 2
    assert cache.names() == ["current_generation", "lookup", "save"]
    _args, saved = cache.only("save")
    assert [hit.chunk_id for hit in saved["hits"]] == list(planted.chunk_ids)
    assert [round(hit.score, 6) for hit in saved["hits"]] == [
        round(c["score"], 6) for c in payload["citations"]
    ]
    assert list(saved["vector"]) == list(dense_vector())
    assert saved["top_k"] == 4
    assert saved["generation"] == GENERATION
    assert saved["tenant_id"] == mcp_env.tenant_a.tenant_id
    assert saved["now"] == FIXED_NOW


@pytest.mark.usefixtures("cache_on")
@pytest.mark.parametrize("stage", ["current_generation", "lookup"])
@pytest.mark.parametrize(
    "error",
    [
        pytest.param(VectorStoreUnavailableError("down"), id="typed-store-error"),
        pytest.param(RuntimeError("fake boom"), id="any-other-error"),
        pytest.param(ValueError("fake unparsable page"), id="unparsable-page"),
    ],
)
def test_a_cache_lookup_error_is_a_miss_and_the_search_still_answers(
    mcp_env: SeededMcp, monkeypatch: pytest.MonkeyPatch, stage: str, error: Exception
) -> None:
    """Catches a tool that lets a cache failure fail the search, or that only
    catches the typed store error (a saved page that does not parse arrives as
    a plain `ValueError`). Either read failing, the page is still the full
    search's, reported `miss`."""
    planted, store = _planted(mcp_env)
    _install(monkeypatch, store)
    FakeCache(failing={stage: error}).install(monkeypatch)

    payload = _search(mcp_env, monkeypatch)

    assert payload["cache"] == "miss"
    assert payload["refused"] is False
    assert [c["chunk_id"] for c in payload["citations"]] == [
        str(planted.chunk_ids[0]),
        str(planted.chunk_ids[1]),
    ]
    assert len(store.calls) == 2


@pytest.mark.usefixtures("cache_on")
@pytest.mark.parametrize(
    "error",
    [
        pytest.param(VectorStoreUnavailableError("down"), id="typed-store-error"),
        pytest.param(RuntimeError("fake boom"), id="any-other-error"),
    ],
)
def test_a_cache_write_error_still_answers(
    mcp_env: SeededMcp, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """Catches a tool that lets a failed save fail a search it already
    finished: the person asked a question and has the answer in hand. The page
    is returned whole and reported `miss`; the save was tried."""
    planted, store = _planted(mcp_env)
    _install(monkeypatch, store)
    cache = FakeCache(failing={"save": error}).install(monkeypatch)

    payload = _search(mcp_env, monkeypatch)

    assert payload["cache"] == "miss"
    assert [c["chunk_id"] for c in payload["citations"]] == [
        str(planted.chunk_ids[0]),
        str(planted.chunk_ids[1]),
    ]
    assert "save" in cache.names()


# --- when the cache is not used ------------------------------------------------


@pytest.mark.usefixtures("cache_on")
@pytest.mark.parametrize(
    ("query", "cached"),
    [
        pytest.param("a" * 15, False, id="15-characters"),
        pytest.param("a" * 16, True, id="16-characters"),
        pytest.param("   " + "a" * 15 + "   ", False, id="15-characters-padded-with-spaces"),
        pytest.param("  " + "a" * 16 + "  ", True, id="16-characters-padded-with-spaces"),
    ],
)
def test_a_15_character_question_is_not_cached_and_16_is(
    mcp_env: SeededMcp, monkeypatch: pytest.MonkeyPatch, query: str, cached: bool
) -> None:
    """Catches a length test on the raw text (padding would push a 15-character
    question over the line), an off-by-one in either direction (`<= 16`,
    `< 15`), and a short question that still reads or writes the cache. After
    trimming, 15 makes no cache call of any kind; 16 looks up and saves. Both
    still search and report `miss`."""
    _planted_doc, store = _planted(mcp_env)
    _install(monkeypatch, store)
    cache = FakeCache(found=None).install(monkeypatch)

    payload = _search(mcp_env, monkeypatch, query=query)

    assert payload["cache"] == "miss"
    assert len(store.calls) == 2
    if cached:
        assert cache.names() == ["current_generation", "lookup", "save"]
    else:
        assert cache.calls == []


def test_with_the_switch_off_the_cache_is_never_called(
    mcp_env: SeededMcp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a tool that ignores `POLICY_SEARCH_CACHE`: with it false there
    must be no lookup, no generation read and no save, the search runs as
    before (dense and sparse each once) and the answer says `miss`. The cache
    would otherwise answer from a hit the switch had forbidden."""
    monkeypatch.setenv("POLICY_SEARCH_CACHE", "false")
    planted, store = _planted(mcp_env)
    model = _install(monkeypatch, store)
    cache = FakeCache(found=_saved_page(planted)).install(monkeypatch)

    payload = _search(mcp_env, monkeypatch)

    assert payload["cache"] == "miss"
    assert cache.calls == []
    assert model.calls == ["embed_dense", "embed_sparse"]
    assert len(payload["citations"]) == 2


def test_the_switch_is_on_when_the_name_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Catches a default of `False`: a deployment that never set the name would
    run with no cache and nobody would see why. The environment name is
    `POLICY_SEARCH_CACHE`, and `false` turns it off."""
    monkeypatch.delenv("POLICY_SEARCH_CACHE", raising=False)
    assert load_settings().policy_search_cache is True

    monkeypatch.setenv("POLICY_SEARCH_CACHE", "false")
    assert load_settings().policy_search_cache is False


# --- the counter -------------------------------------------------------------


@pytest.mark.usefixtures("cache_on")
def test_each_search_counts_once_by_tenant_and_result(
    mcp_env: SeededMcp, monkeypatch: pytest.MonkeyPatch, fresh_registry: CollectorRegistry
) -> None:
    """Catches a counter that is not moved, moved twice per call, moved on the
    wrong result, or labelled with the slug or the user instead of the tenant
    id. One hit then one miss: each cell is 1 and the whole counter is 2."""
    planted, store = _planted(mcp_env)
    _install(monkeypatch, store)
    cache = FakeCache(found=_saved_page(planted)).install(monkeypatch)
    tenant = str(mcp_env.tenant_a.tenant_id)

    _search(mcp_env, monkeypatch)
    cache.found = None
    _search(mcp_env, monkeypatch)

    assert fresh_registry.get_sample_value(COUNTER, {"tenant": tenant, "result": "hit"}) == 1.0
    assert fresh_registry.get_sample_value(COUNTER, {"tenant": tenant, "result": "miss"}) == 1.0
    assert _total(fresh_registry) == 2.0


@pytest.mark.parametrize(
    ("switch", "query", "found", "expected"),
    [
        pytest.param("true", QUERY, True, "hit", id="a-hit"),
        pytest.param("true", QUERY, False, "miss", id="a-miss"),
        pytest.param("true", "a" * 15, False, "miss", id="a-short-question"),
        pytest.param("false", QUERY, False, "miss", id="the-switch-off"),
    ],
)
def test_every_call_that_reaches_the_search_counts_exactly_once(
    mcp_env: SeededMcp,
    monkeypatch: pytest.MonkeyPatch,
    fresh_registry: CollectorRegistry,
    switch: str,
    query: str,
    found: bool,
    expected: str,
) -> None:
    """Catches a counter that skips the calls that never touched the cache (a
    short question, the switch off): they are misses and must be counted, once.
    The whole counter is read, so a second increment on any series shows."""
    monkeypatch.setenv("POLICY_SEARCH_CACHE", switch)
    planted, store = _planted(mcp_env)
    _install(monkeypatch, store)
    FakeCache(found=_saved_page(planted) if found else None).install(monkeypatch)

    payload = _search(mcp_env, monkeypatch, query=query)

    assert payload["cache"] == expected
    tenant = str(mcp_env.tenant_a.tenant_id)
    assert fresh_registry.get_sample_value(COUNTER, {"tenant": tenant, "result": expected}) == 1.0
    assert _total(fresh_registry) == 1.0
