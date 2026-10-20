"""The next steps a reply may offer, worked out by code.

Pure: no I/O. Another role is offered only when the resolved role's system has
one, and a ticket only when the person's principals may call `create_ticket`,
in the matrix's own words. A ticket is offered only where the chat cannot
help itself: an `open_ticket` or `other` turn, or a turn where a tool failed
(`test_next_steps.py`). The writer gets this list and may offer nothing
else, and a sentence of its own that offers a ticket anyway is cut
(`without_ticket_offer`).
"""

import re
from collections.abc import Sequence

from helpdesk.agent_api.agent.state import Intent, ResolvedEntities
from helpdesk.shared.auth.capabilities import SENTENCES
from helpdesk.shared.auth.rbac import Tool, tools_for

TICKET_INTENTS = frozenset({Intent.OPEN_TICKET, Intent.OTHER})
"""The turns where a ticket is the help: the chat has no tool for the rest."""

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_TICKET_OFFER = re.compile(r"\b(open|raise|file|create|log|submit)\b.*\bticket\b", re.IGNORECASE)
"""An offer names the act before the ticket; "Ticket TCK-0002 is open" does not."""


def offers_ticket(
    principals: Sequence[str], intent: Intent | None = None, *, tool_failed: bool = False
) -> bool:
    """Whether this turn's next steps may offer a ticket. With no intent known
    the ticket stays on offer, as before the intent was read."""
    if Tool.CREATE_TICKET not in tools_for(principals):
        return False
    return intent is None or intent in TICKET_INTENTS or tool_failed


def next_step_lines(
    principals: Sequence[str],
    resolved: ResolvedEntities | None,
    intent: Intent | None = None,
    *,
    tool_failed: bool = False,
) -> list[str]:
    """The other-role line first, when there is one, then the ticket line."""
    lines: list[str] = []
    if resolved is not None and resolved.other_role_names:
        names = ", ".join(resolved.other_role_names)
        lines.append(f"Ask for another role on {resolved.system_name}: {names}.")
    if offers_ticket(principals, intent, tool_failed=tool_failed):
        lines.append(SENTENCES[Tool.CREATE_TICKET])
    return lines


def without_ticket_offer(text: str) -> str:
    """The text with every sentence that offers a ticket cut. A text that would
    be left empty comes back whole: it is all the person has to read."""
    sentences = _SENTENCE_END.split(text.strip())
    kept = [sentence for sentence in sentences if not _TICKET_OFFER.search(sentence)]
    if not kept:
        return text
    return " ".join(kept)
