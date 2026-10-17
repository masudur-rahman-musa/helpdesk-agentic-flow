"""The decision-word check on a composed reply, as a pure table.

The allowed words come from everything the turn returned, so a turn that ran
no tool allows none. Only successful results count. Unknown values allow
nothing, so a new status narrows the reply instead of widening it. Matching
is on whole lowercase words, so `approvals` never matches `approved`.
"""

import re
from collections.abc import Mapping

from helpdesk.agent_api.agent.state import TurnOutcome, TurnState
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
"""What a reply that contradicted twice ends on. It claims no outcome and
uses none of the decision words."""


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
