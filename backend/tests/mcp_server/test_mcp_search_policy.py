"""An earlier change §1 and §6: the `search_policy` MCP tool, driven over the real transport.

Surface under test (written before it exists; the implementer must provide
exactly this):

``mcp_server.rag_clients`` (new)
    ``qdrant(settings: Settings) -> AsyncQdrantClient`` — the pooled client for
    the RUNNING EVENT LOOP, keyed by ``(loop, url)`` the way
    ``redis_client._redis`` is. Two calls in one loop are the same object; two
    loops get two clients.
    ``_build_embedder() -> Embedder`` — the uncached constructor, and the seam
    a test fakes so no case in this suite pays an ONNX model load.
    ``embedder() -> Embedder`` — ``_build_embedder`` cached once per PROCESS
    (it holds no connection and no loop affinity).
    ``close_loop_clients() -> None`` — closes and forgets the running loop's
    clients.
    Both accessors are read through the MODULE attribute by the handler
    (``rag_clients.qdrant(...)``, ``rag_clients.embedder()``), the way
    ``main.py`` reads ``readiness.check_*``, so a test can substitute either.

``mcp_server.tools.policy`` (new)
    ``SearchPolicyInput`` — ``query: str``, ``top_k: int = TOP_K_DEFAULT``,
    extras IGNORED (``_IGNORE_EXTRAS``): a ``tenant_id`` argument is dropped,
    never honoured and never rejected.
    ``PolicyCitation`` — exactly ``document`` (the document SLUG), ``title``,
    ``version``, ``heading_path``, ``chunk_id``, ``text``, ``score``.
    ``SearchPolicyOutput`` — ``citations: list[PolicyCitation]`` and
    ``refused: bool``.
    ``search_policy(session, identity, args)`` — ``retrieval.search_hits``,
    then ``should_refuse``, then ``resolve_citations`` ONLY when the page is
    not refused. The collection comes from ``identity.tenant_id`` → the
    tenant's slug → ``vectors.collection_name``.

``mcp_server.registry.REGISTRY["search_policy"]``
    ``tool=rbac.Tool.SEARCH_POLICY``, ``mutating=False``,
    ``event_type=SUCCESS_EVENT_TYPE``.

``helpdesk.core.repository.find_tenant_by_id(session, tenant_id) -> Tenant | None``

a project rule: Postgres, Redis and Qdrant are `mcp_fixtures`' session-scoped
ones, none of them rebuilt. Per-test isolation is a namespace — a
uuid-suffixed document slug inside the session's collections, and a fresh
`sid` claim per call so the audit rows one case wrote are the only ones it
reads.

Most cases here drive a `RecordingQdrant` rather than the compose instance.
That is not a convenience: a programmed page is the only way to make the FUSED
order differ from both arms' own orders, to put a chosen cosine either side of
the refusal floor, and to prove that a refused argument built no request at
all. The real store is driven end to end by `test_mcp_policy_isolation.py`.

No pytest-asyncio: each case is a sync function driving one `asyncio.run`.
"""

import asyncio
import uuid
from typing import Any

import pytest
from mcp_fixtures import (  # noqa: F401 - fixtures are resolved by name
    PlantedDocument,
    RecordingEmbedder,
    RecordingQdrant,
    SeededMcp,
    SeededUser,
    as_client,
    bearer,
    call_tool,
    error_code,
    list_tools,
    mcp_env,
    mcp_pg,
    mcp_redis_base_url,
    mcp_redis_url,
    plant_policy_document,
    seeded_mcp_db,
    stub_point,
    token_with_claims,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from helpdesk.mcp_server import rag_clients
from helpdesk.mcp_server.registry import REGISTRY, SUCCESS_EVENT_TYPE
from helpdesk.shared.auth import rbac
from helpdesk.shared.core.db import create_engine, create_session_factory, tenant_session
from helpdesk.shared.core.settings import load_settings
from helpdesk.shared.rag.embedding import Embedder
from helpdesk.shared.rag.naming import MAX_QUERY_CHARS, MAX_TOP_K
from helpdesk.shared.rag.retrieval import REFUSAL_THRESHOLD, RRF_K, TOP_K_DEFAULT
from helpdesk.shared.rag.vectors import collection_name

SEARCH = "search_policy"

# The audit contract as literals, never imports: reading these strings from the
# module under test would let a rename pass unnoticed (the same reason
# `test_mcp_search_audit.py` writes them out).
SUCCEEDED_EVENT = "tool.succeeded"
FAILED_EVENT = "tool.failed"

# the design gives `search_policy` every principal at `Scope.ANY` (seven cells,
# counted off `rbac.Principal` below rather than carried over from another
# plan's row, a project rule).
PRINCIPALS = tuple(principal.value for principal in rbac.Principal)

# Exactly the seven fields plan §1 fixes for a citation on the wire. An eighth
# is 15a's internal dense cosine reaching a prompt; a missing one is a citation
# the UI cannot render.
CITATION_FIELDS = frozenset(
    {"document", "title", "version", "heading_path", "chunk_id", "text", "score"}
)

# A closed port: connection refused before any timeout can matter.
CLOSED_PORT_URL = "http://127.0.0.1:1"

QUERY = "fake question about spare badges"


# --- driving the tool ----------------------------------------------------------


def _structured(result: Any) -> dict[str, Any]:
    assert result.is_error is False, result.content
    assert result.structured_content is not None, "a successful call carries structured content"
    payload: dict[str, Any] = result.structured_content
    return payload


def _install(monkeypatch: pytest.MonkeyPatch, store: RecordingQdrant | None) -> RecordingEmbedder:
    """Substitute the two process-level clients the handler reaches for.

    `store=None` leaves the REAL accessor in place (the unreachable-store case
    needs the client the settings name); the embedder is always faked, because
    no case in this file may pay an ONNX load.
    """
    model = RecordingEmbedder()
    monkeypatch.setattr(rag_clients, "embedder", lambda: model)
    if store is not None:
        monkeypatch.setattr(rag_clients, "qdrant", lambda settings: as_client(store))
    return model


def _plant(
    env: SeededMcp, user: SeededUser, texts: tuple[str, ...], *, title: str
) -> PlantedDocument:
    """One policy version in Postgres for this case, under its own slug."""
    slug = f"fake-policy-{uuid.uuid4().hex[:8]}"

    async def scenario() -> PlantedDocument:
        engine = create_engine(env.database_url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, user.tenant_id) as session:
                return await plant_policy_document(
                    session,
                    tenant_slug=user.tenant_slug,
                    tenant_id=user.tenant_id,
                    slug=slug,
                    texts=texts,
                    title=title,
                )
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _audit_rows(database_url: str, session_id: uuid.UUID) -> list[Any]:
    """Every audit event of one tool call, found by the token's own `sid`.

    The database is shared for the session, so the `sid` claim is this case's
    namespace: a count of "one event" over the whole table would be a count of
    every case that ran before it.
    """

    async def scenario() -> list[Any]:
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as conn:
                return list(
                    await conn.execute(
                        text(
                            "SELECT event_type, subject_type, subject_id, payload"
                            " FROM audit_events WHERE session_id = :sid ORDER BY seq"
                        ),
                        {"sid": str(session_id)},
                    )
                )
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _call(
    user: SeededUser, arguments: dict[str, Any], *, session_id: uuid.UUID | None = None
) -> Any:
    token = token_with_claims(user, {"sid": str(session_id or uuid.uuid4())})
    return call_tool(bearer(token), SEARCH, arguments)


# --- registration (criterion 1) ------------------------------------------------


def test_the_registry_row_is_the_matrix_tool_and_is_not_mutating() -> None:
    """Catches a row registered under a different matrix member — which would
    silently give the tool another row's principals — and one marked
    `mutating=True`, which would push a read through `run_once` and demand an
    idempotency key the design does not give it."""
    spec = REGISTRY[SEARCH]

    assert spec.name == rbac.Tool.SEARCH_POLICY.value
    assert spec.tool is rbac.Tool.SEARCH_POLICY
    assert spec.mutating is False
    assert spec.event_type == SUCCESS_EVENT_TYPE


def test_search_policy_is_advertised_with_a_default_page_of_six(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches an input model whose `top_k` default is any other number, read
    off the schema a CLIENT sees: an agent that omits `top_k` gets whatever
    this default says, so a wrong one is a wrong page for every caller and is
    invisible to a case that passes `top_k` explicitly."""
    result = list_tools(bearer(token_with_claims(mcp_env.tenant_a, {})))
    advertised = {tool.name: tool for tool in result.tools}

    assert SEARCH in advertised, "search_policy is not on the wire at all"
    schema = advertised[SEARCH].input_schema
    assert schema["properties"]["top_k"]["default"] == 6
    assert schema["required"] == ["query"]


@pytest.mark.parametrize("principal", PRINCIPALS)
def test_every_principal_may_search_policy(
    mcp_env: SeededMcp,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    principal: str,
) -> None:
    """Catches a registry row pointing at a narrower matrix member: the design
    gives this row all seven principals at `Scope.ANY`, so a single
    `FORBIDDEN` here is a tool an employee cannot use to read policy.

    The caller holds ONE principal, so a pass cannot come from a second, wider
    cell the seeded user happens to carry."""
    _install(monkeypatch, RecordingQdrant())
    token = token_with_claims(
        mcp_env.tenant_a, {"principals": [principal], "sid": str(uuid.uuid4())}
    )

    result = call_tool(bearer(token), SEARCH, {"query": QUERY})

    assert result.is_error is False, error_code(result)


# --- the page (criteria 3 and 4) -----------------------------------------------


def test_a_page_carries_exactly_the_seven_citation_fields_in_fused_order(
    mcp_env: SeededMcp,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches three wrong implementations at once.

    A citation set that is not exactly the seven fields (an eighth is 15a's
    internal `dense_score` reaching a prompt; a `document_slug` key instead of
    `document` is 15a's dataclass forwarded unrenamed).

    A page in the DENSE arm's order — that is, a tool that issues one arm or
    lets the store fuse: the two arms are programmed so the fused order
    (`c0, c2, c1`) equals neither arm's own order.

    A `score` copied from the arm rather than the fused one: the first
    citation's score is the RRF sum `1/(k+1) + 1/(k+2)`, not the 0.9 the dense
    arm returned.
    """
    planted = _plant(
        mcp_env,
        mcp_env.tenant_a,
        ("Fake chunk zero about badges.", "Fake chunk one.", "Fake chunk two."),
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
        for index, score in enumerate((0.9, 0.8, 0.7))
    ]
    # dense: c0, c1, c2 · sparse: c2, c0, c1 · fused: c0, c2, c1.
    store = RecordingQdrant(dense=points, sparse=[points[2], points[0], points[1]])
    _install(monkeypatch, store)

    payload = _structured(_call(mcp_env.tenant_a, {"query": QUERY}))

    assert payload["refused"] is False
    citations = payload["citations"]
    assert [citation["text"] for citation in citations] == [
        planted.texts[0],
        planted.texts[2],
        planted.texts[1],
    ]
    for citation in citations:
        assert set(citation) == CITATION_FIELDS, citation
        assert citation["document"] == planted.slug
        assert citation["title"] == planted.title
        assert citation["version"] == planted.version
    assert citations[0]["chunk_id"] == str(planted.chunk_ids[0])
    assert citations[0]["heading_path"] == ["1. Purpose"]
    assert citations[0]["score"] == pytest.approx(1.0 / (RRF_K + 1) + 1.0 / (RRF_K + 2))


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        pytest.param({"query": QUERY}, 6, id="the-default-page-is-six"),
        pytest.param({"query": QUERY, "top_k": 1}, 1, id="the-smallest-page"),
        pytest.param({"query": QUERY, "top_k": 4}, 4, id="a-page-in-between"),
    ],
)
def test_the_page_is_never_longer_than_top_k(
    mcp_env: SeededMcp,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    arguments: dict[str, Any],
    expected: int,
) -> None:
    """Catches a handler that passes `top_k` to the arms but never cuts the
    fused page (fusion of two 10-point arms returns 10 whatever the arms were
    asked for), and a default other than 6 — the store is programmed with more
    points than any of these pages, so the length can only come from the cut."""
    planted = _plant(
        mcp_env,
        mcp_env.tenant_a,
        tuple(f"Fake chunk {index} about badges." for index in range(10)),
        title="Fake Long Policy",
    )
    points = [
        stub_point(
            chunk_id=planted.chunk_ids[index],
            document_id=planted.document_id,
            document_slug=planted.slug,
            text=planted.texts[index],
            score=0.9 - index / 100.0,
        )
        for index in range(10)
    ]
    _install(monkeypatch, RecordingQdrant(dense=points, sparse=list(reversed(points))))

    payload = _structured(_call(mcp_env.tenant_a, arguments))

    assert payload["refused"] is False
    assert len(payload["citations"]) == expected


# --- the collection comes from the token (criterion 2) -------------------------


def test_the_collection_searched_is_the_tokens_and_an_extra_tenant_id_is_dropped(
    mcp_env: SeededMcp,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a handler that names the collection from an ARGUMENT.

    a project rule with no second guard behind it: one collection per tenant IS
    the isolation (the design), so nothing downstream would catch a search of
    the collection the argument named — the points would simply come back. The
    recorded collection name is therefore asserted directly, on both arms.

    It also catches an input model with `extra="forbid"`: a dropped `tenant_id`
    must leave the call SUCCEEDING, not turn into `VALIDATION`.

    Both slugs are resolved from the handles the seeding fixture read back, never from a slug pinned by name here.
    """
    caller = mcp_env.tenant_a
    other = mcp_env.tenant_b
    assert caller.tenant_slug != other.tenant_slug, "the two seeded tenants share a slug"
    store = RecordingQdrant()
    _install(monkeypatch, store)

    result = _call(caller, {"query": QUERY, "tenant_id": str(other.tenant_id)})

    assert result.is_error is False, error_code(result)
    assert store.collections == [
        collection_name(caller.tenant_slug),
        collection_name(caller.tenant_slug),
    ], store.collections
    assert collection_name(other.tenant_slug) not in store.collections


# --- input validation (criterion 5, a project rule and 2) --------------------


def test_the_search_bounds_are_exactly_their_planned_values() -> None:
    """Catches a cap or a floor of any other size. Each bounds a value that
    reaches a service with no backstop of its own — a Qdrant `limit`, a model
    input truncated at 512 tokens — and the threshold decides whether an
    unanswerable question is answered anyway, so a band would admit the wrong
    number in every case."""
    assert MAX_TOP_K == 50
    assert MAX_QUERY_CHARS == 1000
    assert TOP_K_DEFAULT == 6
    assert REFUSAL_THRESHOLD == 0.35


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param({"query": QUERY, "top_k": 1}, id="top-k-one"),
        pytest.param({"query": QUERY, "top_k": 50}, id="top-k-exactly-the-cap"),
        pytest.param({"query": "f" * 1000}, id="a-query-of-exactly-the-cap"),
    ],
)
def test_the_inclusive_boundary_arguments_are_accepted(
    mcp_env: SeededMcp,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    arguments: dict[str, Any],
) -> None:
    """Catches an exclusive comparison at any of the three edges (`> 1`,
    `< MAX_TOP_K`, `>= MAX_QUERY_CHARS`): each turns a legal call into a
    `VALIDATION` refusal, and only a case sitting exactly ON the edge can tell
    the two comparisons apart."""
    store = RecordingQdrant()
    model = _install(monkeypatch, store)

    result = _call(mcp_env.tenant_a, arguments)

    assert result.is_error is False, error_code(result)
    # Read off the recorders: the accepted call really did reach both, so the
    # pass is an accepted search and not an early return.
    assert model.calls == ["embed_dense", "embed_sparse"]
    assert len(store.calls) == 2


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param({"query": QUERY, "top_k": 0}, id="an-empty-page"),
        pytest.param({"query": QUERY, "top_k": 51}, id="one-over-the-page-cap"),
        pytest.param({"query": "f" * 1001}, id="one-character-over-the-query-cap"),
        pytest.param({"query": ""}, id="the-empty-query"),
    ],
)
def test_a_refused_argument_reaches_neither_the_embedder_nor_the_store(
    mcp_env: SeededMcp,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    arguments: dict[str, Any],
) -> None:
    """Catches a handler that forwards the argument to the store or the model
    before checking it: 0 and 51 arrive as a Qdrant `limit` it interprets
    itself, a 1001-character query as an input the model silently truncates,
    and the empty string as a zero vector that sits near every chunk.

    The zero-call assertions are what distinguish "refused" from "refused
    before any effect" — an exception alone proves only the first.

    Rule 20, stated rather than claimed: where the input model also declares
    the same bound, that guard and 15a's `check_top_k`/`check_query` refuse the
    same row and neither is separately killable. What this case pins is the
    PAIR and the zero calls, which no single guard's deletion leaves standing.
    """
    store = RecordingQdrant()
    model = _install(monkeypatch, store)

    result = _call(mcp_env.tenant_a, arguments)

    assert error_code(result) == "VALIDATION"
    assert store.calls == [], "the refused call reached the store"
    assert model.calls == [], "the refused call reached the embedder"


# --- the refusal (criterion 4, plan §5) ----------------------------------------


@pytest.mark.parametrize(
    ("best_cosine", "refused"),
    [
        pytest.param(0.35, False, id="exactly-the-threshold-answers"),
        pytest.param(0.3499, True, id="one-hundredth-of-a-percent-below-refuses"),
    ],
)
def test_the_refusal_decision_table_at_the_tool_boundary(
    mcp_env: SeededMcp,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    best_cosine: float,
    refused: bool,
) -> None:
    """Catches a threshold read the wrong way round at the boundary (`>` where
    the design is inclusive on the ANSWERING side) and a `refused` flag set
    from anything but `should_refuse` over the fused page.

    The pair straddles 0.35 by a ten-thousandth, so a one-sided case cannot
    tell `>=` from `>`; and the refusing row must carry NO citations, which is
    the failure requirement #14 exists to prevent — a page below the floor that
    row 17 could still answer from.
    """
    planted = _plant(
        mcp_env, mcp_env.tenant_a, ("Fake chunk about badges.",), title="Fake Threshold Policy"
    )
    point = stub_point(
        chunk_id=planted.chunk_ids[0],
        document_id=planted.document_id,
        document_slug=planted.slug,
        text=planted.texts[0],
        score=best_cosine,
    )
    _install(monkeypatch, RecordingQdrant(dense=[point], sparse=[point]))

    payload = _structured(_call(mcp_env.tenant_a, {"query": QUERY}))

    assert payload["refused"] is refused
    if refused:
        assert payload["citations"] == []
    else:
        assert len(payload["citations"]) == 1


def test_a_refused_page_never_pays_the_title_query(
    mcp_env: SeededMcp,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a handler that resolves citations FIRST and blanks them after.

    The programmed points name a `document_id` with no `policy_documents` row,
    so `resolve_citations` cannot run without raising
    `CitationUnresolvableError` (`INTERNAL`). The refusing call must therefore
    succeed, which it can only do by never asking for the titles.

    The second half is the premise, not decoration: the SAME unresolvable page
    with a cosine above the floor must answer `INTERNAL`. Without it the first
    half would pass against a tool that never resolves titles at all.
    """
    ghost = uuid.uuid4()
    below = stub_point(
        chunk_id=uuid.uuid4(),
        document_id=ghost,
        document_slug="fake-ghost-policy",
        text="Fake chunk whose document row is gone.",
        score=0.30,
    )
    above = stub_point(
        chunk_id=uuid.uuid4(),
        document_id=ghost,
        document_slug="fake-ghost-policy",
        text="Fake chunk whose document row is gone.",
        score=0.95,
    )

    _install(monkeypatch, RecordingQdrant(dense=[below], sparse=[below]))
    refused = _structured(_call(mcp_env.tenant_a, {"query": QUERY}))

    _install(monkeypatch, RecordingQdrant(dense=[above], sparse=[above]))
    answered = _call(mcp_env.tenant_a, {"query": QUERY})

    assert refused["refused"] is True
    assert refused["citations"] == []
    assert error_code(answered) == "INTERNAL", (
        "the premise: this page's titles are unresolvable, so an answering call must raise"
    )


# --- the store is down (criterion 6) -------------------------------------------


def test_an_unreachable_store_is_one_typed_internal(
    mcp_env: SeededMcp,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a handler that swallows a store outage into an empty, refused
    page — which would tell a user "no policy matches" throughout an outage —
    and one that reports the outage as the caller's fault (`VALIDATION`).

    The real `rag_clients.qdrant` is used, against a closed port, so the client
    the settings name is the one that fails.

    No kill claim for "the message names neither the store nor its URL": the
    pipeline answers with the bare code for EVERY failure, so that half is
    pinned by `test_mcp_pipeline.py` and nothing about this tool could make it
    false. `error_code` still reads the whole body, so a code with an
    explanation appended fails here.
    """
    # The premise, read off the registry: an UNREGISTERED
    # tool name also answers INTERNAL, so without this the case would be green
    # before the tool exists and would pin nothing about the store at all.
    assert SEARCH in REGISTRY, "search_policy is not registered"
    _install(monkeypatch, None)
    monkeypatch.setenv("QDRANT_URL", CLOSED_PORT_URL)
    monkeypatch.setenv("QDRANT_TIMEOUT_SECONDS", "2")

    result = _call(mcp_env.tenant_a, {"query": QUERY})

    assert error_code(result) == "INTERNAL"


# --- the audit events (criterion 1, a project rule) --------------------------


def test_a_successful_search_writes_exactly_one_audit_event(
    mcp_env: SeededMcp,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a handler that appends a domain event of its own beside the
    pipeline's (a read that changed nothing has nothing to record twice) and
    one registered with an event type other than `tool.succeeded`.

    The subject and the payload keys are named, not just the count: an event
    filed under the wrong subject answers no question an auditor asks.
    """
    _install(monkeypatch, RecordingQdrant())
    session_id = uuid.uuid4()

    result = _call(mcp_env.tenant_a, {"query": QUERY}, session_id=session_id)
    rows = _audit_rows(mcp_env.database_url, session_id)

    assert result.is_error is False, error_code(result)
    assert len(rows) == 1, rows
    assert rows[0].event_type == SUCCEEDED_EVENT
    assert rows[0].subject_type == "tool"
    assert rows[0].subject_id == SEARCH
    assert rows[0].payload == {"tool": SEARCH, "scope": "any"}


def test_a_refused_argument_writes_exactly_one_failure_event(
    mcp_env: SeededMcp,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a failure recorded as a success, as a permission decision
    (`tool.denied`), or not at all — and a payload with no `code`, which is the
    one field that tells an auditor why the call did not run."""
    _install(monkeypatch, RecordingQdrant())
    session_id = uuid.uuid4()

    result = _call(mcp_env.tenant_a, {"query": QUERY, "top_k": 0}, session_id=session_id)
    rows = _audit_rows(mcp_env.database_url, session_id)

    assert error_code(result) == "VALIDATION"
    assert len(rows) == 1, rows
    assert rows[0].event_type == FAILED_EVENT
    assert rows[0].subject_type == "tool"
    assert rows[0].subject_id == SEARCH
    assert rows[0].payload["tool"] == SEARCH
    assert rows[0].payload["code"] == "VALIDATION"


# --- the two lifetimes (plan §5, decision E) -----------------------------------


class _CountingFactory:
    """Stands in for `_build_embedder` and counts how often it was asked."""

    def __init__(self) -> None:
        self.count = 0

    def __call__(self) -> Embedder:
        self.count += 1
        return RecordingEmbedder()


def test_the_qdrant_client_is_one_per_loop_and_the_embedder_one_per_process(
    mcp_env: SeededMcp,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches the two lifetimes swapped, which is the risk plan §5 names.

    A client cached per PROCESS is a pooled connection belonging to a loop that
    has died — the failure `redis_client` exists to avoid — so two
    `asyncio.run` scenarios must get two clients, and one scenario must get one.

    An embedder cached per LOOP pays an ONNX model load on every request, so
    the constructor must run exactly once across both scenarios; the counting
    factory sees the difference, which two identical objects alone would not.
    """
    factory = _CountingFactory()
    monkeypatch.setattr(rag_clients, "_build_embedder", factory)
    settings = load_settings()

    async def scenario() -> tuple[Any, Any, Embedder]:
        try:
            return (
                rag_clients.qdrant(settings),
                rag_clients.qdrant(settings),
                rag_clients.embedder(),
            )
        finally:
            await rag_clients.close_loop_clients()

    first, first_again, model_one = asyncio.run(scenario())
    second, second_again, model_two = asyncio.run(scenario())

    assert first is first_again
    assert second is second_again
    assert first is not second, "the client outlived the loop that opened it"
    assert model_one is model_two, "the embedder was rebuilt for the second loop"
    assert factory.count == 1, factory.count
