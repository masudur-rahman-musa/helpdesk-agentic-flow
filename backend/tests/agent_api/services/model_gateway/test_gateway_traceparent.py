"""An earlier change §6: the W3C `traceparent` header minted and checked in
`helpdesk/gateway/traceparent.py`.

Expected implementation surface (the implementer must provide exactly this):

``helpdesk.gateway.traceparent``
    ``TRACEPARENT_PATTERN: re.Pattern[str]`` — the version-``00`` W3C shape,
        matched with ``re.fullmatch``.
    ``def new_traceparent() -> str`` — a fresh header from 16 random bytes of
        trace id and 8 of span id.
    ``def check_traceparent(value: str) -> str`` — the accepted value, or a
        ``ToolError`` whose code is ``ErrorCode.VALIDATION``.
    ``def traceparent_for(value: str | None) -> str`` — mint when the caller
        passes none, propagate verbatim when the caller passes a valid one.
        Plan §2 names this as the ONE seam row 20 replaces with the active
        span's context, which is why these cases pin the header's SHAPE
        rather than the generator.

Pure: no I/O, no socket, no backing service. `os.urandom` is the only
non-determinism, which is why the mint is asserted against the pattern and
against a second mint, never against a literal.

An earlier change takes the seam this file's docstring reserved, and ADDS the three
cases at the bottom; nothing above them moves (the plan's §8 says so, and
``test_no_caller_value_mints_a_fresh_header`` is named there as the green
that proves the change is additive). Expected implementation surface:

``helpdesk.gateway.traceparent.traceparent_for(None)`` returns
``f"00-{current_trace_id()}-{current_span_id()}-{TRACE_FLAGS}"`` when BOTH are
available from ``helpdesk.obs.tracing``, and mints exactly as today when
either is ``None``. A caller-supplied valid value still wins verbatim, and the
signature does not change.

The active span is produced by the same recording fake ``test_obs_tracing.py``
drives, imported rather than copied so one fake defines what "active" means;
still no network and no backing service.
"""

from collections.abc import Iterator

import pytest

from helpdesk.agent_api.services.model_gateway.traceparent import (
    TRACE_FLAGS,
    TRACEPARENT_PATTERN,
    check_traceparent,
    new_traceparent,
    traceparent_for,
)
from helpdesk.agent_api.services.tracing import (
    configure_tracing,
    current_span_id,
    current_trace_id,
    span,
    turn_trace,
)
from helpdesk.shared.core.errors import ErrorCode, ToolError
from tests.agent_api.services.test_obs_tracing import FakeTraceClient

VALID = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
"""The W3C specification's own example header, unsampled flag included."""

ZERO_TRACE_ID = "0" * 32
ZERO_SPAN_ID = "0" * 16


# --- the minted header --------------------------------------------------------


def test_a_minted_traceparent_matches_the_version_00_pattern() -> None:
    """Catches a generator that mints a header of the wrong shape — a 16-byte
    trace id written as 16 hex characters instead of 32, uppercase hex, or a
    missing flags field. The gateway parses this header itself, so a
    malformed mint would be answered 422 on EVERY call rather than failing
    locally, and the join between the two services would never form."""
    minted = new_traceparent()

    assert TRACEPARENT_PATTERN.fullmatch(minted) is not None
    assert minted.startswith("00-")
    assert len(minted.split("-")) == 4


def test_two_mints_differ_in_their_trace_ids() -> None:
    """Catches a generator that mints one header per process (a module-level
    constant, or a seeded RNG): every request would then carry the same trace
    id, and two concurrent turns would be one indistinguishable trace in the
    console's log."""
    first = new_traceparent()
    second = new_traceparent()

    assert first.split("-")[1] != second.split("-")[1]


def test_a_minted_traceparent_is_accepted_by_the_checker() -> None:
    """Catches a mint and a check that disagree — a pattern narrower than the
    generator's output, or a generator that can emit the all-zero trace id the
    checker forbids. Nothing else in the suite pairs the two functions, so a
    drift between them would otherwise surface only as a refused request."""
    minted = new_traceparent()

    assert check_traceparent(minted) == minted


# --- the rejection table ------------------------------------------------------


_REJECTED: tuple[tuple[str, str], ...] = (
    ("version-01", "01-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"),
    ("version-ff", "ff-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"),
    ("short-trace-id", "00-4bf92f3577b34da6a3ce929d0e0e473-00f067aa0ba902b7-01"),
    ("long-trace-id", "00-4bf92f3577b34da6a3ce929d0e0e47366-00f067aa0ba902b7-01"),
    ("short-span-id", "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b-01"),
    ("long-span-id", "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b77-01"),
    ("zero-trace-id", f"00-{ZERO_TRACE_ID}-00f067aa0ba902b7-01"),
    ("zero-span-id", f"00-4bf92f3577b34da6a3ce929d0e0e4736-{ZERO_SPAN_ID}-01"),
    ("trailing-newline", VALID + "\n"),
    ("trailing-crlf", VALID + "\r\n"),
    ("trailing-space", VALID + " "),
    ("empty", ""),
)


@pytest.mark.parametrize(
    "value", [value for _, value in _REJECTED], ids=[name for name, _ in _REJECTED]
)
def test_a_malformed_traceparent_is_refused_with_a_typed_code(value: str) -> None:
    """Catches a checker built on `re.match` or `re.search` rather than
    `re.fullmatch`: the three trailing rows all carry a
    valid header as their PREFIX, so only a full match refuses them, and a
    header value ending in `\\n` is a header-injection shape going onto the
    wire. The two all-zero rows catch a pattern that is hex-correct but
    ignores the W3C invalid-id rule, which the gateway answers 422 to. The
    assertion is on the code rather than on a message, because the caller
    that reads this is the MCP boundary."""
    with pytest.raises(ToolError) as excinfo:
        check_traceparent(value)

    assert excinfo.value.code is ErrorCode.VALIDATION


@pytest.mark.parametrize(
    "value", [value for _, value in _REJECTED], ids=[name for name, _ in _REJECTED]
)
def test_the_pattern_itself_refuses_every_malformed_row(value: str) -> None:
    """Catches a `TRACEPARENT_PATTERN` too loose to be the checker's basis
    while `check_traceparent` happens to refuse the row some other way. The
    pattern is exported because it is the shape the client asserts on a built
    request; a pattern that admits `00-...-01 ` there would let a trailing
    space reach a header even though this module's own checker refused it."""
    assert TRACEPARENT_PATTERN.fullmatch(value) is None


# --- the caller's value -------------------------------------------------------


def test_a_valid_caller_value_is_returned_verbatim() -> None:
    """Catches a `traceparent_for` that always mints (plan decision C's option
    ii): an agent turn that already holds a trace would then start a second,
    unconnected one, and the two services' spans would never join. Verbatim,
    not merely valid — a re-encoded or re-flagged header is a different span
    id and breaks the same join."""
    assert traceparent_for(VALID) == VALID


def test_no_caller_value_mints_a_fresh_header() -> None:
    """Catches a `traceparent_for` that returns `None` or the empty string
    when the caller passes none: the client would then send `traceparent: ""`
    or omit the header, and every unparented call would be untraceable in the
    console."""
    minted = traceparent_for(None)

    assert TRACEPARENT_PATTERN.fullmatch(minted) is not None
    assert minted != VALID


def test_a_malformed_caller_value_is_refused_rather_than_replaced() -> None:
    """Catches a `traceparent_for` that swallows a bad caller value and mints
    over it. Silently substituting would hide the caller's bug behind a trace
    that joins nothing, so the refusal is what makes the malformed header a
    local `VALIDATION` instead of an unexplained gap in the trace."""
    with pytest.raises(ToolError) as excinfo:
        traceparent_for(VALID + "\n")

    assert excinfo.value.code is ErrorCode.VALIDATION


# --- an earlier change: the active span the header joins -------------------------------


@pytest.fixture
def traced() -> Iterator[FakeTraceClient]:
    """A recording trace client installed for one case, and removed after it.

    Not autouse: every case above this line must keep running with tracing
    exactly as it is at rest — unconfigured — which is what makes the mint
    case the plan's proof that the seam is additive.
    """
    client = FakeTraceClient()
    configure_tracing(client=client)
    try:
        yield client
    finally:
        configure_tracing()


def test_an_active_span_makes_the_header_carry_that_trace_and_that_span(
    traced: FakeTraceClient,
) -> None:
    """Catches a `traceparent_for` that still mints from `os.urandom` when a
    trace is active: the console would record the model call under a trace id
    this process has never heard of, and the design's two-service join would
    silently be two disconnected traces that both look healthy.

    The span id half is asserted too, and it is the one a "trace id only"
    implementation fails: a header carrying the right trace and a random
    parent hangs the gateway's span off nothing, so the call shows in the
    trace with no caller."""
    with turn_trace(), span("a-node"):
        header = traceparent_for(None)
        trace_id = current_trace_id()
        span_id = current_span_id()

    # Premise, read off the facade: this case only means something while a
    # span really is active.
    assert trace_id is not None
    assert span_id is not None

    assert TRACEPARENT_PATTERN.fullmatch(header) is not None
    assert header == f"00-{trace_id}-{span_id}-{TRACE_FLAGS}"


def test_a_caller_value_still_wins_over_an_active_span(traced: FakeTraceClient) -> None:
    """Catches a seam that OVERWRITES the caller's header with the local span
    once tracing is on. The caller's value is a parent from another process; replacing it re-parents the call onto this service
    and loses the join the caller was asking for — and this is the only case
    where both a caller value and an active span exist at once, so nothing
    else can decide it."""
    with turn_trace():
        assert traceparent_for(VALID) == VALID


def test_with_the_trace_closed_the_header_is_minted_fresh_again(
    traced: FakeTraceClient,
) -> None:
    """Catches a seam that caches the last trace it saw (a module global set
    inside `turn_trace`): a call made after the turn ended — a background
    ingest, a health probe — would be filed under that finished turn's trace
    forever. The client here is still installed, so only the "no ACTIVE span"
    branch can produce the mint."""
    with turn_trace():
        traced_header = traceparent_for(None)

    minted = traceparent_for(None)

    assert TRACEPARENT_PATTERN.fullmatch(minted) is not None
    assert minted.split("-")[1] != traced_header.split("-")[1]
