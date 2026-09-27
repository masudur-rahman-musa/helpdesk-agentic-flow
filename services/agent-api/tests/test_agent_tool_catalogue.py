"""An earlier change §6: the agent's tool catalogue against the server's own advertisement.

Surface under test (written before it exists):

``agent_api.tools.catalogue``
    ``SEARCH_POLICY = "search_policy"`` · ``SEARCH_POLICY_TOP_K = 6``
    ``class SearchPolicyArgs(BaseModel)`` — ``query: str``,
        ``top_k: int = SEARCH_POLICY_TOP_K``.
    ``TOOL_INPUTS: Mapping[str, type[BaseModel]]`` — a ``MappingProxyType``
        holding ``{SEARCH_POLICY: SearchPolicyArgs}``.
    ``class SearchPolicyResult(BaseModel)`` — ``citations: list[Citation]``,
        ``refused: bool``.

``agent_api.graph.state``
    ``class Citation(BaseModel)`` (frozen) — the seven wire fields.

An earlier change extends the same file. ``TOOL_INPUTS`` grows to the nineteen tools
the agent may reach, the drift sweep is parametrized over every one of them,
and the catalogue gains

``agent_api.tools.catalogue``
    ``def planner_schema(name: str) -> dict[str, Any]`` — that tool's JSON
        schema with ``idempotency_key`` removed from both ``properties`` and
        ``required`` (decision E). It is what ``plan_tools`` shows the model,
        so a key the model names is an unknown argument rather than an
        override.

Decision C is what this file exists for. agent-api and mcp-server are separate
deployables that talk over HTTP and never import each other, so the argument
models have to be written twice; this test is what makes the second copy true.
A rename on the server turns into a red suite here rather than into a mid-turn
`VALIDATION` for a user.

The advertisement is read off the REAL server, in process: `mcp_fixtures`
drives `create_app()` over an httpx2 ASGI transport, so no socket is opened.
Cost is that fixture module's session-scoped Postgres and Redis (standing rule
7); `tools/list` itself touches neither Qdrant nor a model.
"""

import sys
from collections.abc import Iterator
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
from agent_api.graph.state import Citation
from agent_api.tools.catalogue import (
    SEARCH_POLICY,
    SEARCH_POLICY_TOP_K,
    TOOL_INPUTS,
    SearchPolicyArgs,
    SearchPolicyResult,
    planner_schema,
)

# `mcp_fixtures` lives beside the mcp-server tests and is imported by name, the
# way every file in that suite imports it. pytest only puts a test directory on
# `sys.path` when it collects it, and `services/agent-api/tests` is collected
# FIRST, so the path is added here rather than assumed.
_MCP_TESTS = Path(__file__).resolve().parents[3] / "services" / "mcp-server" / "tests"
if str(_MCP_TESTS) not in sys.path:
    sys.path.insert(0, str(_MCP_TESTS))

from mcp_fixtures import (  # noqa: E402 - path set immediately above
    TEST_JWT_SIGNING_PHRASE,
    TEST_QDRANT_URL,
    SeededMcp,
    bearer,
    list_tools,
    mcp_pg,  # noqa: F401 - fixtures are resolved by name
    mcp_redis_base_url,  # noqa: F401
    seeded_mcp_db,  # noqa: F401
    token_for,
)

CITATION_FIELDS: frozenset[str] = frozenset(
    {"document", "title", "version", "heading_path", "chunk_id", "text", "score"}
)
"""the design's six citation fields plus `title` (15a decision E), as a literal.

Counted from the specification and the tool's own model: seven (standing rule
6). Written out rather than read off the server, so a field DROPPED on both
sides at once is still a red row here."""


ADMIN_TOOLS: frozenset[str] = frozenset(
    {
        "search_audit",
        "set_kill_switch",
        "rollback_request",
        # An earlier change: the kill switch BOARD, registered as a tool and
        # deliberately left out of the catalogue (decision F) for the same
        # reason as the other three — the admin page asks the wire directly,
        # and a tool the model may call is model-driven behaviour that owes
        # golden cases before it merges.
        "list_kill_switches",
        # An earlier change: the SoD rules board, registered as a tool and left out of
        # the catalogue for the same reason (decision F) - `/admin/rules` asks
        # the wire directly, and the policy engine already gets the rules it
        # needs through `snapshot.py`.
        "list_sod_rules",
        # An earlier change: the policy documents board, registered as a tool and left
        # out of the catalogue for the same reason (24ea decision F) -
        # `/admin/policies` asks the wire directly, and the model already
        # reaches the corpus through `search_policy`, which answers the spans
        # an employee may read rather than the ingest's version history.
        "list_policy_documents",
        # An earlier change decision G: the re-ingest, registered as a tool and
        # deliberately left out of the catalogue. Sharper than the five above -
        # a model that can re-ingest a corpus can change the evidence its own
        # next answer cites, so the only caller is a person on
        # `/admin/policies`, exactly as `set_kill_switch` is person-only.
        "reingest_policy_document",
    }
)
"""The seven tools rows 24 and 31a own, which this catalogue must NOT register."""

CATALOGUED_TOOLS: frozenset[str] = frozenset(
    {
        "get_me",
        "list_my_access",
        "list_my_requests",
        "classify_access_request",
        "create_access_request",
        "revoke_access",
        "onboard_user",
        "transfer_user",
        "offboard_user",
        "open_access_review",
        "list_review_items",
        "decide_review_item",
        "list_pending_approvals",
        "approve_request",
        "reject_request",
        "reset_own_password",
        "unlock_own_account",
        "search_policy",
        "create_ticket",
    }
)
"""Every tool the agent may reach, as a literal: nineteen.

Counted from the server's own `REGISTRY` — twenty-nine specs — less the seven
in `ADMIN_TOOLS`, `list_reviews`, and the two notification tools row 23ga
deliberately leaves out (decision E): a tool the model may call is
model-driven behaviour and owes golden cases before it merges, and the
`/reviews` and `/notifications` pages ask the wire directly.
Written out rather than read off `tools/list`, so a tool DROPPED on both sides
at once is still a red row here."""

MUTATING_TOOLS: frozenset[str] = frozenset(
    {
        "create_access_request",
        "revoke_access",
        "onboard_user",
        "transfer_user",
        "offboard_user",
        "open_access_review",
        "decide_review_item",
        "approve_request",
        "reject_request",
        "reset_own_password",
        "unlock_own_account",
        "create_ticket",
    }
)
"""The twelve catalogued tools whose registry spec says `mutating=True`.

Counted from `REGISTRY` and written out here, because agent-api never imports
mcp-server (the design): these are exactly the tools whose wire input carries
an `idempotency_key`, and the twelve rows below are what hold that copy true."""

IDEMPOTENCY_KEY = "idempotency_key"

REINGEST_POLICY_DOCUMENT = "reingest_policy_document"
"""An earlier change's tool, named here as a literal: the catalogue must know it only as
a name it does NOT register (decision G)."""


def _advertised(env: SeededMcp) -> dict[str, Any]:
    """Every tool the real server advertises, by name, over a real session."""
    result = list_tools(bearer(token_for(env.tenant_a)))
    return {tool.name: tool for tool in result.tools}


def _properties(schema: dict[str, Any]) -> dict[str, Any]:
    properties: dict[str, Any] = schema.get("properties", {})
    return properties


def _required(schema: dict[str, Any]) -> frozenset[str]:
    return frozenset(schema.get("required", []))


# --- the constants ------------------------------------------------------------


def test_the_catalogue_constants_are_exactly_the_planned_values() -> None:
    """a project rule, and the one that bites hardest: catches a `top_k`
    default written as 5 or 10 on the agent side. A page size differing from
    the corpus default silently changes how much policy every answer is
    composed from, and nothing downstream would report it."""
    assert SEARCH_POLICY == "search_policy"
    assert SEARCH_POLICY_TOP_K == 6


def test_the_args_model_defaults_top_k_to_the_catalogue_constant() -> None:
    """Catches a model whose default was typed as a literal beside the
    constant: the two would then drift apart on the next tuning change, and the
    constant would be the one thing no call actually uses."""
    assert SearchPolicyArgs(query="fake question").top_k == SEARCH_POLICY_TOP_K
    assert SearchPolicyArgs.model_fields["top_k"].default == SEARCH_POLICY_TOP_K


def test_tool_inputs_holds_exactly_the_nineteen_tools_the_agent_may_reach() -> None:
    """An earlier change criterion 1, replacing 17b's one-entry assertion. Catches a
    catalogue short of a tool — a name in an `INTENT_TOOLS` row that no model
    validates, so its arguments reach the server unchecked — and one that
    registers an admin tool: `search_audit`, `set_kill_switch`,
    `rollback_request` and `list_kill_switches` belong to row 24, and a tool
    with no catalogue entry
    cannot be proposed at all, which is the defence in depth behind the RBAC
    matrix that decision A asks for.

    `MappingProxyType` is asserted for the reason it was in 17b: a mutable
    mapping lets a node register a tool at runtime, and a tool registered at
    runtime is a tool the drift sweep below never compares."""
    assert set(TOOL_INPUTS) == CATALOGUED_TOOLS
    assert len(TOOL_INPUTS) == 19
    assert ADMIN_TOOLS.isdisjoint(TOOL_INPUTS)
    assert TOOL_INPUTS[SEARCH_POLICY] is SearchPolicyArgs
    assert isinstance(TOOL_INPUTS, MappingProxyType)


def test_the_result_model_carries_exactly_the_citations_and_the_flag() -> None:
    """Catches a result model that adds a field the tool never sends (a
    `query`, a `score` threshold) or drops `refused`: dropping it makes the
    refusal branch undecidable, since decision D says the flag is read and
    never recomputed."""
    assert set(SearchPolicyResult.model_fields) == {"citations", "refused"}


def test_the_citation_model_carries_exactly_the_seven_wire_fields() -> None:
    """Catches a `Citation` that keeps an internal value the tool deliberately
    withholds — the dense cosine `should_refuse` reads is NOT on the wire — or
    that drops `title`, which is the field a rendered citation needs (15a
    decision E). Frozen is asserted too: a node handing citations to 17c must
    not be able to edit one on the way."""
    assert set(Citation.model_fields) == set(CITATION_FIELDS)
    assert Citation.model_config.get("frozen") is True


def test_the_reingest_tool_is_advertised_but_is_not_the_models_to_call() -> None:
    """An earlier change decision G, both halves.

    KILL: a `TOOL_INPUTS` entry for `reingest_policy_document`. The agent
    proposes tools from this mapping, so an entry is a model that can re-ingest
    a tenant's corpus mid-turn - rewriting the very `policy_chunks` its next
    `search_policy` cites, with no person in the loop and no golden case over
    the behaviour. `set_kill_switch` is the precedent, and this is the same
    class of control: the page asks the wire directly.

    The membership assertion is the other half. `ADMIN_TOOLS` is what the
    catalogue-size case measures itself against, so a name simply forgotten
    here would leave the tool absent from both sets and nothing red - the
    disjointness check is satisfied by a name that is in neither.
    """
    assert REINGEST_POLICY_DOCUMENT in ADMIN_TOOLS
    assert REINGEST_POLICY_DOCUMENT not in TOOL_INPUTS
    assert REINGEST_POLICY_DOCUMENT not in CATALOGUED_TOOLS


# --- decision C: the drift test ------------------------------------------------


@pytest.fixture(scope="session")
def catalogue_mcp_env(
    seeded_mcp_db: SeededMcp,  # noqa: F811
    mcp_redis_base_url: str,  # noqa: F811
) -> Iterator[SeededMcp]:
    """`mcp_fixtures.mcp_env`, session-scoped, for this file only.

    The shared `mcp_env` takes pytest's function-scoped `monkeypatch`, so every
    session-scoped consumer of it is a `ScopeMismatch` at setup rather than a
    test. a project rule wants the backing services built once, so this file
    drives its own `pytest.MonkeyPatch()` over the same four variables and
    reads the SESSION Redis index, undoing them when the session ends.

    Pinned by every row of this file: without it they error instead of running."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("DATABASE_URL", seeded_mcp_db.database_url)
        patch.setenv("REDIS_URL", mcp_redis_base_url)
        patch.setenv("JWT_SECRET", TEST_JWT_SIGNING_PHRASE)
        patch.setenv("QDRANT_URL", TEST_QDRANT_URL)
        yield seeded_mcp_db


@pytest.fixture(scope="session")
def advertised(catalogue_mcp_env: SeededMcp) -> dict[str, Any]:
    """One `tools/list`, read once for the whole sweep.

    Nineteen parametrized rows driving their own MCP session would pay the
    handshake nineteen times for an answer that cannot change between them."""
    return _advertised(catalogue_mcp_env)


@pytest.mark.parametrize("name", sorted(CATALOGUED_TOOLS))
def test_every_catalogued_tool_is_one_the_server_advertises(
    name: str, advertised: dict[str, Any]
) -> None:
    """One row per tool, where 17b had one subset check. Catches
    a tool renamed on the server, or misspelled here: the agent would call a
    name `registry.lookup` cannot resolve and the turn would end as an
    `INTERNAL` a user cannot act on, for a tool that exists. It also catches
    the catalogue entry being ABSENT, which the subset form could not — a
    missing entry made that assertion truer, not falser."""
    assert name in advertised
    assert name in TOOL_INPUTS


@pytest.mark.parametrize("name", sorted(CATALOGUED_TOOLS))
def test_every_catalogued_schema_agrees_with_the_advertised_one(
    name: str, advertised: dict[str, Any]
) -> None:
    """Plan §5's first risk, now over all nineteen. Catches a field renamed on
    either side and a field that changed from optional to required.

    The property-name comparison catches `query` becoming `question` or
    `user_id` becoming `target_user_id`; the required-set comparison catches a
    field losing its default on the server, which would make every agent call
    that omits it fail `VALIDATION` while both models still list the same
    fields. Neither check can answer for the other, and neither can be
    answered by the name sweep above."""
    theirs: dict[str, Any] = advertised[name].input_schema
    ours = TOOL_INPUTS[name].model_json_schema()

    assert set(_properties(ours)) == set(_properties(theirs)), name
    assert _required(ours) == _required(theirs), name


# --- decision E: the planner-facing schema ------------------------------------


@pytest.mark.parametrize("name", sorted(MUTATING_TOOLS))
def test_every_mutating_tools_wire_model_still_carries_the_idempotency_key(
    name: str,
) -> None:
    """The half decision E's projection cannot see. The projection is defined
    as "the wire schema minus the key", so a wire model that never declared
    one would satisfy it vacuously — and the derived key would then have no
    field to travel in, leaving every mutation to be refused by the server for
    a missing required argument."""
    schema = TOOL_INPUTS[name].model_json_schema()

    assert IDEMPOTENCY_KEY in _properties(schema), name
    assert IDEMPOTENCY_KEY in _required(schema), name


@pytest.mark.parametrize("name", sorted(CATALOGUED_TOOLS))
def test_the_planner_facing_schema_is_the_wire_schema_minus_the_idempotency_key(
    name: str,
) -> None:
    """Decision E and plan §5's third risk, over every tool rather than the
    mutating twelve, because the read tools are what catch the opposite defect.

    Catches a projection that returns the wire schema untouched — the model
    would then be shown a required `idempotency_key`, name one, and have it
    honoured, which is the identifier the model is free to reuse across two
    different requests. Catches a projection that strips more than the key
    (an empty `required`, or every property dropped), which would leave the
    model guessing argument names it was never shown."""
    wire = TOOL_INPUTS[name].model_json_schema()
    ours = planner_schema(name)

    assert set(_properties(ours)) == set(_properties(wire)) - {IDEMPOTENCY_KEY}, name
    assert _required(ours) == _required(wire) - {IDEMPOTENCY_KEY}, name
    assert IDEMPOTENCY_KEY not in _properties(ours), name


def test_the_advertised_top_k_default_is_the_catalogues_own(
    catalogue_mcp_env: SeededMcp,
) -> None:
    """Catches the corpus default moving in `helpdesk.rag.retrieval` while the
    agent keeps asking for six. The property names and the required set both
    stay equal through such a change, so the two assertions above cannot see
    it — only the default itself can."""
    advertised = _advertised(catalogue_mcp_env)

    theirs: dict[str, Any] = advertised[SEARCH_POLICY].input_schema
    assert _properties(theirs)["top_k"]["default"] == SEARCH_POLICY_TOP_K


def test_the_citation_fields_are_the_ones_the_server_returns(
    catalogue_mcp_env: SeededMcp,
) -> None:
    """The OUTPUT half of decision C, which the input drift test cannot reach.

    Catches `Citation` drifting from `PolicyCitation`: a field renamed on the
    server would arrive as an extra key and the missing one would fail
    validation mid-turn, ending an answerable question as an `INTERNAL`. Read
    off the advertised output schema rather than by importing `mcp_server`,
    because the agent talks to that service over HTTP and never by import."""
    advertised = _advertised(catalogue_mcp_env)

    output: dict[str, Any] = advertised[SEARCH_POLICY].output_schema
    citation_ref = _properties(output)["citations"]["items"]["$ref"]
    citation_schema: dict[str, Any] = output["$defs"][citation_ref.rsplit("/", 1)[-1]]

    assert set(_properties(citation_schema)) == set(CITATION_FIELDS)
    assert _required(citation_schema) == CITATION_FIELDS
    assert set(_properties(output)) == set(SearchPolicyResult.model_fields)
