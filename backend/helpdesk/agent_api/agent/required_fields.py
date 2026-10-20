"""One rule for every required field: ask, say it is required, then stop.

Pure. Each field has one row: the words of the first ask, the "is required"
sentence of the second ask and the short name the stop line uses. No field
has its own special case. The asks per field ride on the stored question
kind on the chat row, so they last only while the questions come one after
another (`test_required_fields.py`).
"""

from collections.abc import Mapping, Set
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

MAX_ASKS = 2
"""Asks per field, inclusive. A field asked this often and still missing stops."""


@dataclass(frozen=True)
class RequiredField:
    """One field: how it is asked the first time, why it is needed, and its
    short name in the stop line. The length has no second ask and no stop:
    it is asked but never required."""

    name: str
    first_ask: str
    required_line: str | None
    stop_name: str | None


REQUIRED_FIELDS: tuple[RequiredField, ...] = (
    RequiredField(
        "system",
        "which system you need",
        "The system is required. I need it to find the right access. "
        "Please give the system's name, even roughly.",
        "a system",
    ),
    RequiredField(
        "target_user",
        "who the person is",
        "The person is required. I need it to know whose account changes. "
        "Please give their name or email.",
        "the person",
    ),
    RequiredField(
        "department",
        "which department",
        "The department is required. I need it to pick the right access. "
        "Please give the department's name.",
        "a department",
    ),
    RequiredField("expiry_days", "how long you need it for", None, None),
    RequiredField(
        "justification",
        "the business reason",
        "The business reason is required. Approvers need it to decide. "
        "Please give a short reason, even one line.",
        "a reason",
    ),
)
"""Every field, in the order the questions name them."""

_ASKABLE = frozenset(field.name for field in REQUIRED_FIELDS if field.required_line)


def stop_line(stop_name: str) -> str:
    """The fixed words a turn ends on when a field was asked twice."""
    return f"I can't file this without {stop_name}. You can start again when you have one."


def first_question(missing: Set[str], *, length_missing: bool) -> str:
    """The normal question, naming every missing detail at once."""
    wanted = set(missing) | ({"expiry_days"} if length_missing else set())
    words = [field.first_ask for field in REQUIRED_FIELDS if field.name in wanted]
    joined = words[0] if len(words) == 1 else f"{', '.join(words[:-1])} and {words[-1]}"
    return f"Before I check this, tell me {joined}."


class AskStep(StrEnum):
    """Where a field stands on the ladder."""

    FIRST = "first"
    SECOND = "second"
    STOP = "stop"


class Ask(BaseModel):
    """What to say, and the counts to store after saying it. A stop stores
    none, so the next message starts clean."""

    model_config = ConfigDict(frozen=True)

    step: AskStep
    reply: str
    asks: dict[str, int]


def next_ask(
    missing: Set[str],
    *,
    asks: Mapping[str, int] | None,
    details_asked: bool,
    length_missing: bool = False,
) -> Ask | None:
    """The next ask for the missing required fields, or `None` when nothing
    is asked. `asks` of `None` is an older stored row: every field counts as
    asked once."""
    fields = [field for field in REQUIRED_FIELDS if field.name in missing and field.required_line]
    counts = {field.name: 1 if asks is None else asks.get(field.name, 0) for field in fields}
    stopped = next((field for field in fields if counts[field.name] >= MAX_ASKS), None)
    if stopped is not None and stopped.stop_name is not None:
        return Ask(step=AskStep.STOP, reply=stop_line(stopped.stop_name), asks={})
    if any(counts.values()):
        reply = " ".join(field.required_line for field in fields if field.required_line)
        return Ask(
            step=AskStep.SECOND,
            reply=reply,
            asks={name: count + 1 for name, count in counts.items()},
        )
    if fields or (length_missing and not details_asked):
        return Ask(
            step=AskStep.FIRST,
            reply=first_question(set(counts), length_missing=length_missing),
            asks=dict.fromkeys(counts, 1),
        )
    return None


def parse_asks(raw: object) -> dict[str, int]:
    """The stored asks. Every name must be a required field and every count
    1 or 2, or the whole value reads as no asks."""
    if not isinstance(raw, dict):
        return {}
    parsed: dict[str, int] = {}
    for name, count in raw.items():
        if name not in _ASKABLE or type(count) is not int or not 1 <= count <= MAX_ASKS:
            return {}
        parsed[name] = count
    return parsed


NON_ANSWERS = frozenset(
    {
        "no reason",
        "i don't know",
        "dont know",
        "idk",
        "dunno",
        "not sure",
        "no idea",
        "none",
        "nothing",
        "n/a",
        "no",
    }
)
"""Answers that give nothing. Only the whole answer counts: "no access to
the docs yet" is a reason."""


CURLY_APOSTROPHE = chr(0x2019)
"""Phones often type this in place of a straight apostrophe; both read the same."""


def is_non_answer(text: str) -> bool:
    """Whether the text, ignoring case, spaces and end dots, gives nothing."""
    cleaned = text.replace(CURLY_APOSTROPHE, "'").strip().rstrip(".").strip().lower()
    return cleaned in NON_ANSWERS
