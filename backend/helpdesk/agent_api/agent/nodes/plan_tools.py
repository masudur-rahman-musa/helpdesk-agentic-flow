"""`plan_tools`: which tools this turn should call, and with what.

Uses the strong model: picking a change and its arguments is the riskiest
choice the model makes, and a wrong answer means a wrong record. Intents with
no tools, and turns past a budget, make no model call at all. Everything
gathered goes in as `user` data, never `system`, so policy text cannot
become an instruction that leads to a change. Every round's tokens count,
refused rounds too. The node never raises.
"""

import json
from typing import Any

from helpdesk.agent_api.agent import gates
from helpdesk.agent_api.agent.nodes.extract_intent import FAILURE_REPLY, SYSTEM_PROMPT
from helpdesk.agent_api.agent.prompts import load_prompt
from helpdesk.agent_api.agent.state import ProposedCall, ToolPlanAnswer, TurnOutcome, TurnState
from helpdesk.agent_api.agent.tool_args import planner_schema
from helpdesk.agent_api.agent.turn_deps import TurnDeps
from helpdesk.agent_api.services.model_gateway.client import ModelAlias
from helpdesk.shared.core.errors import ToolError

PLANNING_PROMPT = "tool_planning"


def elapsed_seconds(state: TurnState, deps: TurnDeps) -> float:
    """How long the turn has run, on the injected clock. Zero if never
    stamped, so the deadline cannot fire on a clock that was not started."""
    if state.started_at is None:
        return 0.0
    return (deps.now() - state.started_at).total_seconds()


def _refuse(state: TurnState) -> TurnState:
    """Propose nothing and route to the refusal: the round was over the
    per-round cap, so there is nothing to reply about."""
    return state.model_copy(update={"outcome": TurnOutcome.REFUSE, "proposed": ()})


def _as_data(label: str, value: Any) -> dict[str, str]:
    """One labelled `user` message of gathered material."""
    return {"role": "user", "content": f"{label}:\n{json.dumps(value, default=str)}"}


async def plan_tools(state: TurnState, deps: TurnDeps) -> TurnState:
    """Ask for one round of tool calls, or decide there is nothing to ask.
    Nothing runs here; the answer lands on `proposed` for validation."""
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
        # Budget spent. A turn that already did something replies about it
        # rather than claiming no policy covers the question.
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

    # Map the wire answer onto `ProposedCall`, the shape validation judges.
    plan = [ProposedCall(tool=call.tool, arguments=call.arguments) for call in answer.value.calls]
    tokens_used = state.tokens_used + answer.usage.total_tokens

    if len(plan) > gates.MAX_CALLS_PER_ROUND:
        # Refused whole, not trimmed: the first three of five calls is a
        # plan the model did not make.
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
