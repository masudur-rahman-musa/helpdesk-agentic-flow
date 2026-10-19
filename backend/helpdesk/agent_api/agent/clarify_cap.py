"""At most one clarifying question in a row: the stored time and the fixed lines.

Pure. The time of the last clarifying question is kept on the chat row as an
ISO string and checked here on every read, like the open question of
`confirmation.py` (a design note, pinned by `test_clarify_cap.py`).
"""

from datetime import datetime, timedelta

CLARIFY_CAP_LIFE = timedelta(hours=24)
"""A question this old or older counts as none: the person has moved on."""

CORRECT_ME_LINE = "If I read that wrong, tell me what you meant."
"""Added by code to every best-reading reply, so a wrong guess is easy to fix."""

BEST_READING_FALLBACK = (
    "I could not pin down what you need yet. Tell me the system or the task "
    "in a few words and I will take it from there."
)
"""What a best-reading reply ends on when the model asked a question twice."""


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
