"""The decision-word check on a composed reply, as a pure table.

The allowed words come from everything the turn returned, so a turn that ran
no tool allows none. Only successful results count. Unknown values allow
nothing, so a new status narrows the reply instead of widening it. Matching
is on whole lowercase words, so `approvals` never matches `approved`.
"""

import re
from collections.abc import Mapping
from typing import Any

from helpdesk.agent_api.agent.state import ToolResult, TurnOutcome, TurnState
from helpdesk.shared.models.access import RequestStatus
from helpdesk.shared.policy.types import Decision

DECISION_WORDS = frozenset({"approved", "granted", "blocked", "pending"})
"""The words a reply may use only if the turn returned them."""

_TOKENS = re.compile(r"[a-z]+")

_DECISION_PERMITS: Mapping[str, frozenset[str]] = {
    Decision.AUTO.value: frozenset({"approved", "granted"}),
    Decision.APPROVAL.value: frozenset({"pending"}),
    Decision.BLOCKED.value: frozenset({"blocked"}),
}
"""What each policy decision lets a reply say. `auto` runs the grant in
the same call, so both "approved" and "granted" are true."""

_STATUS_PERMITS: Mapping[str, frozenset[str]] = {
    RequestStatus.PENDING.value: frozenset({"pending"}),
    RequestStatus.APPROVED.value: frozenset({"approved"}),
    RequestStatus.EXECUTED.value: frozenset({"approved", "granted"}),
    RequestStatus.REJECTED.value: frozenset(),
    RequestStatus.FAILED.value: frozenset(),
}
"""What each request status lets a reply say. `approved` is not `granted`:
the grant may not have run yet. Statuses with no row allow nothing."""

POSTCHECK_FALLBACK_REPLY = (
    "Something went wrong while putting this answer into words, so I would "
    "rather say nothing about the outcome than describe it wrongly. Please "
    "open the requests page to see where this stands, or ask the service desk "
    "to look at it with you."
)
"""What a reply that contradicted twice ends on when the turn returned no
outcome. It claims none and uses none of the decision words."""

SEAT_NAMES: Mapping[str, str] = {
    "department_manager": "Department manager",
    "system_owner": "System owner",
    "it_admin": "IT admin",
    "hr": "HR",
}
"""Approver seats as the web app labels them, so the reply matches the card."""


def words_in(text: str) -> frozenset[str]:
    """The decision words `text` actually uses, as whole lowercase tokens."""
    return DECISION_WORDS & frozenset(_TOKENS.findall(text.lower()))


def permitted_words(state: TurnState) -> frozenset[str]:
    """The decision words this turn's own results entitle a reply to use."""
    permitted: set[str] = set()

    if state.outcome is TurnOutcome.AWAIT_APPROVAL:
        permitted |= {"pending"}

    for result in state.tool_results:
        if not result.ok:
            continue
        decision = result.payload.get("decision")
        if isinstance(decision, str):
            permitted |= _DECISION_PERMITS.get(decision, frozenset())
        status = result.payload.get("status")
        if isinstance(status, str):
            permitted |= _STATUS_PERMITS.get(status, frozenset())
        if result.payload.get("grant_id") is not None:
            permitted |= {"granted"}

    return frozenset(permitted)


def contradicts(reply: str, state: TurnState) -> bool:
    """Whether `reply` uses a decision word the turn did not return. Every word
    must be allowed, not just one."""
    return not words_in(reply) <= permitted_words(state)


_FILED_STATUSES = frozenset(
    {
        RequestStatus.PENDING.value,
        RequestStatus.APPROVED.value,
        RequestStatus.EXECUTED.value,
    }
)


def _filed(result: ToolResult) -> bool:
    """Whether one result filed or granted a request. A bare `decision` is a
    classification and files nothing."""
    return result.ok and (
        result.payload.get("status") in _FILED_STATUSES
        or result.payload.get("grant_id") is not None
    )


def request_filed(state: TurnState) -> bool:
    """Whether a successful call this turn already filed or granted a request."""
    if state.outcome is TurnOutcome.AWAIT_APPROVAL:
        return True
    return any(_filed(result) for result in state.tool_results)


def asks_after_filing(asks: bool, state: TurnState) -> bool:
    """Whether a reply the writer marked as asking (`asks`) asks although the
    request is already filed.

    The question reads as if an answer still mattered, and it does not. A
    blocked turn, or one that filed nothing, may still ask.
    """
    return asks and request_filed(state)


def asks_on_a_best_reading(asks: bool, state: TurnState) -> bool:
    """Whether a reply the writer marked as asking (`asks`) asks on a turn that
    may not ask one: the turn before was already a clarifying question."""
    return asks and state.best_reading


def _seats(payload: Mapping[str, Any]) -> str:
    """The approver seats in `payload` as one phrase, or empty when there are none."""
    approvers = payload.get("required_approvers")
    if not isinstance(approvers, list):
        return ""
    names = [
        SEAT_NAMES[slot]
        for approver in approvers
        if isinstance(approver, dict)
        and isinstance(slot := approver.get("slot"), str)
        and slot in SEAT_NAMES
    ]
    if len(names) < 2:
        return "".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"


def _from(payload: Mapping[str, Any]) -> str:
    seats = _seats(payload)
    return f" from the {seats}" if seats else ""


def _lasting(payload: Mapping[str, Any]) -> str:
    days = payload.get("effective_expiry_days")
    if isinstance(days, int) and not isinstance(days, bool) and days > 0:
        return f" It would last {days} days."
    return ""


def _filed_reply(payload: Mapping[str, Any]) -> str | None:
    """The sentence for a filed request, or `None` for a status with no sentence."""
    status = payload.get("status")
    if payload.get("grant_id") is not None or status == RequestStatus.EXECUTED.value:
        return "Your access is granted. You can find it on the requests page."
    if status == RequestStatus.APPROVED.value:
        return "Your request is approved, and the access is being set up."
    if status == RequestStatus.PENDING.value:
        return f"Your request is filed and waits for approval{_from(payload)}."
    return None


def _classified_reply(payload: Mapping[str, Any]) -> str | None:
    """The sentence for a classification that filed nothing."""
    decision = payload.get("decision")
    if decision == Decision.BLOCKED.value:
        return "This request is blocked by policy, so nothing was filed."
    if decision == Decision.APPROVAL.value:
        return (
            f"Nothing is filed yet. This access needs approval{_from(payload)}.{_lasting(payload)}"
        )
    if decision == Decision.AUTO.value:
        return (
            "Nothing is filed yet. This access can be given without anyone "
            f"else signing off.{_lasting(payload)}"
        )
    return None


def outcome_reply(state: TurnState) -> str:
    """The last-resort reply, said from the turn's own results with no model call.

    A filed request wins over a classification, and the latest result wins
    within each. Each sentence uses only the decision words its result earns
    and asks nothing, so it passes the check it replaces. A turn with no
    usable outcome gets the generic text.
    """
    ok = [result for result in reversed(state.tool_results) if result.ok]
    for result in ok:
        if _filed(result) and (reply := _filed_reply(result.payload)) is not None:
            return reply
    if state.outcome is TurnOutcome.AWAIT_APPROVAL:
        return "Your request is filed and waits for approval."
    for result in ok:
        if (reply := _classified_reply(result.payload)) is not None:
            return reply
    return POSTCHECK_FALLBACK_REPLY
