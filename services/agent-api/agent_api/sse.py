"""The Server-Sent Events wire format, and nothing else.

Five event names, one frame encoder and one chunk size. Pure — every function
here takes values and returns strings — so the whole of §1's injection
criterion is tested with no app, no graph and no database, the same shape
`postcheck.py` already has. Nothing in this module knows what a turn is.

**A frame's `data` is ONE line, whatever the payload contains.** SSE ends a
frame at a blank line and starts a field at a newline, so a reply carrying
`\\n\\nevent: reply\\ndata: {}` would otherwise close its own frame and forge a
second `reply` event — a body the turn never produced, acted on by a client
that cannot tell the difference. `json.dumps` escapes every newline, and
nothing here ever writes the reply text into a frame raw. Pinned by
`test_turn_stream.py::test_a_reply_containing_a_frame_boundary_forges_no_event`
and `::test_every_frame_carries_its_data_on_one_line`, and end to end through
the route by
`test_chat_stream.py::test_one_message_streams_its_steps_its_tokens_and_its_reply`.
"""

import json
from collections.abc import Mapping
from typing import Any

__all__ = [
    "DONE",
    "ERROR",
    "REPLY",
    "REPLY_CHUNK_CHARS",
    "STEP",
    "TOKEN",
    "chunk_reply",
    "encode_event",
]

STEP = "step"
"""One graph node the turn visited (decision C)."""

TOKEN = "token"
"""One slice of the CHECKED reply (decision B, a design note)."""

REPLY = "reply"
"""The terminal event: the same body `/chat` answers the same turn with."""

ERROR = "error"
"""A failure after the first byte, as a typed code (decision E1)."""

DONE = "done"
"""The turn is over — the only frame that says so, rather than a quiet socket."""

REPLY_CHUNK_CHARS = 64
"""How many characters one `token` event carries.

A slice of a finished string, not a model's own token: decision B streams the
reply the post-check has already passed, because a token on the wire cannot be
regenerated. The size is small enough that a long reply arrives as
text appearing rather than as one block, and large enough that a 4000-character
reply is 63 frames rather than hundreds.

a project rule, pinned from both sides by
`test_turn_stream.py::test_a_reply_of_exactly_sixty_four_characters_is_one_token_event`
and `::test_a_reply_of_sixty_five_characters_is_two`.
"""


def chunk_reply(reply: str) -> tuple[str, ...]:
    """The reply cut into `REPLY_CHUNK_CHARS`-character pieces, in order.

    Cut on the constant and on nothing else — not on a word or a sentence
    boundary — so the concatenation of the chunks is the reply again,
    character for character, and a client that joins them renders exactly what
    was stored. An exact multiple of the cap produces no empty trailing chunk.
    """
    return tuple(
        reply[start : start + REPLY_CHUNK_CHARS]
        for start in range(0, len(reply), REPLY_CHUNK_CHARS)
    )


def encode_event(event: str, data: Mapping[str, Any]) -> str:
    """One SSE frame: an event line, a single data line, and a blank line.

    `json.dumps` is what makes the data line one line — it escapes every
    newline, carriage return and line separator a payload can hold — and it is
    the only thing that writes the payload. See the module docstring for what
    an encoder that interpolated the text instead would let a reply forge.
    """
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"
