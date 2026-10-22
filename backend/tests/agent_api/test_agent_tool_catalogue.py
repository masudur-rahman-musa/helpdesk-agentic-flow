"""The agent's tool catalogue against the server's own advertisement.

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

``TOOL_INPUTS`` holds the twenty tools
the agent may reach, the drift sweep is parametrized over every one of them,
and the catalogue gains

``agent_api.tools.catalogue``
    ``def planner_schema(name: str) -> dict[str, Any]`` — that tool's JSON
        schema with ``idempotency_key`` removed from both ``properties`` and
        ``required``. It is what ``plan_tools`` shows the model,
        so a key the model names is an unknown argument rather than an
        override.

The drift test is what this file exists for. agent-api and mcp-server are separate
deployables that talk over HTTP and never import each other, so the argument
models have to be written twice; this test is what makes the second copy true.
A rename on the server turns into a red suite here rather than into a mid-turn
`VALIDATION` for a user.

The advertisement is read off the REAL server, in process: `mcp_fixtures`
drives `create_app()` over an httpx2 ASGI transport, so no socket is opened.
Cost is that fixture module's session-scoped Postgres and Redis;
`tools/list` itself touches neither Qdrant nor a model.
"""

from collections.abc import Iterator
from types import MappingProxyType
from typing import Any

import pytest
from pydantic import ValidationError

from helpdesk.agent_api.agent.state import Citation
from helpdesk.agent_api.agent.tool_args import (
    SEARCH_POLICY,
    SEARCH_POLICY_TOP_K,
    TOOL_INPUTS,
    ListMyTicketsArgs,
    ListRequestableRolesArgs,
    SearchPolicyArgs,
    SearchPolicyResult,
    planner_schema,
)
from tests.support.mcp import (
    TEST_JWT_SIGNING_PHRASE,
    TEST_QDRANT_URL,
    SeededMcp,
    bearer,
    list_tools,
    token_for,
)

CITATION_FIELDS: frozenset[str] = frozenset(
    {"document", "title", "version", "heading_path", "chunk_id", "text", "score"}
)
"""Six citation fields plus `title`, as a literal.

Counted from the design and the tool's own model: seven. Written out rather
than read off the server, so a field DROPPED on both sides at once is still a red row here."""


ADMIN_TOOLS: frozenset[str] = frozenset(
    {
        "search_audit",
        "set_kill_switch",
        "rollback_request",
        # The kill switch BOARD, registered as a tool and
        # deliberately left out of the catalogue for the same
        # reason as the other three — the admin page asks the wire directly,
        # and a tool the model may call is model-driven behaviour that owes
        # golden cases before it merges.
        "list_kill_switches",
        # The SoD rules board, registered as a tool and left out of
        # the catalogue for the same reason - `/admin/rules` asks
        # the wire directly, and the policy engine already gets the rules it
        # needs through `snapshot.py`.
        "list_sod_rules",
        # The policy documents board, registered as a tool and left
        # out of the catalogue for the same reason -
        # `/admin/policies` asks the wire directly, and the model already
        # reaches the corpus through `search_policy`, which answers the spans
        # an employee may read rather than the ingest's version history.
        "list_policy_documents",
        # The re-ingest, registered as a tool and
        # deliberately left out of the catalogue. Sharper than the five above -
        # a model that can re-ingest a corpus can change the evidence its own
        # next answer cites, so the only caller is a person on
        # `/admin/policies`, exactly as `set_kill_switch` is person-only.
        "reingest_policy_document",
        # The tool-steps switch, person-only like `set_kill_switch`: the
        # `/admin/settings` page asks the wire directly, and the model never
        # sees or calls it.
        "set_tool_steps_visible",
    }
)
"""The eight admin tools, which this catalogue must NOT register."""

CATALOGUED_TOOLS: frozenset[str] = frozenset(
    {
        "get_me",
        "list_my_access",
        "list_my_requests",
        "list_my_tickets",
        "list_requestable_roles",
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
        "cancel_request",
    }
)
"""Every tool the agent may reach, as a literal: twenty-two.

Counted from the server's own `REGISTRY` — thirty specs — less the seven
in `ADMIN_TOOLS`, `list_reviews`, and the two notification tools
deliberately left out: a tool the model may call is
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
        "cancel_request",
    }
)
"""The thirteen catalogued tools whose registry spec says `mutating=True`.

Counted from `REGISTRY` and written out here, because agent-api never imports
mcp-server: these are exactly the tools whose wire input carries
an `idempotency_key`, and the twelve rows below are what hold that copy true."""

IDEMPOTENCY_KEY = "idempotency_key"

REINGEST_POLICY_DOCUMENT = "reingest_policy_document"
"""The re-ingest tool, named here as a literal: the catalogue must know it only as
a name it does NOT register."""


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
    """And the one that bites hardest: catches a `top_k`
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


def test_tool_inputs_holds_exactly_the_twenty_two_tools_the_agent_may_reach() -> None:
    """Catches a
    catalogue short of a tool — a name in an `INTENT_TOOLS` row that no model
    validates, so its arguments reach the server unchecked — and one that
    registers an admin tool: `search_audit`, `set_kill_switch`,
    `rollback_request` and `list_kill_switches` belong to the admin set, and a tool
    with no catalogue entry
    cannot be proposed at all, which is the defence in depth behind the RBAC
    matrix.

    `MappingProxyType` is asserted for the reason it was in 17b: a mutable
    mapping lets a node register a tool at runtime, and a tool registered at
    runtime is a tool the drift sweep below never compares."""
    assert set(TOOL_INPUTS) == CATALOGUED_TOOLS
    assert len(TOOL_INPUTS) == 22
    assert ADMIN_TOOLS.isdisjoint(TOOL_INPUTS)
    assert TOOL_INPUTS[SEARCH_POLICY] is SearchPolicyArgs
    assert isinstance(TOOL_INPUTS, MappingProxyType)


def test_the_result_model_carries_exactly_the_citations_and_the_flag() -> None:
    """Catches a result model that adds a field the tool never sends (a
    `query`, a `score` threshold) or drops `refused`: dropping it makes the
    refusal branch undecidable, since the flag is read and
    never recomputed."""
    assert set(SearchPolicyResult.model_fields) == {"citations", "refused"}


def test_the_citation_model_carries_exactly_the_seven_wire_fields() -> None:
    """Catches a `Citation` that keeps an internal value the tool deliberately
    withholds — the dense cosine `should_refuse` reads is NOT on the wire — or
    that drops `title`, which is the field a rendered citation needs.
    Frozen is asserted too: a node handing citations to planning must
    not be able to edit one on the way."""
    assert set(Citation.model_fields) == set(CITATION_FIELDS)
    assert Citation.model_config.get("frozen") is True


def test_the_reingest_tool_is_advertised_but_is_not_the_models_to_call() -> None:
    """Both halves.

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


# --- the drift test ------------------------------------------------


@pytest.fixture(scope="module")
def catalogue_mcp_env(
    seeded_mcp_db: SeededMcp,
    mcp_redis_base_url: str,
) -> Iterator[SeededMcp]:
    """`mcp_fixtures.mcp_env`, module-scoped, for this file only.

    The shared `mcp_env` takes pytest's function-scoped `monkeypatch`, so every
    wider-scoped consumer of it is a `ScopeMismatch` at setup rather than a
    test. The backing services are built once, so this file
    drives its own `pytest.MonkeyPatch()` over the same four variables and
    reads the SESSION Redis index, undoing them when this file's last test ends.
    Module scope, not session: a patch that lived to the end of the run would
    leak `REDIS_URL` into every later file, and the shared Redis gate then
    refuses to flush an index equal to the configured one.

    Pinned by every row of this file: without it they error instead of running."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("DATABASE_URL", seeded_mcp_db.database_url)
        patch.setenv("REDIS_URL", mcp_redis_base_url)
        patch.setenv("JWT_SECRET", TEST_JWT_SIGNING_PHRASE)
        patch.setenv("QDRANT_URL", TEST_QDRANT_URL)
        yield seeded_mcp_db


@pytest.fixture(scope="module")
def advertised(catalogue_mcp_env: SeededMcp) -> dict[str, Any]:
    """One `tools/list`, read once for the whole sweep.

    Twenty parametrized rows driving their own MCP session would pay the
    handshake twenty times for an answer that cannot change between them."""
    return _advertised(catalogue_mcp_env)


@pytest.mark.parametrize("name", sorted(CATALOGUED_TOOLS))
def test_every_catalogued_tool_is_one_the_server_advertises(
    name: str, advertised: dict[str, Any]
) -> None:
    """One row per tool. Catches
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
    """Over all twenty tools. Catches a field renamed on
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


# --- the planner-facing schema ------------------------------------


@pytest.mark.parametrize("name", sorted(MUTATING_TOOLS))
def test_every_mutating_tools_wire_model_still_carries_the_idempotency_key(
    name: str,
) -> None:
    """The half the projection cannot see. The projection is defined
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
    """Over every tool rather than the
    mutating thirteen, because the read tools are what catch the opposite defect.

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
    """The OUTPUT half, which the input drift test cannot reach.

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


def test_list_requestable_roles_args_match_the_server_input(
    advertised: dict[str, Any],
) -> None:
    """Catches the agent's `ListRequestableRolesArgs` drifting from the
    server's input in the one place the name sweep cannot see: an agent-side
    `limit` default that is not the server's 50, or an argument the server
    does not take. The tool must also be advertised, so the new name is not
    only a catalogue entry."""
    theirs: dict[str, Any] = advertised["list_requestable_roles"].input_schema
    ours = ListRequestableRolesArgs.model_json_schema()

    assert TOOL_INPUTS["list_requestable_roles"] is ListRequestableRolesArgs
    assert set(_properties(ours)) == set(_properties(theirs)) == {"system", "limit"}
    assert _required(ours) == _required(theirs) == frozenset()
    assert _properties(theirs)["limit"]["default"] == 50
    assert _properties(ours)["limit"]["default"] == _properties(theirs)["limit"]["default"]


def test_list_my_tickets_args_match_the_server_input(
    advertised: dict[str, Any],
) -> None:
    """Catches the agent's `ListMyTicketsArgs` drifting from the server's
    input: a `limit` default that is not the server's 50, a required argument,
    or any extra argument such as a person to look up."""
    theirs: dict[str, Any] = advertised["list_my_tickets"].input_schema
    ours = ListMyTicketsArgs.model_json_schema()

    assert TOOL_INPUTS["list_my_tickets"] is ListMyTicketsArgs
    assert set(_properties(ours)) == set(_properties(theirs)) == {"limit"}
    assert _required(ours) == _required(theirs) == frozenset()
    assert _properties(theirs)["limit"]["default"] == 50
    assert _properties(ours)["limit"]["default"] == 50


def test_the_agent_cannot_reach_the_ticket_solving_tools() -> None:
    """Catches a catalogue that registers `solve_ticket` or `list_open_tickets`:
    a tool with no entry cannot be proposed at all, which is the defence behind
    the rights matrix."""
    assert "solve_ticket" not in TOOL_INPUTS
    assert "list_open_tickets" not in TOOL_INPUTS


def test_list_my_tickets_args_refuse_a_user_id() -> None:
    """Catches an args model that accepts a person to look up: the chat would
    then be able to ask for another person's tickets. Identity comes from the
    token only."""
    with pytest.raises(ValidationError):
        ListMyTicketsArgs.model_validate({"user_id": "33333333-3333-5333-8333-333333333333"})
