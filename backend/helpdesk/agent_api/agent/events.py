"""Typed events a turn yields: before and after each loop step that runs a tool,
then the ending.

Nothing here knows about SSE: ``api/sse.py`` imports these and builds the
frames.
"""

from dataclasses import dataclass
from typing import Any

from helpdesk.shared.core.errors import ErrorCode

__all__ = ["ErrorEvent", "ProgressEvent", "ReplyEvent", "StepEvent", "TurnEvent"]


@dataclass(frozen=True)
class ProgressEvent:
    """Plain words for a tool step about to run, shown while the person waits."""

    round: int
    text: str


@dataclass(frozen=True)
class StepEvent:
    """One graph node the turn visited."""

    node: str
    round: int
    tools: list[dict[str, Any]]


@dataclass(frozen=True)
class ReplyEvent:
    """The finished turn's reply, ready to stream as tokens.

    ``reply_data`` has the same keys ``/chat`` returns, so streaming and polling
    clients read one shape.
    """

    reply: str | None
    reply_data: dict[str, Any]


@dataclass(frozen=True)
class ErrorEvent:
    """A failure after the first byte was sent."""

    code: ErrorCode


type TurnEvent = ProgressEvent | StepEvent | ReplyEvent | ErrorEvent
