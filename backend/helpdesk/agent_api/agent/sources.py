"""The passages the Sources box shows, worked out by code.

Pure: no I/O. The reply writer marks the passages its text relies on by their
1-based `passage` number. A mark outside the list is ignored, a repeated mark
counts once, and the kept passages stay in search order. A fixed
sentence sent in place of a refused reply shows none: code wrote it from tool
results and it uses no policy text. When the marks are not known (a fixed
ending that never composed), every passage is shown.
"""

from collections.abc import Sequence

from helpdesk.agent_api.agent.state import Citation, TurnState


def pick_used(citations: Sequence[Citation], marks: Sequence[int]) -> tuple[Citation, ...]:
    """The passages whose number is among `marks`, in search order."""
    wanted = set(marks)
    return tuple(citation for number, citation in enumerate(citations, start=1) if number in wanted)


def shown_citations(state: TurnState) -> tuple[Citation, ...]:
    """What both outputs send: the reply's own passages when known, else all."""
    return state.citations if state.reply_citations is None else state.reply_citations
