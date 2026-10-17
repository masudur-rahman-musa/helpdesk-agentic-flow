"""Typed events the turn graph yields, one per visited node or terminal.

The API layer reads these and turns each one into its wire frames (an earlier change,
a design note). Nothing here knows what SSE is: each event carries the values a
frame needs, and the encoder in ``api/sse.py`` builds the strings.

The dependency points down: ``api/sse.py`` imports these types, not the
other way around.
"""

from dataclasses import dataclass
from typing import Any

from helpdesk.shared.core.errors import ErrorCode

__all__ = ["ErrorEvent", "ReplyEvent", "StepEvent", "TurnEvent"]


@dataclass(frozen=True)
class StepEvent:
    """One graph node the turn visited."""

    node: str
    round: int
    tools: list[dict[str, Any]]


@dataclass(frozen=True)
class ReplyEvent:
    """The turn is finished; its reply ready to stream as tokens then confirm.

    ``reply`` is the text to cut into ``token`` events. ``reply_data`` is the
    pre-rendered dict that fills the ``reply`` SSE frame — the same five keys
    ``/chat`` returns, so a streaming client and a polling one read one shape
    (19a decision E1, an earlier change).
    """

    reply: str | None
    reply_data: dict[str, Any]


@dataclass(frozen=True)
class ErrorEvent:
    """A failure after the first byte."""

    code: ErrorCode


type TurnEvent = StepEvent | ReplyEvent | ErrorEvent
