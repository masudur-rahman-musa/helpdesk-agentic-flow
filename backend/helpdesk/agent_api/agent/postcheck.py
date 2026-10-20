"""The decision-word check on a composed reply, as a pure table.

The allowed words come from everything the turn returned, so a turn that ran
no tool allows none (a follow-up also allows the words of the reply it
reshapes). Only successful results count. Unknown values allow
nothing, so a new status narrows the reply instead of widening it. Matching
is on whole lowercase words, so `approvals` never matches `approved`.
"""

import re
from collections.abc import Mapping
from typing import Any

from helpdesk.agent_api.agent.follow_up import OUT_OF_SCOPE_REPLY, last_assistant_reply
from helpdesk.agent_api.agent.gates import ALREADY_REQUESTED
from helpdesk.agent_api.agent.state import Intent, ToolResult, TurnOutcome, TurnState
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
    "Sorry, I couldn't answer that one properly. Try asking it another way, "
    "or open My requests to see where things stand."
)
"""What a reply that contradicted twice ends on when the turn returned nothing
code can word. It claims nothing, uses no decision word, and never reads like a
fault in the app (`test_fallback_text_is_plain`)."""

LIST_KEY = "requests"
"""Where `list_my_requests` puts its rows, each with its own `status`."""


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
    """The decision words this turn's own results entitle a reply to use. A
    follow-up also gets the words of the last stored assistant reply, which
    already passed this check; never the new message's, and never on any other
    intent (`test_follow_up.py`)."""
    permitted: set[str] = set()

    if state.extraction is not None and state.extraction.intent is Intent.FOLLOW_UP:
        earlier = last_assistant_reply(state.history)
        if earlier is not None:
            permitted |= words_in(earlier)

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
        # A listing carries each status inside its rows
        # (`test_list_statuses_permit_their_words`).
        for row in _rows(result.payload):
            row_status = row.get("status")
            if isinstance(row_status, str):
                permitted |= _STATUS_PERMITS.get(row_status, frozenset())

    return frozenset(permitted)


def _rows(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The rows of a listing result; empty when the payload is not one."""
    rows = payload.get(LIST_KEY)
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, Mapping)]


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
    return (
        result.ok
        and result.payload.get(ALREADY_REQUESTED) is not True
        and (
            result.payload.get("status") in _FILED_STATUSES
            or result.payload.get("grant_id") is not None
        )
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


def _repeat_reply(payload: Mapping[str, Any]) -> str | None:
    """The sentence for a request that was already open, so nothing was filed."""
    if payload.get(ALREADY_REQUESTED) is not True:
        return None
    number = payload.get("request_number")
    named = f" ({number})" if isinstance(number, str) and number else ""
    if payload.get("status") == RequestStatus.APPROVED.value:
        return f"You already asked for this{named}. It is approved and waits to be granted."
    return (
        f"You already asked for this{named} and it is waiting for approval, "
        "so I have not filed it again."
    )


_LISTED_PHRASES: tuple[tuple[str, str, str], ...] = (
    (RequestStatus.PENDING.value, "request waiting for approval", "requests waiting for approval"),
    (
        RequestStatus.APPROVED.value,
        "request approved and being set up",
        "requests approved and being set up",
    ),
    (RequestStatus.EXECUTED.value, "request granted", "requests granted"),
    (RequestStatus.REJECTED.value, "request turned down", "requests turned down"),
    (
        RequestStatus.FAILED.value,
        "request that could not be completed",
        "requests that could not be completed",
    ),
)
"""How a listing reply names each status, one and many. Each phrase uses only
the decision words that status permits."""


def _listing_reply(payload: Mapping[str, Any]) -> str | None:
    """A count by status for a listing, with the waiting ones by number."""
    rows = payload.get(LIST_KEY)
    if not isinstance(rows, list):
        return None
    if not rows:
        return "You have no access requests."
    parts: list[str] = []
    for status, one, many in _LISTED_PHRASES:
        numbers = [
            str(row.get("request_number")) for row in _rows(payload) if row.get("status") == status
        ]
        if not numbers:
            continue
        part = f"{len(numbers)} {one if len(numbers) == 1 else many}"
        if status == RequestStatus.PENDING.value:
            part += f" ({', '.join(numbers)})"
        parts.append(part)
    if not parts:
        return None
    return f"You have {'; '.join(parts)}."


def outcome_reply(state: TurnState) -> str:
    """The last-resort reply, said from the turn's own results with no model call.

    A filed request wins over a classification, and the latest result wins
    within each. Each sentence uses only the decision words its result earns
    and asks nothing, so it passes the check it replaces. A turn with no
    usable outcome gets the generic text.
    """
    ok = [result for result in reversed(state.tool_results) if result.ok]
    for result in ok:
        if (reply := _repeat_reply(result.payload)) is not None:
            return reply
    for result in ok:
        if _filed(result) and (reply := _filed_reply(result.payload)) is not None:
            return reply
    if state.outcome is TurnOutcome.AWAIT_APPROVAL:
        return "Your request is filed and waits for approval."
    for result in ok:
        if (reply := _classified_reply(result.payload)) is not None:
            return reply
    for result in ok:
        if (reply := _listing_reply(result.payload)) is not None:
            return reply
    if state.extraction is not None and state.extraction.intent is Intent.OTHER:
        return OUT_OF_SCOPE_REPLY
    return POSTCHECK_FALLBACK_REPLY
