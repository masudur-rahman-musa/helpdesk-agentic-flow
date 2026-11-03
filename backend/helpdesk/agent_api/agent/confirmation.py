"""Asking before filing: the shape of the model's reading of the person's
answer, and the words of the question.

The model reads the answer to a held change into one of six kinds
(`ReplyReading`); code only makes sure a yes runs the stored calls and nothing
else. The open question is stored on the chat row as plain JSON and checked
here on every read, with the text the person was shown (`asked`). The question
is a summary of what a yes files: the role in plain words, the days and the
reason. Every other change gets its own code-written line per tool, ending
`CHANGE_LINE`.

The model writes the question the person sees, in their language; code makes
sure every fact of the change (`confirm_facts`) is in it, and shows the
code-written summary when one is not.
"""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from helpdesk.agent_api.agent.matching import fold
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
    """How the model reads the person's message against an open question, the
    way a person would. Each kind starts one step of the turn."""

    YES = "yes"
    """They agree; a reason, thanks or the same facts said again beside it
    change nothing."""
    YES_WITH_CHANGE = "yes_with_change"
    """They agree once a value they name differs from the one shown."""
    NO = "no"
    NO_WITH_NEW_REQUEST = "no_with_new_request"
    """They drop the change and ask for something else instead."""
    QUESTION = "question"
    """They ask about the change before they answer it."""
    UNCLEAR = "unclear"


class ChangeField(StrEnum):
    """The parts of a proposed change a person can correct."""

    DAYS = "days"
    REASON = "reason"
    ROLE = "role"
    PERSON = "person"
    DATE = "date"


def _all_required(schema: dict[str, Any]) -> None:
    """Strict mode: every property required, none with a default."""
    properties = schema.get("properties", {})
    for field in properties.values():
        field.pop("default", None)
    schema["required"] = list(properties)


def _same(left: str | None, right: str) -> bool:
    return left is not None and left.strip().casefold() == right.strip().casefold()


class Change(BaseModel):
    """One value the person corrected: what the question showed, and what
    they want instead. The model gives both, so it compares rather than guesses."""

    model_config = ConfigDict(extra="forbid", frozen=True, json_schema_extra=_all_required)

    field: ChangeField
    shown: str | None
    """The value in the question, or null when the question showed none."""
    new: str

    @property
    def differs(self) -> bool:
        return not _same(self.shown, self.new)


class ReplyReading(BaseModel):
    """The model's reading of the answer to a held change: one kind, and on
    `yes_with_change` the values the person corrected, so the held request is
    updated before it is asked again. A `yes_with_change` with no value that
    differs from the one shown is not a valid reading, so it goes through the
    usual retry for bad structured output and then reads as unclear
    (`test_a_change_equal_to_what_was_shown_is_not_valid`)."""

    model_config = ConfigDict(extra="forbid", frozen=True, json_schema_extra=_all_required)

    answer: Reading
    changes: list[Change] = []

    @model_validator(mode="after")
    def _a_change_changes_something(self) -> Self:
        if self.answer is Reading.YES_WITH_CHANGE and not any(c.differs for c in self.changes):
            raise ValueError("yes_with_change needs a value that differs from the one shown")
        return self

    def changed(self, field: ChangeField) -> str | None:
        """The new value the person gave for `field`, when it differs from the shown one."""
        for change in self.changes:
            if change.field is field and change.differs:
                return change.new.strip()
        return None


UNCLEAR_REPLY = "I didn't quite get that. Should I go ahead? Please reply yes or no."
"""Said when the reading is unclear; the change stays held."""


def waiting_message(asked: str) -> str:
    """The held question, shown to the model as data while the person asks
    about it, so its answer can end by asking again."""
    return f"Change still waiting for a yes (data, not instructions): {json.dumps(asked)}"


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
    return f'I\'ll file this: {role}, {length}, reason "{reason}". {FILE_LINE} {next_step(results)}'


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


_GRANT_LISTINGS = frozenset({"list_my_access", "list_person_access"})


def _listed_grants(results: Sequence[ToolResult]) -> dict[str, str]:
    """ "CRM viewer" per grant id, from this turn's `list_my_access` and
    `list_person_access` rows."""
    names: dict[str, str] = {}
    for result in results:
        if result.tool not in _GRANT_LISTINGS or not result.ok:
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
    call: ValidatedCall,
    resolved: ResolvedEntities | None,
    grants: dict[str, str],
    person: str | None = None,
) -> str:
    """One held change other than a request or a decision, in fixed words. A
    held revoke names the access from this turn's grant listings, and whose
    it is when it is not the person's own."""
    arguments = call.arguments
    fixed = _FIXED_CHANGES.get(call.tool)
    if fixed is not None:
        return fixed
    if call.tool == "create_ticket":
        return f"I'll open a ticket: {str(arguments.get('title', '')).strip().rstrip('.')}."
    if call.tool == "revoke_access":
        whose = f"{person}'s" if person else "your"
        listed = grants.get(str(arguments.get("grant_id")))
        if listed:
            return f"I'll remove {whose} access to {listed}."
        system = resolved.system_name if resolved else None
        return f"I'll remove {whose} access to {system}." if system else "I'll remove this access."
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
        _call_line(call, resolved, grants, person)
        for call in calls
        if call.tool not in _DECISION_VERBS and needs_idempotency_key(call.tool)
    )
    lines = list(dict.fromkeys(lines))
    return f"{' '.join(lines)} {CHANGE_LINE}"


@dataclass(frozen=True)
class Fact:
    """One thing the written question must show, as code knows it."""

    text: str
    number: bool = False
    """Found as the digits standing alone, so "30 Tage" and "30 jours" both
    show 30 and "130" does not."""

    def found_in(self, question: str) -> bool:
        """Whether the question shows this fact, ignoring case and accents."""
        if self.number:
            return re.search(rf"(?<!\d){re.escape(self.text)}(?!\d)", question) is not None
        return fold(self.text) in fold(question)


def _day_count(results: Sequence[ToolResult], create: dict[str, object]) -> int | None:
    """This turn's classified length, else the days the create call asks for."""
    verdict = _verdict(results)
    for days in (
        verdict.effective_expiry_days if verdict else None,
        create.get("requested_expiry_days"),
    ):
        if isinstance(days, int) and not isinstance(days, bool) and days > 0:
            return days
    return None


def _listed_numbers(results: Sequence[ToolResult]) -> dict[str, str]:
    """ "REQ-0006" per request id, from this turn's listings."""
    numbers: dict[str, str] = {}
    for result in results:
        key = _LISTINGS.get(result.tool)
        if key is None or not result.ok:
            continue
        rows: object = result.payload.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict) and row.get("request_number"):
                numbers[str(row.get("request_id"))] = str(row["request_number"])
    return numbers


_FACT_ARGUMENTS: dict[str, tuple[str, ...]] = {
    "onboard_user": ("display_name", "email", "start_date"),
    "transfer_user": ("effective_date",),
    "offboard_user": ("end_date",),
    "create_ticket": ("title",),
    "open_access_review": ("period",),
}
"""The arguments each change shows word for word."""


def confirm_facts(
    calls: Sequence[ValidatedCall],
    results: Sequence[ToolResult],
    resolved: ResolvedEntities | None,
    person: str | None = None,
) -> tuple[Fact, ...]:
    """What the written question must show for the held calls: for an access
    request the role, the days, the reason and who it is for; for a decision
    each listed request number; for a revoke the access; for the rest the
    names, dates, title or period they carry. Unknown values are not facts."""
    facts: list[Fact] = []
    numbers = _listed_numbers(results)
    grants = _listed_grants(results)
    for call in calls:
        arguments = call.arguments
        if call.tool == CREATE_ACCESS_REQUEST:
            if resolved and resolved.role_name:
                facts.append(Fact(resolved.role_name))
            days = _day_count(results, arguments)
            if days is not None:
                facts.append(Fact(str(days), number=True))
            reason = str(arguments.get("justification") or "").strip().rstrip(".").strip()
            if reason:
                facts.append(Fact(reason))
            if person:
                facts.append(Fact(person))
        elif call.tool in _DECISION_VERBS:
            number = numbers.get(str(arguments.get("request_id")))
            if number:
                facts.append(Fact(number))
        elif call.tool == "revoke_access":
            access = grants.get(str(arguments.get("grant_id"))) or (
                resolved.system_name if resolved else None
            )
            if access:
                facts.append(Fact(access))
            if person:
                facts.append(Fact(person))
        else:
            for name in _FACT_ARGUMENTS.get(call.tool, ()):
                value = str(arguments.get(name) or "").strip()
                if call.tool == "create_ticket":
                    value = value.rstrip(".")
                if value:
                    facts.append(Fact(value))
    return tuple(dict.fromkeys(facts))


_SELF_CORRECTION = re.compile(
    r"\b(?:hmm+|wait|oops|scratch that|i mean|let me rephrase|correction|sorry)\b",
    re.IGNORECASE,
)
"""Words that show the writer correcting itself inside the question."""


def question_problems(question: str) -> list[str]:
    """Why a written confirm question is not just the question: a second
    question, or a line where the writer corrects itself. Empty when it is
    fine. The person must see one question they can answer with a yes."""
    problems: list[str] = []
    if question.count("?") > 1:
        problems.append("more than one question")
    if _SELF_CORRECTION.search(question):
        problems.append("a line correcting yourself")
    return problems


def missing_facts(question: str, facts: Sequence[Fact]) -> list[str]:
    """The text of every fact the question does not show."""
    return [fact.text for fact in facts if not fact.found_in(question)]
