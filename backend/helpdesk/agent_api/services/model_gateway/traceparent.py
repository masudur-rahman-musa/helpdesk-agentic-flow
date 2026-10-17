"""The W3C `traceparent` header, so this service and the Ops Console share a trace."""

import os
import re

from helpdesk.agent_api.services.tracing import current_span_id, current_trace_id
from helpdesk.shared.core.errors import ErrorCode, ToolError

TRACE_ID_BYTES = 16
"""Trace id length fixed by W3C: 32 hex characters."""

SPAN_ID_BYTES = 8
"""Span id length fixed by W3C: 16 hex characters."""

TRACE_FLAGS = "01"
"""The sampled flag: joining the two services' traces is the point."""

_ZERO_TRACE_ID = "0" * (TRACE_ID_BYTES * 2)
_ZERO_SPAN_ID = "0" * (SPAN_ID_BYTES * 2)

TRACEPARENT_PATTERN = re.compile(
    rf"00-(?!{_ZERO_TRACE_ID})([0-9a-f]{{32}})-(?!{_ZERO_SPAN_ID})([0-9a-f]{{16}})-[0-9a-f]{{2}}"
)
"""Version `00` only, lowercase hex, with W3C's all-zero ids refused."""


class TraceparentInvalidError(ToolError):
    """The caller's `traceparent` cannot be sent. The fix is in the caller."""

    code = ErrorCode.VALIDATION


def new_traceparent() -> str:
    """A fresh random header, so two turns never merge into one trace."""
    return f"00-{os.urandom(TRACE_ID_BYTES).hex()}-{os.urandom(SPAN_ID_BYTES).hex()}-{TRACE_FLAGS}"


def check_traceparent(value: str) -> str:
    """The header if valid, else a typed `VALIDATION`. The gateway would refuse it anyway."""
    if TRACEPARENT_PATTERN.fullmatch(value) is None:
        raise TraceparentInvalidError(
            "the supplied traceparent is not a valid W3C version-00 header"
        )
    return value


def traceparent_for(value: str | None) -> str:
    """Use the caller's header, else the active span's ids, else a new one.

    A bad caller value is refused, not replaced, so the bug stays visible.
    """
    if value is not None:
        return check_traceparent(value)
    trace_id = current_trace_id()
    span_id = current_span_id()
    if trace_id is None or span_id is None:
        return new_traceparent()
    return f"00-{trace_id}-{span_id}-{TRACE_FLAGS}"
