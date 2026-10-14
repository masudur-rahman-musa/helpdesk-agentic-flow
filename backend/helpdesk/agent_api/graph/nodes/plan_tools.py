"""`plan_tools`: WHICH tools this turn should call, and with what (the design step 6).

One structured call per round under the `agent-strong` alias (decision G).
Choosing which mutation to run and with what arguments is the riskiest
judgment the model makes in a turn: the cheap alias classifies into a closed
label set, where a wrong answer costs a clarifying question, and this one
costs a wrong record. Pinned by
`test_agent_turn.py::test_the_planner_asks_the_strong_alias_and_the_classifier_the_default_one`,
which is the only case that can see the alias at all.

**The round is decided before the gateway is touched.** An intent whose
`INTENT_TOOLS` row is empty (`ask_policy`, `other`) makes NO model call
(`::test_an_ask_policy_turn_makes_no_planning_call_at_all`), and a turn past
any of the three budgets makes none either
(`::test_a_turn_past_the_call_budget_proposes_nothing_and_never_reaches_the_gateway`).
Both are pure predicates in `gates.py`: the budget exists to stop PAYING for a
strong-alias call, not to discard its answer.

**Everything the turn has gathered is quoted as DATA** (the design). The
resolved entities, the retrieved policy passages and the results of tools
already called ride in `user` messages; the only `system` messages are the two
prompt files this service ships. Policy text is somebody else's writing, and
merged into a system prompt it becomes an instruction the model obeys — the
prompt-injection path straight to a mutation. Pinned by
`::test_the_planning_call_quotes_the_citations_as_data_and_not_as_instructions`.

**The planning prompt's digest rides on `planning_prompt_version`**, never on
`prompt_version` (criterion 8): one string cannot carry two versions, and
`prompt_version` is the extraction prompt's by 17b's assertion. Pinned by
`::test_the_planning_prompt_version_rides_on_its_own_state_field`.

**Every round's own `total_tokens` is ADDED to `tokens_used`** (17d criterion
7), including the round that is refused for being over the per-round cap: the
call was paid for whether or not its answer was used, and a budget that only
counted rounds it liked would let an over-proposing planner run for free.
Pinned across all three call sites by
`test_agent_turn.py::test_tokens_used_is_the_sum_of_every_staged_answers_total_tokens`.

The node does not raise. A gateway that cannot answer ends the TURN, as state,
with a typed code and the templated reply — the same shape every other node
uses, and nothing survives on `proposed`
(`::test_a_gateway_failure_in_planning_ends_the_turn_through_fail_turn`).
"""

import json
from typing import Any

from helpdesk.agent_api.graph import gates
from helpdesk.agent_api.graph.deps import TurnDeps
from helpdesk.agent_api.graph.nodes.extract_intent import FAILURE_REPLY, SYSTEM_PROMPT
from helpdesk.agent_api.graph.state import ProposedCall, ToolPlanAnswer, TurnOutcome, TurnState
from helpdesk.agent_api.prompts import load_prompt
from helpdesk.agent_api.services.model_gateway.client import ModelAlias
from helpdesk.agent_api.tools.catalogue import planner_schema
from helpdesk.shared.errors import ToolError

PLANNING_PROMPT = "tool_planning"


def elapsed_seconds(state: TurnState, deps: TurnDeps) -> float:
    """How long this turn has been running, from the injected clock.

    Zero when `started_at` was never stamped, which is the safe answer: an
    unstamped turn has spent no measurable time, so the deadline cannot refuse
    a round for a clock that was never started. `run_turn` stamps it for every
    real turn (`test_agent_turn.py::test_run_turn_stamps_started_at_from_the_injected_clock`).
    """
    if state.started_at is None:
        return 0.0
    return (deps.now() - state.started_at).total_seconds()


def _refuse(state: TurnState) -> TurnState:
    """Propose nothing and route to the refusal, leaving the round unplanned.

    This is the REJECTED round, not the spent budget: a round over
    `MAX_CALLS_PER_ROUND` is a plan the model did not think through, and there
    is nothing to compose from it. The budget path answers with
    `gates.exhausted_outcome` instead (decision C), because a turn that already
    called something has results a reply must be written from.
    """
    return state.model_copy(update={"outcome": TurnOutcome.REFUSE, "proposed": ()})


def _as_data(label: str, value: Any) -> dict[str, str]:
    """One `user` message carrying gathered material, labelled and serialised.

    A `user` role and never `system`: see this module's docstring. The label is
    this service's own word, so the model can tell the four kinds of material
    apart without any of them being able to claim it is an instruction.
    """
    return {"role": "user", "content": f"{label}:\n{json.dumps(value, default=str)}"}


async def plan_tools(state: TurnState, deps: TurnDeps) -> TurnState:
    """Ask for one round of tool calls, or decide there is nothing to ask.

    The three ways out, in the order they are decided: an intent that plans
    nothing, a turn with a budget spent, and otherwise one gateway call whose
    answer lands on `proposed` for `validate_tool_calls` to judge. Nothing is
    executed here and no assistant message is written — 17d owns both.
    """
    extraction = state.extraction
    if extraction is None:  # pragma: no cover - the graph never routes here
        return state

    allowed = gates.allowed_tools(extraction.intent)
    if not allowed:
        return state.model_copy(update={"outcome": TurnOutcome.CONTINUE, "proposed": ()})

    if not gates.may_plan_another_round(
        tool_calls_made=state.tool_calls_made,
        tokens_used=state.tokens_used,
        elapsed_seconds=elapsed_seconds(state, deps),
    ):
        # Decision C: the budget is spent, so the turn stops planning — but
        # WHAT that means depends on whether it has already done something.
        # A turn that filed a request may not answer with the refusal
        # template, which says no policy covers the question. The branch is
        # the pure predicate and the edge only reads it (the design).
        return state.model_copy(
            update={
                "outcome": gates.exhausted_outcome(len(state.tool_results)),
                "proposed": (),
            }
        )

    prompt = load_prompt(PLANNING_PROMPT)
    system = load_prompt(SYSTEM_PROMPT)
    round_number = state.round_number + 1

    messages = [
        {"role": "system", "content": system.text},
        {"role": "system", "content": prompt.text},
        _as_data("intent and entities", extraction.model_dump(mode="json")),
        _as_data(
            "resolved directory ids",
            state.resolved.model_dump(mode="json") if state.resolved else {},
        ),
        _as_data(
            "allowed tools and their argument schemas",
            {n: planner_schema(n) for n in sorted(allowed)},
        ),
        _as_data(
            "policy passages retrieved for this turn",
            [c.model_dump(mode="json") for c in state.citations],
        ),
        _as_data("tool results so far", [r.model_dump(mode="json") for r in state.tool_results]),
        {"role": "user", "content": state.user_message},
    ]

    try:
        answer = await deps.model.complete_json(
            ToolPlanAnswer,
            messages,
            alias=ModelAlias.AGENT_STRONG,
            tenant_slug=state.tenant_slug,
            session_id=str(state.session_id),
        )
    except ToolError as exc:
        return state.model_copy(
            update={
                "planning_prompt_version": prompt.sha256,
                "round_number": round_number,
                "outcome": TurnOutcome.FAILED,
                "error_code": exc.code,
                "reply": FAILURE_REPLY,
                "proposed": (),
            }
        )

    # The strict wire answer mapped onto the unchanged `ProposedCall`, so
    # `validate_tool_calls` judges the same objects it always did.
    plan = [ProposedCall(tool=call.tool, arguments=call.arguments) for call in answer.value.calls]
    tokens_used = state.tokens_used + answer.usage.total_tokens

    if len(plan) > gates.MAX_CALLS_PER_ROUND:
        # An over-long round is refused WHOLE rather than trimmed, for
        # decision C's reason: keeping the first three of five calls executes
        # a plan the model did not make. Pinned by
        # `test_agent_turn.py::test_a_round_over_the_per_round_cap_is_refused_whole`.
        return _refuse(
            state.model_copy(
                update={"planning_prompt_version": prompt.sha256, "tokens_used": tokens_used}
            )
        )

    return state.model_copy(
        update={
            "planning_prompt_version": prompt.sha256,
            "round_number": round_number,
            "tokens_used": tokens_used,
            "outcome": TurnOutcome.CONTINUE,
            "proposed": tuple(plan),
        }
    )
