"""The one place a turn becomes the design label values.

`TurnState` is a service type, so the translation lives here rather than in
`helpdesk.obs.metrics`: the package owns the counters and their label sets,
this module owns what a turn's intent, outcome and error code are CALLED when
they reach them. One translation, called at every terminal of `run_turn`,
`stream_turn` and `resume_turn`, so the three cannot disagree about what a
turn counts as.

Every value crossing into a label is a closed enum member or the tenant uuid
the token carried. Nothing from the message, the model's answer or an
exception's words is ever a label, which is what keeps the unauthenticated
`/metrics` free of identity and free of unbounded cardinality. Pinned by
`test_agent_metrics.py::test_the_exposition_names_no_user_of_the_seeded_tenant`.
"""

from typing import Any

from helpdesk.agent_api.graph.state import TurnOutcome, TurnState
from helpdesk.shared.errors import ErrorCode
from helpdesk.shared.obs import metrics
from helpdesk.shared.obs.context import current_context
from helpdesk.shared.policy.types import RiskClass, severity

__all__ = ["record_turn_code", "record_turn_state", "trace_attributes"]

_REFUSED_OUTCOMES = frozenset({TurnOutcome.REFUSE, TurnOutcome.FAILED})
"""The two outcomes that are also a refusal (§1's fourth criterion).

A turn is counted once whatever happens to it; these two are counted a second
time under `helpdesk_refusals_total`, because "how many turns ran" and "how
many were refused" are two questions and the second needs the typed code.
"""


def record_turn_state(state: TurnState) -> None:
    """Count one finished turn, and its refusal when it was refused.

    Called at a TERMINAL — where a turn is answered, paused or failed — and
    never inside a node: LangGraph replays the interrupting node on a resume,
    so a counter under a node would report a paused turn again every time an
    approver decides. Pinned by
    `test_agent_metrics.py::test_a_resumed_turn_counts_exactly_one`.

    `intent` is the extraction's when there is one and the literal `unknown`
    when the turn ended before anything was classified; a turn that never
    reached the classifier still happened, and dropping it would make the turn
    rate disagree with the refusal rate during exactly the outage an operator
    is looking at.
    """
    tenant = str(state.tenant_id)
    intent = metrics.UNKNOWN_INTENT if state.extraction is None else state.extraction.intent.value
    metrics.record_turn(tenant=tenant, intent=intent, decision=state.outcome.value)
    if state.outcome in _REFUSED_OUTCOMES:
        code = state.error_code or ErrorCode.INTERNAL
        metrics.record_refusal(tenant=tenant, reason=code.value)


def record_turn_code(code: ErrorCode) -> None:
    """Count a turn that failed before any state existed to describe it.

    `stream_turn`'s early returns are the callers: a `ToolError` out of the
    MCP handshake, and a graph that yielded no update at all. There is no
    extraction and no outcome to read, so the labels are the sentinel intent
    and `failed`, and the tenant is the one the request's telemetry context
    bound — the same source every log line of that request used, and the only
    one available here. Pinned by
    `test_agent_metrics.py::test_a_stream_that_fails_before_any_state_counts_intent_unknown`.
    """
    tenant = current_context().tenant_id
    metrics.record_turn(
        tenant=tenant,
        intent=metrics.UNKNOWN_INTENT,
        decision=TurnOutcome.FAILED.value,
    )
    metrics.record_refusal(tenant=tenant, reason=code.value)


_RISK_CLASS_KEY = "risk_class"
"""The key a tool's payload names its classification under (the design).

`classify_access_request` and `request_access` both answer it;
`search_policy`, which runs on nearly every turn, does not — so the read is a
`.get`, never a subscript. Pinned by
`test_agent_tracing.py::test_the_traces_risk_class_is_the_highest_any_tool_named`.
"""


def _turn_risk_class(state: TurnState) -> str | None:
    """The HIGHEST risk class any tool of this turn named, or `None`.

    The agent never classifies anything itself — the design puts that in the
    policy engine behind the MCP boundary — so the only place a turn's risk
    lives is in what the tools answered. A turn can carry several results
    (one round executes up to three calls, and a turn replans), and the
    highest is the honest summary for the same reason §9.2's overlapping rows
    resolve that way: a trace reporting `low` for a turn that also touched a
    critical system would understate exactly the turn an auditor is looking
    for.

    A payload naming no class contributes nothing, and a turn where nothing
    classified anything answers `None` — which the tracing facade DROPS rather
    than exporting as a null, so "never classified" and "classified as
    nothing" stay different facts.
    """
    highest: RiskClass | None = None
    for result in state.tool_results:
        named = result.payload.get(_RISK_CLASS_KEY)
        if not isinstance(named, str) or named not in set(RiskClass):
            continue
        candidate = RiskClass(named)
        if highest is None or severity(candidate) > severity(highest):
            highest = candidate
    return None if highest is None else highest.value


def trace_attributes(state: TurnState) -> dict[str, Any]:
    """The six values the design puts on a turn's trace, and nothing else.

    Beside `record_turn_state` because it is the same translation for a
    different sink: a `TurnState` becoming telemetry-safe values. `decision`
    is the outcome's VALUE and `refused` the same two outcomes the refusal
    counter uses, so the trace and the metrics cannot answer one turn
    differently.

    Every value here is a uuid, a closed enum member, a bool or a count.
    There is deliberately no `user_id`, no intent-free-text and nothing from
    the message: the design permits four fields in telemetry, and this map is
    published to a third-party store. The sweep proving it over a real turn is
    `test_agent_tracing.py::test_no_span_of_a_turn_carries_a_person_the_user_id_or_the_message`;
    the field set itself is pinned by
    `::test_the_attributes_are_exactly_the_six_planned_keys_read_off_the_state`.
    """
    return {
        "tenant_id": str(state.tenant_id),
        "session_id": str(state.session_id),
        "risk_class": _turn_risk_class(state),
        "decision": state.outcome.value,
        "refused": state.outcome in _REFUSED_OUTCOMES,
        "citations_count": len(state.citations),
    }
