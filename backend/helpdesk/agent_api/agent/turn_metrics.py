"""Turns a finished turn into metric labels and trace values, the same way
for every driver. Labels are enum values or the tenant id, never free text."""

from typing import Any

from helpdesk.agent_api.agent.postcheck import request_filed
from helpdesk.agent_api.agent.state import TurnOutcome, TurnState
from helpdesk.shared.core.errors import ErrorCode
from helpdesk.shared.core.observability import metrics
from helpdesk.shared.core.observability.context import current_context
from helpdesk.shared.policy.types import RiskClass, severity

__all__ = ["record_turn_code", "record_turn_state", "trace_attributes", "turn_risk_class"]

_REFUSED_OUTCOMES = frozenset({TurnOutcome.REFUSE, TurnOutcome.FAILED})
"""Outcomes also counted as refusals."""


def record_turn_state(state: TurnState) -> None:
    """Count one finished turn, and its refusal. Called where a turn ends, never
    in a node, which LangGraph reruns on resume. Unclassified turns count as
    `unknown`."""
    tenant = str(state.tenant_id)
    intent = metrics.UNKNOWN_INTENT if state.extraction is None else state.extraction.intent.value
    metrics.record_turn(tenant=tenant, intent=intent, decision=state.outcome.value)
    if state.outcome in _REFUSED_OUTCOMES:
        code = state.error_code or ErrorCode.INTERNAL
        metrics.record_refusal(tenant=tenant, reason=code.value)
    ending = _ending(state)
    if ending is not None:
        metrics.record_turn_ending(tenant=tenant, outcome=ending)
    if ending == "filed":
        # The history holds the earlier messages; this turn's own is not in it yet.
        earlier = sum(1 for line in state.history if line.startswith(_USER_LINE))
        metrics.record_turns_to_file(tenant=tenant, user_messages=earlier + 1)


_USER_LINE = "user:"
"""How a user message starts in `TurnState.history`."""

_ASKED_OUTCOMES = frozenset({TurnOutcome.CLARIFY, TurnOutcome.DISAMBIGUATE, TurnOutcome.CONFIRM})


def _ending(state: TurnState) -> str | None:
    """How the turn ended: the first match of failed, refused, stopped, unclear,
    filed, asked. A plain answer is `None`."""
    if state.outcome is TurnOutcome.FAILED:
        return "failed"
    if state.outcome is TurnOutcome.REFUSE:
        return "refused"
    if state.outcome is TurnOutcome.STOP:
        return "stopped"
    if state.best_reading:
        return "unclear"
    if request_filed(state):
        return "filed"
    if state.outcome in _ASKED_OUTCOMES:
        return "asked"
    return None


def record_turn_code(code: ErrorCode) -> None:
    """Count a turn that failed before any state existed."""
    tenant = current_context().tenant_id
    metrics.record_turn(
        tenant=tenant,
        intent=metrics.UNKNOWN_INTENT,
        decision=TurnOutcome.FAILED.value,
    )
    metrics.record_refusal(tenant=tenant, reason=code.value)


_RISK_CLASS_KEY = "risk_class"
"""Not every tool returns it, so it is read with `.get`."""


def turn_risk_class(state: TurnState) -> str | None:
    """The highest risk class any tool named, or `None`. The highest, so a turn
    that touched a critical system never shows as `low`."""
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
    """The values a turn's trace carries: ids, enum values, a bool and a count.
    No user id and no message text, since this goes to an outside store."""
    return {
        "tenant_id": str(state.tenant_id),
        "session_id": str(state.session_id),
        "risk_class": turn_risk_class(state),
        "decision": state.outcome.value,
        "refused": state.outcome in _REFUSED_OUTCOMES,
        "citations_count": len(state.citations),
    }
