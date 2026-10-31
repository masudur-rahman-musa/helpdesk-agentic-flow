"""A tool result made shorter for the model to read, and only for the model.

Pure: no I/O. The reply check, the Sources list and the saved results keep
the full answer; only the copy that goes back to the model is cut, so a long
listing costs fewer tokens and a smaller wait.
"""

from typing import Any

__all__ = [
    "CUT_MARK",
    "DROPPED",
    "MAX_ROWS",
    "MAX_TEXT",
    "NEVER_TRIMMED",
    "trim_for_model",
]

MAX_ROWS = 20
"""Rows of a list the model reads; the rest are counted in `more`."""

MAX_TEXT = 1_500
"""Characters of one text the model reads."""

CUT_MARK = " ...(cut)"
"""Put after a cut text, so the model can tell it stopped."""

DROPPED = frozenset({"score", "chunk_id"})
"""Keys that mean nothing to the model."""

NEVER_TRIMMED = frozenset(
    {
        "decision",
        "status",
        "grant_id",
        "request_id",
        "request_number",
        "role_id",
        "role_name",
        "target_user_id",
        "user_id",
        "manager_user_id",
    }
)
"""The fields the reply check and the change check read. Cut, they would stop
matching the full result."""


def trim_for_model(payload: Any) -> Any:
    """A copy of `payload` where, at any depth, a list in a dict keeps its first
    `MAX_ROWS` rows and the dict gains `"more": N`, a text over `MAX_TEXT` is
    cut there with `CUT_MARK`, and `DROPPED` keys are gone. `NEVER_TRIMMED`
    fields stay whole. The input is left as it is."""
    if isinstance(payload, dict):
        trimmed: dict[str, Any] = {}
        more = 0
        for key, value in payload.items():
            if key in DROPPED:
                continue
            if key in NEVER_TRIMMED:
                trimmed[key] = value
                continue
            if isinstance(value, list | tuple) and len(value) > MAX_ROWS:
                more += len(value) - MAX_ROWS
                value = value[:MAX_ROWS]
            trimmed[key] = trim_for_model(value)
        if more:
            trimmed["more"] = more
        return trimmed
    if isinstance(payload, list | tuple):
        return [trim_for_model(item) for item in payload]
    if isinstance(payload, str) and len(payload) > MAX_TEXT:
        return payload[:MAX_TEXT] + CUT_MARK
    return payload
