"""The W3C `traceparent` header: minted here, or accepted from a caller.

the design and §25 want ONE trace spanning this service and the console, so an
agent turn that already holds a trace hands it down rather than starting a
second one; `traceparent_for` is that choice in one function, and an earlier change
decision C is why it is not "always mint".

`traceparent_for` is the seam an earlier change takes: with a Langfuse span active it
derives the header from that span instead of minting, so the console records
the model call under the same trace as the turn that made it (the design).
Everything downstream asserts the header's SHAPE against
`TRACEPARENT_PATTERN`, which stays true whichever source produces the value.

Pure: `os.urandom`, `re` and the tracing facade's two id readers — no I/O and
no models. `helpdesk.obs.tracing` never raises and never blocks (an earlier change
decision C), so reading it costs this module none of its own guarantees. The
pattern is matched with
`re.fullmatch`, so a value ending in a newline, a CRLF or a
space is a refusal rather than a header-injection shape going onto the wire;
the rejection table is
`test_gateway_traceparent.py::test_a_malformed_traceparent_is_refused_with_a_typed_code`
and its pattern-level twin
`::test_the_pattern_itself_refuses_every_malformed_row`.
"""

import os
import re

from helpdesk.errors import ErrorCode, ToolError
from helpdesk.obs.tracing import current_span_id, current_trace_id

TRACE_ID_BYTES = 16
"""Bytes of trace id, rendered as 32 hex characters, as W3C fixes them.

Pinned through the shape by
`test_gateway_traceparent.py::test_a_minted_traceparent_matches_the_version_00_pattern`;
a shorter id is a header the gateway answers 422 to on every call."""

SPAN_ID_BYTES = 8
"""Bytes of span id, rendered as 16 hex characters, as W3C fixes them."""

TRACE_FLAGS = "01"
"""The sampled flag. This process asks for the call to be recorded: the join
between the two services is the whole point of propagating the header."""

TRACEPARENT_PATTERN = re.compile(r"00-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}")
"""The version-`00` shape, lowercase hex only, matched with `re.fullmatch`.

Version `00` alone: a later version may add fields this module cannot read,
and forwarding a header it does not understand would claim a parent span it
cannot name. Pinned by
`test_gateway_traceparent.py::test_the_pattern_itself_refuses_every_malformed_row`.
"""

_ZERO_TRACE_ID = "0" * (TRACE_ID_BYTES * 2)
_ZERO_SPAN_ID = "0" * (SPAN_ID_BYTES * 2)


class TraceparentInvalidError(ToolError):
    """The caller's `traceparent` is not a header this client can send.

    `VALIDATION` rather than `INTERNAL`: a retry with the same value cannot
    help, and the fix is in the caller that built the header.
    """

    code = ErrorCode.VALIDATION


def new_traceparent() -> str:
    """Mint a fresh version-`00` header for a call with no parent span.

    Random per call, never per process:
    `test_gateway_traceparent.py::test_two_mints_differ_in_their_trace_ids`
    fails on a module-level constant or a seeded generator, which would merge
    two concurrent turns into one trace in the console's log.
    """
    return f"00-{os.urandom(TRACE_ID_BYTES).hex()}-{os.urandom(SPAN_ID_BYTES).hex()}-{TRACE_FLAGS}"


def check_traceparent(value: str) -> str:
    """The accepted header, or a typed `VALIDATION` refusal.

    The all-zero trace and span ids are refused as well as the malformed
    shapes: W3C forbids both, and the gateway answers 422 to them, so
    accepting one here would spend a round trip to learn what a `fullmatch`
    and two comparisons already know. Pinned by
    `test_gateway_traceparent.py::test_a_malformed_traceparent_is_refused_with_a_typed_code`.
    """
    match = TRACEPARENT_PATTERN.fullmatch(value)
    if match is None or match.group(1) == _ZERO_TRACE_ID or match.group(2) == _ZERO_SPAN_ID:
        raise TraceparentInvalidError(
            "the supplied traceparent is not a valid W3C version-00 header"
        )
    return value


def traceparent_for(value: str | None) -> str:
    """Propagate the caller's header, derive it from the active span, or mint.

    A malformed caller value is refused rather than replaced: minting over it
    would hide the caller's bug behind a trace that joins nothing, which is
    the one failure a trace store cannot report. Pinned by
    `test_gateway_traceparent.py::test_a_valid_caller_value_is_returned_verbatim`,
    `::test_no_caller_value_mints_a_fresh_header` and
    `::test_a_malformed_caller_value_is_refused_rather_than_replaced`.

    With no caller value and a Langfuse span active, the header names THAT
    span: the trace id joins the console's record to
    this turn's, and the span id makes the gateway's span a child of the node
    that called it rather than a root hanging off nothing. Pinned by
    `::test_an_active_span_makes_the_header_carry_that_trace_and_that_span`.

    Both ids or neither. A trace id with a random parent would file the call
    under the right trace with no caller, which is worse than a clean mint —
    and with tracing unconfigured, or after the turn's trace has closed, both
    are `None` and this mints exactly as it did before this plan
    (`::test_with_the_trace_closed_the_header_is_minted_fresh_again`).

    A caller's own value still wins over an active span: it is a parent from
    another process, and re-parenting it onto this
    service loses the join the caller asked for
    (`::test_a_caller_value_still_wins_over_an_active_span`).
    """
    if value is not None:
        return check_traceparent(value)
    trace_id = current_trace_id()
    span_id = current_span_id()
    if trace_id is None or span_id is None:
        return new_traceparent()
    return f"00-{trace_id}-{span_id}-{TRACE_FLAGS}"
