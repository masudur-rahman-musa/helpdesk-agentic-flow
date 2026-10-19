"""Asking before filing: read the person's answer and word the question.

Pure, so the yes that files a request is read by fixed word lists, never by the
model: a misread yes would file. The open question is stored on the
chat row as plain JSON and checked here on every read.
"""

import re
from collections.abc import Sequence
from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, ValidationError

from helpdesk.agent_api.agent.state import (
    Citation,
    IntentExtraction,
    ResolvedEntities,
    ToolResult,
    ValidatedCall,
)
from helpdesk.shared.policy.types import ApproverSlot, Decision

CLASSIFY_ACCESS_REQUEST = "classify_access_request"

YES_WORDS = frozenset({"yes", "yep", "yeah", "correct", "right", "sure", "ok", "okay", "confirm"})
NO_WORDS = frozenset({"no", "nope", "wrong"})

MAX_YES_WORDS = 5
"""A longer message that starts with yes may add a change, so it is not a yes."""

MAX_NO_WORDS = 3
"""Longer, and a no is most likely a correction ("no, I meant CRM")."""

OPEN_QUESTION_LIFE = timedelta(hours=24)
"""A question this old or older counts as none: the person has moved on."""

NOTHING_FILED_REPLY = (
    "Okay, nothing has been filed. What do you need instead? Tell me the "
    "system and the role, and I will check it with you before filing."
)
"""The fixed reply to a bare no. No model call, so nothing new is planned."""

_WORD = re.compile(r"[a-z']+")


class Answer(StrEnum):
    """How the next message reads against an open question."""

    YES = "yes"
    NO = "no"
    OTHER = "other"


def read_answer(message: str) -> Answer:
    """A yes or a bare no by its first word and its length; anything else is other."""
    words = _WORD.findall(message.lower())
    if not words:
        return Answer.OTHER
    if words[0] in YES_WORDS and len(words) <= MAX_YES_WORDS:
        return Answer.YES
    if words[0] in NO_WORDS and len(words) <= MAX_NO_WORDS:
        return Answer.NO
    return Answer.OTHER


class OpenQuestion(BaseModel):
    """What a yes files: the held calls with their keys, and what the turn knew."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    asked_at: datetime
    message: str
    calls: tuple[ValidatedCall, ...]
    extraction: IntentExtraction
    resolved: ResolvedEntities
    citations: tuple[Citation, ...] = ()


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


_SEAT_WORDS: dict[ApproverSlot, str] = {
    ApproverSlot.DEPARTMENT_MANAGER: "your manager",
    ApproverSlot.SYSTEM_OWNER: "the system owner",
    ApproverSlot.IT_ADMIN: "IT admin",
    ApproverSlot.HR: "HR",
}


class _Verdict(BaseModel):
    """The classify fields the next step reads."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    decision: Decision
    required_approvers: list[dict[str, object]] = []


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


def _joined(words: Sequence[str]) -> str:
    """ "a", "a and b", "a, b and c"."""
    if len(words) <= 1:
        return "".join(words)
    return f"{', '.join(words[:-1])} and {words[-1]}"


def next_step(results: Sequence[ToolResult]) -> str:
    """What happens after a yes, from this turn's classification only."""
    verdict = _verdict(results)
    if verdict is None:
        return "If you say yes, I will file it and the policy decides once it is filed."
    if verdict.decision is Decision.AUTO:
        return "If you say yes, it is granted right away."
    if verdict.decision is Decision.BLOCKED:
        return "The rules block it, so if you say yes it will be filed and refused."
    seats = []
    for approver in verdict.required_approvers:
        try:
            seats.append(_SEAT_WORDS[ApproverSlot(str(approver.get("slot")))])
        except ValueError:
            continue
    who = _joined(seats) or "an approver"
    return f"If you say yes, I will file it and it needs approval from {who}."


def question_reply(resolved: ResolvedEntities | None, results: Sequence[ToolResult]) -> str:
    """ "Do you mean <role> access in <system>?" and what happens next."""
    role = resolved.role_name if resolved else None
    system = resolved.system_name if resolved else None
    if role and system:
        question = f"Do you mean {role} access in {system}?"
    elif system:
        question = f"Do you mean access in {system}?"
    elif role:
        question = f"Do you mean {role} access?"
    else:
        question = "Do you mean to file this access request?"
    return f"{question} {next_step(results)}"
