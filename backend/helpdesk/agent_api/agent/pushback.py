"""Two asks the chat says no to in fixed words: skipping approval, and access
to everything.

Pure, and read by fixed word rules, never by the model: a rule cannot be
talked out of its answer. An approve word with a skip word up to
three words before it ("skip the approval", "no approval needed") puts
`SKIP_LINE` first in the reply. "Everything", "all systems" or "every system"
on a request ends the turn with `EVERYTHING_REPLY` (`test_pushback.py`).
"""

import re
from itertools import pairwise

SKIP_LINE = (
    "Approval can't be skipped: the policy decides who must approve, and I can't change that."
)

EVERYTHING_REPLY = (
    "No. I can't ask for access to everything. Each request is for one role "
    "on one system. Tell me the one you need and why."
)

SKIP_WORDS = frozenset({"skip", "skipping", "bypass", "bypassing", "without", "no"})

SKIP_REACH = 3
"""How many words before the approve word a skip word may sit, inclusive."""

_SYSTEM_WORDS = frozenset({"system", "systems"})
_WORD = re.compile(r"[a-z']+")


def _words(message: str) -> list[str]:
    return _WORD.findall(message.lower())


def skips_approval(message: str) -> bool:
    """Whether the message asks to skip or go around approval."""
    words = _words(message)
    for index, word in enumerate(words):
        if word.startswith("approv") and SKIP_WORDS.intersection(
            words[max(0, index - SKIP_REACH) : index]
        ):
            return True
    return False


def wants_everything(message: str) -> bool:
    """Whether the message asks for everything instead of one system."""
    words = _words(message)
    if "everything" in words:
        return True
    return any(
        first in {"all", "every"} and second in _SYSTEM_WORDS for first, second in pairwise(words)
    )
