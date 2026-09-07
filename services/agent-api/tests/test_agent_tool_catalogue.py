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
from pathlib import Path
from types import MappingProxyType
from typing import Any

from agent_api.graph.state import Citation
from agent_api.tools.catalogue import (
    SEARCH_POLICY,
    SEARCH_POLICY_TOP_K,
    TOOL_INPUTS,
    SearchPolicyArgs,
    SearchPolicyResult,
)

# `mcp_fixtures` lives beside the mcp-server tests and is imported by name, the
# way every file in that suite imports it. pytest only puts a test directory on
# `sys.path` when it collects it, and `services/agent-api/tests` is collected
# FIRST, so the path is added here rather than assumed.
_MCP_TESTS = Path(__file__).resolve().parents[3] / "services" / "mcp-server" / "tests"
if str(_MCP_TESTS) not in sys.path:
    sys.path.insert(0, str(_MCP_TESTS))

from mcp_fixtures import (  # noqa: E402 - path set immediately above
    SeededMcp,
    bearer,
    list_tools,
    mcp_env,  # noqa: F401 - fixtures are resolved by name
    mcp_pg,  # noqa: F401
    mcp_redis_base_url,  # noqa: F401
    mcp_redis_url,  # noqa: F401
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


def test_tool_inputs_holds_exactly_the_one_tool_this_plan_registers() -> None:
    """Catches a catalogue that registers 17c's tools early — a model with no
    drift row on the server would be advertised nowhere and validated by
    nothing — and a `TOOL_INPUTS` left mutable, where a node could register a
    tool at runtime and escape the drift test below entirely."""
    assert dict(TOOL_INPUTS) == {SEARCH_POLICY: SearchPolicyArgs}
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


# --- decision C: the drift test ------------------------------------------------


def test_every_catalogued_tool_is_one_the_server_advertises(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches a tool renamed on the server, or misspelled here: the agent
    would call a name `registry.lookup` cannot resolve and the turn would end
    as an `INTERNAL` a user cannot act on, for a tool that exists."""
    advertised = _advertised(mcp_env)

    assert set(TOOL_INPUTS) <= set(advertised)


def test_every_catalogued_schema_agrees_with_the_advertised_one(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Plan §5's first risk. Catches a field renamed on either side and a
    field that changed from optional to required.

    The property-name comparison catches `query` becoming `question`; the
    required-set comparison catches `top_k` losing its default on the server,
    which would make every agent call that omits it fail `VALIDATION` while
    both models still list the same two fields. Neither check can answer for
    the other."""
    advertised = _advertised(mcp_env)

    for name, model in TOOL_INPUTS.items():
        theirs: dict[str, Any] = advertised[name].input_schema
        ours = model.model_json_schema()
        assert set(_properties(ours)) == set(_properties(theirs)), name
        assert _required(ours) == _required(theirs), name


def test_the_advertised_top_k_default_is_the_catalogues_own(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches the corpus default moving in `helpdesk.rag.retrieval` while the
    agent keeps asking for six. The property names and the required set both
    stay equal through such a change, so the two assertions above cannot see
    it — only the default itself can."""
    advertised = _advertised(mcp_env)

    theirs: dict[str, Any] = advertised[SEARCH_POLICY].input_schema
    assert _properties(theirs)["top_k"]["default"] == SEARCH_POLICY_TOP_K


def test_the_citation_fields_are_the_ones_the_server_returns(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """The OUTPUT half of decision C, which the input drift test cannot reach.

    Catches `Citation` drifting from `PolicyCitation`: a field renamed on the
    server would arrive as an extra key and the missing one would fail
    validation mid-turn, ending an answerable question as an `INTERNAL`. Read
    off the advertised output schema rather than by importing `mcp_server`,
    because the agent talks to that service over HTTP and never by import."""
    advertised = _advertised(mcp_env)

    output: dict[str, Any] = advertised[SEARCH_POLICY].output_schema
    citation_ref = _properties(output)["citations"]["items"]["$ref"]
    citation_schema: dict[str, Any] = output["$defs"][citation_ref.rsplit("/", 1)[-1]]

    assert set(_properties(citation_schema)) == set(CITATION_FIELDS)
    assert _required(citation_schema) == CITATION_FIELDS
    assert set(_properties(output)) == set(SearchPolicyResult.model_fields)
