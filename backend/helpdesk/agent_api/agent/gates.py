"""Every turn decision as a pure function, with no framework and no I/O.
LangGraph only routes on what these return, so each is testable alone."""

import uuid
from collections.abc import Mapping, Sequence

from helpdesk.agent_api.agent.state import Intent, IntentExtraction, ToolResult, TurnOutcome

CONFIDENCE_THRESHOLD = 0.7
"""Inclusive. Lower lets a guess act; higher asks needless questions."""

MAX_DISAMBIGUATION_OPTIONS = 3
"""Few enough to read, and a cap on how much of a directory one word lists."""

REQUIRED_ENTITIES: Mapping[Intent, frozenset[str]] = {
    Intent.RESET_PASSWORD: frozenset(),
    Intent.UNLOCK_ACCOUNT: frozenset(),
    Intent.REQUEST_ACCESS: frozenset({"system"}),
    Intent.REVOKE_ACCESS: frozenset({"system"}),
    Intent.REPORT_MOVER: frozenset({"target_user", "department"}),
    Intent.ONBOARD: frozenset({"target_user", "department"}),
    Intent.OFFBOARD: frozenset({"target_user"}),
    Intent.OPEN_TICKET: frozenset(),
    Intent.ASK_POLICY: frozenset(),
    Intent.CHECK_MY_ACCESS: frozenset(),
    Intent.ASK_CAPABILITIES: frozenset(),
    Intent.APPROVALS: frozenset(),
    Intent.REVIEW: frozenset(),
    Intent.OTHER: frozenset(),
}
"""What each intent must name before the turn goes on. Every intent has a
row, so a new one cannot quietly require nothing. `role` is optional: the
template or policy engine can pick it."""


RETRIEVES_POLICY: Mapping[Intent, bool] = {
    Intent.RESET_PASSWORD: False,
    Intent.UNLOCK_ACCOUNT: False,
    Intent.REQUEST_ACCESS: True,
    Intent.REVOKE_ACCESS: False,
    Intent.REPORT_MOVER: False,
    Intent.ONBOARD: False,
    Intent.OFFBOARD: False,
    Intent.OPEN_TICKET: False,
    Intent.ASK_POLICY: True,
    Intent.CHECK_MY_ACCESS: False,
    Intent.ASK_CAPABILITIES: False,
    Intent.APPROVALS: False,
    Intent.REVIEW: False,
    Intent.OTHER: False,
}
"""Which intents search the policy text: a request, decided against policy,
and a policy question."""


def missing_required_entities(extraction: IntentExtraction) -> frozenset[str]:
    """Every missing required entity, so the user gets one question. Empty
    text counts as missing."""
    return frozenset(
        name
        for name in REQUIRED_ENTITIES[extraction.intent]
        if not _is_supplied(getattr(extraction.entities, name))
    )


def needs_clarification(extraction: IntentExtraction) -> bool:
    """Unsure of the intent, or missing something it needs."""
    return extraction.confidence < CONFIDENCE_THRESHOLD or bool(
        missing_required_entities(extraction)
    )


def clarify_outcome(extraction: IntentExtraction, asked_before: bool) -> TurnOutcome:
    """At most one clarifying question in a row. After a question,
    an unclear message goes on with its best reading, or is answered with no
    tools when something required is still missing, so nothing runs on a guess."""
    if not needs_clarification(extraction):
        return TurnOutcome.CONTINUE
    if not asked_before:
        return TurnOutcome.CLARIFY
    if missing_required_entities(extraction):
        return TurnOutcome.ANSWER
    return TurnOutcome.CONTINUE


def candidate_outcome(candidate_count: int) -> TurnOutcome:
    """None clarifies; several disambiguate, since picking one would be a
    guess."""
    if candidate_count == 0:
        return TurnOutcome.CLARIFY
    if candidate_count == 1:
        return TurnOutcome.CONTINUE
    return TurnOutcome.DISAMBIGUATE


def should_retrieve(intent: Intent) -> bool:
    """Whether this turn searches the policy text."""
    return RETRIEVES_POLICY[intent]


def retrieval_outcome(refused: bool) -> TurnOutcome:
    """Follows the search tool's own `refused` flag; the threshold lives there."""
    return TurnOutcome.REFUSE if refused else TurnOutcome.CONTINUE


def capped_options[T](candidates: Sequence[T]) -> tuple[T, ...]:
    """The first few candidates, keeping the matcher's best-first order."""
    return tuple(candidates[:MAX_DISAMBIGUATION_OPTIONS])


def _is_supplied(value: object) -> bool:
    """Blank text counts as missing: it would match every candidate."""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


INTENT_TOOLS: Mapping[Intent, frozenset[str]] = {
    Intent.RESET_PASSWORD: frozenset({"reset_own_password", "create_ticket"}),
    Intent.UNLOCK_ACCOUNT: frozenset({"unlock_own_account", "create_ticket"}),
    Intent.REQUEST_ACCESS: frozenset(
        {
            "get_me",
            "list_my_access",
            "classify_access_request",
            "create_access_request",
            "create_ticket",
        }
    ),
    Intent.REVOKE_ACCESS: frozenset({"list_my_access", "revoke_access", "create_ticket"}),
    Intent.REPORT_MOVER: frozenset({"transfer_user", "create_ticket"}),
    Intent.ONBOARD: frozenset({"onboard_user", "create_ticket"}),
    Intent.OFFBOARD: frozenset({"offboard_user", "create_ticket"}),
    Intent.OPEN_TICKET: frozenset({"create_ticket"}),
    Intent.ASK_POLICY: frozenset(),
    Intent.CHECK_MY_ACCESS: frozenset({"get_me", "list_my_access", "list_my_requests"}),
    Intent.ASK_CAPABILITIES: frozenset({"list_requestable_roles"}),
    Intent.APPROVALS: frozenset({"list_pending_approvals", "approve_request", "reject_request"}),
    Intent.REVIEW: frozenset({"open_access_review", "list_review_items", "decide_review_item"}),
    Intent.OTHER: frozenset(),
}
"""Which tools the planner may pick per intent. Not permission (RBAC does
that): it narrows what a prompt injection can aim at. `search_policy` is in
no row, since search runs before planning. Empty rows skip the model call."""

MAX_TOOL_CALLS = 6
"""The most tool calls one turn may run, inclusive."""

MAX_CALLS_PER_ROUND = 3
"""A classification, the request it supports, and a listing. More, and one
round could spend the whole turn budget."""

MAX_TURN_TOKENS = 40000
"""Inclusive; about two planning rounds plus a reply."""

TURN_DEADLINE_SECONDS = 90.0
"""Inclusive. Three MCP read timeouts, so a stalled tool times out first."""


def allowed_tools(intent: Intent) -> frozenset[str]:
    """The tools this intent's planner may propose."""
    return INTENT_TOOLS[intent]


def may_plan_another_round(
    *, tool_calls_made: int, tokens_used: int, elapsed_seconds: float
) -> bool:
    """Whether the turn may plan again. Bounds are inclusive; any one spent stops it."""
    if tool_calls_made > MAX_TOOL_CALLS:
        return False
    if tokens_used > MAX_TURN_TOKENS:
        return False
    return not elapsed_seconds > TURN_DEADLINE_SECONDS


def validation_outcome(accepted: bool) -> TurnOutcome:
    """All or nothing: part of a plan is a half-done change."""
    return TurnOutcome.CONTINUE if accepted else TurnOutcome.REFUSE


def execution_outcome(validated_count: int) -> TurnOutcome:
    """An empty accepted round ends the turn instead of looping."""
    return TurnOutcome.CONTINUE if validated_count else TurnOutcome.DONE


def exhausted_outcome(result_count: int) -> TurnOutcome:
    """A spent budget replies about what was done, or refuses if nothing was.
    The refusal says no policy covers it, which is false after a real change."""
    return TurnOutcome.DONE if result_count else TurnOutcome.REFUSE


CREATE_ACCESS_REQUEST = "create_access_request"
"""The one tool that can pause a turn. Named, because
`classify_access_request` returns the same `decision` field but files
nothing."""


def held_round_outcome(*, holds_create: bool, confirmed: bool) -> TurnOutcome | None:
    """`CONFIRM` when an accepted round would file a request nobody said yes
    to. A confirmed turn already ran the calls the person agreed to, so a new
    create there is dropped with `DONE`: it was never asked about. `None`
    leaves the round as it was."""
    if not holds_create:
        return None
    return TurnOutcome.DONE if confirmed else TurnOutcome.CONFIRM


APPROVAL_DECISION = "approval"
"""The decision value for a request that needs an approval chain."""


def _approval_payload(results: Sequence[ToolResult]) -> Mapping[str, object] | None:
    """The latest successful `create_access_request` answer that pauses."""
    for result in reversed(tuple(results)):
        if result.tool != CREATE_ACCESS_REQUEST or not result.ok:
            continue
        if str(result.payload.get("decision", "")) != APPROVAL_DECISION:
            return None
        return result.payload
    return None


def approval_request_id(results: Sequence[ToolResult]) -> uuid.UUID | None:
    """The request a paused turn waits on, or `None` if it is not a valid uuid:
    a pause nobody can match would never resume."""
    payload = _approval_payload(results)
    if payload is None:
        return None
    value: object = payload.get("request_id")
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value.strip())
    except ValueError:
        return None


def approval_seat_count(results: Sequence[ToolResult]) -> int:
    """How many approvals the request waits for; zero if unreadable."""
    payload = _approval_payload(results)
    if payload is None:
        return 0
    approvers: object = payload.get("required_approvers")
    if not isinstance(approvers, list):
        return 0
    return len(approvers)


def approval_outcome(results: Sequence[ToolResult]) -> TurnOutcome:
    """Whether this round pauses for approval. `FAILED` when no usable id came
    back, rather than a pause nobody could ever decide."""
    if _approval_payload(results) is None:
        return TurnOutcome.CONTINUE
    if approval_request_id(results) is None:
        return TurnOutcome.FAILED
    return TurnOutcome.AWAIT_APPROVAL


APPROVE_REQUEST = "approve_request"
"""A resumed turn sees the approver's decision as this tool's result. It did
run, in another process, when the approver decided."""

REJECT_REQUEST = "reject_request"
"""The same for a rejection."""

DECIDED_RESULTS: Mapping[str, tuple[str, bool]] = {
    "approved": (APPROVE_REQUEST, True),
    "executed": (APPROVE_REQUEST, True),
    "rejected": (REJECT_REQUEST, True),
    "failed": (APPROVE_REQUEST, False),
}
"""Request statuses a resume may act on. Left out: `pending`, `rolled_back`
and `expired`. `failed` means approved, but the fresh policy check refused
the grant. Plain strings keep this module free of the ORM."""


def decision_result(*, status: str, request_id: uuid.UUID) -> ToolResult | None:
    """A decided request as a tool result, from the stored status only, so a
    caller cannot turn a rejection into a grant."""
    row = DECIDED_RESULTS.get(status)
    if row is None:
        return None
    tool, ok = row
    return ToolResult(
        tool=tool,
        ok=ok,
        payload={"request_id": str(request_id), "status": status},
    )
