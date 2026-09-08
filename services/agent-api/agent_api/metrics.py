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

from helpdesk.errors import ErrorCode
from helpdesk.obs import metrics
from helpdesk.obs.context import current_context

from agent_api.graph.state import TurnOutcome, TurnState

__all__ = ["record_turn_code", "record_turn_state"]

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
