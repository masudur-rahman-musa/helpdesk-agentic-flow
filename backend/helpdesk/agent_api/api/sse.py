"""The Server-Sent Events wire format: event names and the frame encoder.

A frame's `data` is always one line. SSE splits on newlines, so a reply holding
a blank line and `event: reply` could otherwise forge a second event. Only
`json.dumps`, which escapes every newline, writes a payload.
"""

import json
from collections.abc import Mapping
from typing import Any

from helpdesk.agent_api.agent.events import ProgressEvent, ReplyEvent, StepEvent, TurnEvent

__all__ = [
    "DONE",
    "ERROR",
    "PROGRESS",
    "REPLY",
    "REPLY_CHUNK_CHARS",
    "STEP",
    "TOKEN",
    "chunk_reply",
    "encode_event",
    "encode_turn_event",
]

PROGRESS = "progress"
"""Plain words for the tool step about to run."""

STEP = "step"
"""One graph node the turn visited."""

TOKEN = "token"
"""One slice of the already checked reply."""

REPLY = "reply"
"""The terminal event: the same body `/chat` answers the same turn with."""

ERROR = "error"
"""A failure after the first byte, as a typed code."""

DONE = "done"
"""The turn is over, said out loud rather than by a quiet socket."""

REPLY_CHUNK_CHARS = 64
"""How many characters one `token` event carries.

Slices of the finished reply, not model tokens: only text that passed the
output check is streamed, since a sent token cannot be taken back.
"""


def chunk_reply(reply: str) -> tuple[str, ...]:
    """The reply cut into fixed-size pieces that join back to the exact reply."""
    return tuple(
        reply[start : start + REPLY_CHUNK_CHARS]
        for start in range(0, len(reply), REPLY_CHUNK_CHARS)
    )


def encode_event(event: str, data: Mapping[str, Any]) -> str:
    """One SSE frame: an event line, a single data line, and a blank line."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def encode_turn_event(event: TurnEvent) -> tuple[str, ...]:
    """One turn event as its SSE frames, in order.

    A reply becomes token frames, the reply frame, then done. An error is one
    error frame, then done.
    """
    if isinstance(event, ProgressEvent):
        return (encode_event(PROGRESS, {"round": event.round, "text": event.text}),)
    if isinstance(event, StepEvent):
        data = {"node": event.node, "round": event.round, "tools": event.tools}
        return (encode_event(STEP, data),)
    if isinstance(event, ReplyEvent):
        frames: list[str] = [
            encode_event(TOKEN, {"text": chunk}) for chunk in chunk_reply(event.reply or "")
        ]
        frames.append(encode_event(REPLY, event.reply_data))
        frames.append(encode_event(DONE, {}))
        return tuple(frames)
    # isinstance(event, ErrorEvent)
    return (encode_event(ERROR, {"code": event.code.value}), encode_event(DONE, {}))
