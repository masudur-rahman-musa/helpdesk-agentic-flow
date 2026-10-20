"""At most one clarifying question in a row: the stored time and the fixed lines.

Pure. The time of the last clarifying question is kept on the chat row as an
ISO string and checked here on every read, like the open question of
`confirmation.py`.
"""

from collections.abc import Mapping, Sequence, Set
from datetime import datetime, timedelta
from enum import StrEnum

from helpdesk.agent_api.agent.required_fields import first_question, parse_asks

CLARIFY_CAP_LIFE = timedelta(hours=24)
"""A question this old or older counts as none: the person has moved on."""

CORRECT_ME_LINE = "If I read that wrong, tell me what you meant."
"""Added by code to every best-reading reply, so a wrong guess is easy to fix."""

BEST_READING_FALLBACK = (
    "I could not pin down what you need yet. Tell me the system or the task "
    "in a few words and I will take it from there."
)
"""What a best-reading reply ends on when the model asked a question twice."""


class QuestionKind(StrEnum):
    """What the last question asked, so the cap stops the same kind twice in a
    row, not any two questions: a suggestion, then a pick, then the details
    question is allowed."""

    SUGGEST = "suggest"
    NAME = "name"
    DETAILS = "details"
    OTHER = "other"
    DECLINED = "declined"
    """A bare no to a summary: a yes right after it has nothing to say yes to."""


def parse_asked_kind(raw: object, *, now: datetime) -> QuestionKind | None:
    """The kind of the last question, stored as `{"kind": ..., "at": ...}`.
    A missing, badly shaped, unknown or too old value counts as none."""
    if not isinstance(raw, dict):
        return None
    if not parse_asked_at(raw.get("at"), now=now):
        return None
    kind = raw.get("kind")
    if not isinstance(kind, str):
        return None
    try:
        return QuestionKind(kind)
    except ValueError:
        return None


def stored_kind(
    kind: QuestionKind,
    *,
    now: datetime,
    asks: Mapping[str, int] | None = None,
    options: Sequence[str] | None = None,
) -> dict[str, object]:
    """The kind and its time, in the shape `parse_asked_kind` reads, with the
    asks per field and the offered options when given."""
    stored: dict[str, object] = {"kind": kind.value, "at": now.isoformat()}
    if asks is not None:
        stored["asks"] = dict(asks)
    if options:
        stored["options"] = list(options)
    return stored


def parse_asked_options(raw: object, *, now: datetime) -> tuple[str, ...]:
    """The options the last question offered, in the order shown. A missing,
    bad or too old row reads as none, so nothing can be picked from it."""
    if parse_asked_kind(raw, now=now) is None or not isinstance(raw, dict):
        return ()
    options = raw.get("options")
    if not isinstance(options, list) or not all(isinstance(o, str) for o in options):
        return ()
    return tuple(options)


def parse_asked_asks(raw: object, *, now: datetime) -> dict[str, int] | None:
    """The asks per field stored with the kind. A missing, bad or too old row
    reads as no asks. An older `details` row with no asks gives `None`: each
    missing field then counts as asked once."""
    kind = parse_asked_kind(raw, now=now)
    if kind is None or not isinstance(raw, dict):
        return {}
    if "asks" not in raw:
        return None if kind is QuestionKind.DETAILS else {}
    return parse_asks(raw["asks"])


def parse_asked_at(raw: object, *, now: datetime) -> bool:
    """Whether a clarifying question was asked less than `CLARIFY_CAP_LIFE`
    ago. A missing or badly shaped value counts as none."""
    if not isinstance(raw, str):
        return False
    try:
        asked_at = datetime.fromisoformat(raw)
    except ValueError:
        return False
    if asked_at.tzinfo is None:
        return False
    return now - asked_at < CLARIFY_CAP_LIFE


def details_question(missing: Set[str], *, length_missing: bool) -> str:
    """The one question a confident request asks, worded by code so it names
    every missing detail at once. `missing` is
    `gates.missing_required_entities`; the length is asked but never required."""
    return first_question(missing, length_missing=length_missing)
