"""Asking before filing: the shape of the model's reading of the person's
answer, and the words of the question.

The model reads the answer to a held change into one of four fixed values
(`ReplyReading`); code only makes sure a yes runs the stored calls and nothing
else. The open question is stored on the chat row as plain JSON and checked
here on every read, with the text the person was shown (`asked`). The question
is a summary of what a yes files: the role in plain words, the days and the
reason. Every other change gets its own code-written line per tool, ending
`CHANGE_LINE`.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, ValidationError

from helpdesk.agent_api.agent.role_words import plain_role_name
from helpdesk.agent_api.agent.state import (
    Citation,
    IntentExtraction,
    ResolvedEntities,
    ToolResult,
    ValidatedCall,
)
from helpdesk.agent_api.agent.tool_args import needs_idempotency_key
from helpdesk.shared.policy.types import ApproverSlot, Decision

CLASSIFY_ACCESS_REQUEST = "classify_access_request"
CREATE_ACCESS_REQUEST = "create_access_request"

FILE_LINE = "Say yes to file, or tell me what to change."
CHANGE_LINE = "Say yes to go ahead, or no to stop."
"""Ends the summary of every change other than an access request."""


USUAL_LENGTH = "its usual length"
"""Said for the days when neither the policy nor the person gave a number."""

OPEN_QUESTION_LIFE = timedelta(hours=24)
"""A question this old or older counts as none: the person has moved on."""


class Reading(StrEnum):
    """How the model reads the person's message against an open question."""

    YES = "yes"
    NO = "no"
    NEW_INFO = "new_info"
    """A change to the proposal or a question about it, "yes, but" included."""
    UNCLEAR = "unclear"


class ReplyReading(BaseModel):
    """The model's reading of the answer to a held change: one fixed value."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    answer: Reading


UNCLEAR_REPLY = "I didn't quite get that. Should I go ahead? Please reply yes or no."
"""Said when the reading is unclear; the change stays held."""


class OpenQuestion(BaseModel):
    """What a yes files: the held calls with their keys, and what the turn knew."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    asked_at: datetime
    message: str
    calls: tuple[ValidatedCall, ...]
    extraction: IntentExtraction
    resolved: ResolvedEntities
    citations: tuple[Citation, ...] = ()
    asked: str = ""
    """The question the person was shown; empty on rows stored before it was kept."""


def parse_open_question(raw: object, *, now: datetime) -> OpenQuestion | None:
    """The stored question, or `None` when it is missing, badly shaped, empty
    or `OPEN_QUESTION_LIFE` old or older."""
    if not isinstance(raw, dict):
        return None
    try:
        question = OpenQuestion.model_validate(raw)
    except ValidationError:
        return None
    if not question.calls or now - question.asked_at >= OPEN_QUESTION_LIFE:
        return None
    return question


@dataclass(frozen=True)
class _NextStepWords:
    """What a yes does next."""

    undecided: str
    granted: str
    blocked: str
    approval: str
    """Takes `{who}`."""
    anyone: str
    and_: str
    seats: dict[ApproverSlot, str]


_NEXT_STEP = _NextStepWords(
    undecided="If you say yes, I will file it and the policy decides once it is filed.",
    granted="If you say yes, it is granted right away.",
    blocked="The rules block it, so if you say yes it will be filed and refused.",
    approval="If you say yes, I will file it and it needs approval from {who}.",
    anyone="an approver",
    and_="and",
    seats={
        ApproverSlot.DEPARTMENT_MANAGER: "your manager",
        ApproverSlot.SYSTEM_OWNER: "the system owner",
        ApproverSlot.IT_ADMIN: "IT admin",
        ApproverSlot.HR: "HR",
    },
)


class _Verdict(BaseModel):
    """The classify fields the next step reads."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    decision: Decision
    required_approvers: list[dict[str, object]] = []
    effective_expiry_days: int | None = None


def _verdict(results: Sequence[ToolResult]) -> _Verdict | None:
    """The last successful classification, checked, or `None`."""
    for result in reversed(tuple(results)):
        if result.tool != CLASSIFY_ACCESS_REQUEST or not result.ok:
            continue
        try:
            return _Verdict.model_validate(result.payload)
        except ValidationError:
            return None
    return None


def _joined(parts: Sequence[str], and_: str = "and") -> str:
    """ "a", "a and b", "a, b and c"."""
    if len(parts) <= 1:
        return "".join(parts)
    return f"{', '.join(parts[:-1])} {and_} {parts[-1]}"


def next_step(results: Sequence[ToolResult]) -> str:
    """What happens after a yes, from this turn's classification only."""
    wording = _NEXT_STEP
    verdict = _verdict(results)
    if verdict is None:
        return wording.undecided
    if verdict.decision is Decision.AUTO:
        return wording.granted
    if verdict.decision is Decision.BLOCKED:
        return wording.blocked
    seats = []
    for approver in verdict.required_approvers:
        try:
            seats.append(wording.seats[ApproverSlot(str(approver.get("slot")))])
        except ValueError:
            continue
    who = _joined(seats, wording.and_) or wording.anyone
    return wording.approval.format(who=who)


def _held_create(calls: Sequence[ValidatedCall]) -> dict[str, object]:
    """The arguments of the held create call, or none when there is none."""
    for call in calls:
        if call.tool == CREATE_ACCESS_REQUEST:
            return call.arguments
    return {}


def _days(days: object) -> str | None:
    if not isinstance(days, int) or isinstance(days, bool) or days <= 0:
        return None
    return "1 day" if days == 1 else f"{days} days"


def summary_reply(
    resolved: ResolvedEntities | None,
    results: Sequence[ToolResult],
    held_calls: Sequence[ValidatedCall],
    person: str | None = None,
) -> str:
    """ "I'll file this: <role>, <N> days, reason "<reason>"." then the file
    line and what happens next. The days are this turn's classified length,
    else the days the held create call asks for, else `USUAL_LENGTH`. A
    request for someone else names them after the role ("for Dana")."""
    create = _held_create(held_calls)
    verdict = _verdict(results)
    length = (
        _days(verdict.effective_expiry_days if verdict else None)
        or _days(create.get("requested_expiry_days"))
        or USUAL_LENGTH
    )
    role = (resolved.role_name if resolved else None) or "this access"
    if person:
        role = f"{role} for {person}"
    reason = str(create.get("justification") or "").strip()
    return (
        f'I\'ll file this: {role}, {length}, reason "{reason}". '
        f"{FILE_LINE} {next_step(results)}"
    )


_FIXED_CHANGES: dict[str, str] = {
    "reset_own_password": "I'll reset your password.",
    "unlock_own_account": "I'll unlock your account.",
}

_DECISION_VERBS: dict[str, str] = {
    "approve_request": "approve",
    "reject_request": "reject",
    "cancel_request": "cancel",
}

_LISTINGS: dict[str, str] = {"list_pending_approvals": "approvals", "list_my_requests": "requests"}
"""Each listing a request is named from, and the key its rows sit under."""


def _listed_names(results: Sequence[ToolResult]) -> dict[str, str]:
    """ "REQ-0006 Data Warehouse engineer" per request id, from this turn's listings."""
    names: dict[str, str] = {}
    for result in results:
        key = _LISTINGS.get(result.tool)
        if key is None or not result.ok:
            continue
        rows: object = result.payload.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            role = plain_role_name(str(row.get("system_name", "")), str(row.get("role_slug", "")))
            names[str(row.get("request_id"))] = f"{row.get('request_number', '')} {role}".strip()
    return names


def _listed_grants(results: Sequence[ToolResult]) -> dict[str, str]:
    """ "CRM viewer" per grant id, from this turn's `list_my_access` rows."""
    names: dict[str, str] = {}
    for result in results:
        if result.tool != "list_my_access" or not result.ok:
            continue
        rows: object = result.payload.get("grants")
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict):
                names[str(row.get("grant_id"))] = plain_role_name(
                    str(row.get("system_name", "")), str(row.get("role_slug", ""))
                )
    return names


def _decision_line(verb: str, ids: Sequence[str], names: dict[str, str]) -> str:
    """ "I'll approve these 2 requests: REQ-0006 ..., REQ-0019 ..."."""
    named = ", ".join(names.get(request_id, "a request") for request_id in ids)
    if len(ids) == 1:
        return f"I'll {verb} this request: {named}."
    return f"I'll {verb} these {len(ids)} requests: {named}."


def _call_line(
    call: ValidatedCall, resolved: ResolvedEntities | None, grants: dict[str, str]
) -> str:
    """One held change other than a request or a decision, in fixed words. A
    held revoke names the access from this turn's `list_my_access` rows."""
    arguments = call.arguments
    fixed = _FIXED_CHANGES.get(call.tool)
    if fixed is not None:
        return fixed
    if call.tool == "create_ticket":
        return f"I'll open a ticket: {str(arguments.get('title', '')).strip().rstrip('.')}."
    if call.tool == "revoke_access":
        listed = grants.get(str(arguments.get("grant_id")))
        if listed:
            return f"I'll remove your access to {listed}."
        system = resolved.system_name if resolved else None
        return f"I'll remove your access to {system}." if system else "I'll remove this access."
    if call.tool == "onboard_user":
        return (
            f"I'll set up starting access for {arguments.get('display_name')} "
            f"({arguments.get('email')}), "
            f"starting {arguments.get('start_date')}."
        )
    if call.tool == "transfer_user":
        return (
            f"I'll move this person to their new department on {arguments.get('effective_date')}."
        )
    if call.tool == "offboard_user":
        return f"I'll remove a leaver's access, last day {arguments.get('end_date')}."
    if call.tool == "open_access_review":
        return f"I'll open an access review for {arguments.get('period')}."
    if call.tool == "decide_review_item":
        return f"I'll mark this flagged access as {arguments.get('decision')}."
    return "I'll make this change."


def change_summary(
    calls: Sequence[ValidatedCall],
    results: Sequence[ToolResult],
    resolved: ResolvedEntities | None,
    person: str | None = None,
) -> str:
    """What a yes runs, in code-written words. A held access request keeps its
    own summary (`summary_reply`); every other change gets one line per tool,
    approvals and rejections one line each naming the listed requests, then
    `CHANGE_LINE`."""
    if any(call.tool == CREATE_ACCESS_REQUEST for call in calls):
        return summary_reply(resolved, results, calls, person)
    names = _listed_names(results)
    lines: list[str] = []
    for tool, verb in _DECISION_VERBS.items():
        ids = [str(call.arguments.get("request_id")) for call in calls if call.tool == tool]
        if ids:
            lines.append(_decision_line(verb, ids, names))
    grants = _listed_grants(results)
    lines.extend(
        _call_line(call, resolved, grants)
        for call in calls
        if call.tool not in _DECISION_VERBS and needs_idempotency_key(call.tool)
    )
    lines = list(dict.fromkeys(lines))
    return f"{' '.join(lines)} {CHANGE_LINE}"
