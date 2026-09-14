"""The Langfuse trace, behind one facade that can never fail a caller.

the design asks for a trace per chat turn with a span per graph node. This
module owns the whole of that: the client, the decision that an unconfigured
deployment traces nothing, and the rule that a broken SDK is invisible to the
code it was only supposed to describe.

**Fail open, always.** A trace is a record of a turn, never a condition of
one. Every function here swallows whatever the client raises and answers the
honest "no trace" instead — `current_trace_id()` is `None`, a `span` block
still runs its body, `flush()` returns. Nothing in this module reaches
`/readyz` for the same reason: readiness is the three dependencies a turn
actually needs. Pinned by
`test_obs_tracing.py::test_a_client_whose_every_method_raises_never_reaches_the_caller`
and, end to end,
`test_agent_tracing.py::test_a_client_whose_every_method_raises_leaves_the_turn_untouched`.

**Unconfigured means no client at all.** Both keys or nothing: half a
credential builds an exporter that can only retry a 401 behind every turn, so
`configure_tracing` constructs nothing unless both are set. Pinned by
`test_obs_tracing.py::test_settings_without_keys_construct_no_client_at_all`
and `::test_a_half_configured_deployment_builds_no_client`.

**Attributes only, never input or output** (decision G). Callers hand this
module closed enum members, node names, registry tool names, uuids and
counts; nothing here captures a message, a tool argument or a retrieved
chunk, which is what makes the design's promise a property of the code rather
than of the care taken at each call site. The sweep that proves it is
`test_agent_tracing.py::test_no_span_of_a_turn_carries_a_person_the_user_id_or_the_message`.

An attribute whose value is `None` is DROPPED rather than sent: `risk_class`
is `None` on every turn no tool classified, and a `risk_class: null` reads as
"classified as nothing" where the truth is "never classified"
(`test_obs_tracing.py::test_an_attribute_whose_value_is_none_is_dropped_rather_than_sent`).
"""

from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager, suppress
from typing import TYPE_CHECKING, Any, Protocol, cast

from langfuse import Langfuse

if TYPE_CHECKING:
    from helpdesk.core.settings import Settings

TURN_TRACE_NAME = "chat_turn"
"""The root span's name, and so the trace's, for one chat turn.

One constant rather than a literal at each of the three drivers (`run_turn`,
`stream_turn`, `resume_graph`): the name is what a saved Langfuse view filters
a turn by, so three call sites free to pick their own would make every view
select a third of the traffic. a project rule, pinned by
`test_obs_tracing.py::test_the_turn_trace_name_is_exactly_chat_turn`.
"""

_SPAN_TYPE = "span"
"""The observation type every span this project opens is created as.

Langfuse's own default, named here because the facade passes it explicitly: a `generation` would ask for token and cost fields this
project deliberately does not capture.
"""


class TraceClient(Protocol):
    """The five SDK methods this facade uses, and nothing else.

    A protocol rather than `Langfuse` itself so every test drives a recording
    or raising fake and no test constructs a real client or reaches a network
    (`test_obs_tracing.py`'s `FakeTraceClient`). It is also the seam that
    makes the version dependency explicit: these five names are `langfuse`
    4.x's, checked against the installed version before this module was
    written.
    """

    def start_as_current_observation(
        self, *, name: str, as_type: str = _SPAN_TYPE
    ) -> AbstractContextManager[Any]: ...

    def update_current_span(self, *, metadata: Mapping[str, Any] | None = None) -> None: ...

    def get_current_trace_id(self) -> str | None: ...

    def get_current_observation_id(self) -> str | None: ...

    def flush(self) -> None: ...


_CLIENT: TraceClient | None = None
"""The installed client, or `None` when tracing is disabled.

A module global rather than something threaded through every call: a trace
client is a process resource exactly like the log configuration beside it, and
the alternative is a parameter on `run_turn`, on every node and on the tool
caller for a value none of them decides.
"""


def configure_tracing(
    *, client: TraceClient | None = None, settings: "Settings | None" = None
) -> None:
    """Install this process's trace client, or disable tracing.

    Three inputs, in order: an explicit `client` (the seam the tests drive), a
    `settings` carrying BOTH Langfuse keys, or neither — which disables
    tracing and constructs nothing. The "and" is deliberate and is what
    `test_obs_tracing.py::test_a_half_configured_deployment_builds_no_client`
    fails an `or` on.

    Never raises, including when the SDK's own constructor does: a deployment
    whose Langfuse host is malformed still serves turns.
    """
    global _CLIENT
    if client is not None:
        _CLIENT = client
        return
    if settings is not None and settings.langfuse_public_key and settings.langfuse_secret_key:
        try:
            # `Langfuse` satisfies the subset of its API this module uses, which
            # is exactly what `TraceClient` declares; the SDK's own class is
            # wider and not a structural match.
            _CLIENT = cast(
                TraceClient,
                Langfuse(
                    public_key=settings.langfuse_public_key,
                    secret_key=settings.langfuse_secret_key,
                    host=settings.langfuse_host,
                ),
            )
        except Exception:
            _CLIENT = None
        return
    _CLIENT = None


def tracing_enabled() -> bool:
    """Whether a client is installed. Not whether it works."""
    return _CLIENT is not None


def _clean(attributes: Mapping[str, Any] | None) -> dict[str, Any]:
    """The attributes worth sending: the null-valued ones dropped."""
    if not attributes:
        return {}
    return {key: value for key, value in attributes.items() if value is not None}


def _write(client: TraceClient, attributes: Mapping[str, Any] | None) -> None:
    """Put `attributes` on the currently active span, or do nothing."""
    metadata = _clean(attributes)
    if not metadata:
        return
    try:
        client.update_current_span(metadata=metadata)
    except Exception:
        return


@contextmanager
def span(name: str, *, attributes: Mapping[str, Any] | None = None) -> Iterator[None]:
    """Open one observation around this block, or run the block untraced.

    The context manager is entered and exited by hand rather than with a
    nested `with`, for one reason: the swallow has to sit AROUND the client's
    own enter and exit and never around the body. An implementation wrapping
    the whole `with` in `try/except Exception` would silently eat a node's
    failure and answer the turn as though it had run — so the body's exception
    is re-raised here after the span is closed with it, and only the client's
    is dropped.

    Pinned by
    `test_obs_tracing.py::test_a_client_whose_every_method_raises_never_reaches_the_caller`
    (the client's exception never reaches the caller) and by
    `test_agent_tracing.py::test_a_client_whose_every_method_raises_leaves_the_turn_untouched`
    (the turn's own answer is unchanged).
    """
    client = _CLIENT
    if client is None:
        yield
        return
    try:
        opened = client.start_as_current_observation(name=name, as_type=_SPAN_TYPE)
        opened.__enter__()
    except Exception:
        yield
        return
    _write(client, attributes)
    try:
        yield
    except BaseException as exc:
        with suppress(Exception):
            opened.__exit__(type(exc), exc, exc.__traceback__)
        raise
    try:
        opened.__exit__(None, None, None)
    except Exception:
        return


@contextmanager
def turn_trace(
    name: str = TURN_TRACE_NAME, *, attributes: Mapping[str, Any] | None = None
) -> Iterator[None]:
    """The root observation of one turn, which IS that turn's trace.

    A root span and a trace are the same thing in Langfuse 4, so this is
    `span` under the name the turn's own constant fixes; it exists separately
    because the callers are the three turn drivers and reading `turn_trace()`
    at those three sites says which of the nested spans is the turn.

    One per turn, never per process: a resume is its own turn and opens its
    own, which is what
    `test_agent_tracing.py::test_a_paused_turn_closes_its_trace_and_the_resume_opens_its_own`
    asserts as two different trace ids.
    """
    with span(name, attributes=attributes):
        yield


def set_trace_attributes(attributes: Mapping[str, Any]) -> None:
    """Write attributes onto whichever span is active right now.

    Called at a turn's terminal, where the active span is the turn's root, and
    by the node wrapper, where it is that node's span. It deliberately does
    NOT target the root: the node spans carry `model_alias` and
    `prompt_version` through this same call, and a root-targeting version
    would stack all three prompt versions onto the trace and leave the node
    spans bare. Pinned by
    `test_obs_tracing.py::test_set_trace_attributes_writes_onto_the_span_that_is_active`.
    """
    client = _CLIENT
    if client is None:
        return
    _write(client, attributes)


def current_trace_id() -> str | None:
    """The active trace's id as 32 lowercase hex, or `None` outside one.

    The shape matters because the value is spliced into a W3C `traceparent`
    by `helpdesk.gateway.traceparent`, whose grammar is exactly that. Pinned
    by
    `test_obs_tracing.py::test_the_current_ids_are_hex_of_the_spec_lengths_inside_a_trace_and_none_outside`.
    """
    client = _CLIENT
    if client is None:
        return None
    try:
        return client.get_current_trace_id()
    except Exception:
        return None


def current_span_id() -> str | None:
    """The active observation's id as 16 lowercase hex, or `None`.

    The other half of the `traceparent`: without it the gateway's span hangs
    off a random parent, so the call shows in the trace with no caller.
    """
    client = _CLIENT
    if client is None:
        return None
    try:
        return client.get_current_observation_id()
    except Exception:
        return None


def flush() -> None:
    """Push whatever the exporter is still batching. Called at shutdown only.

    Never on the request path: an export awaited
    inside a turn would put a network round trip between a user and their
    reply. Pinned by
    `test_obs_tracing.py::test_flush_is_forwarded_to_the_installed_client`.
    """
    client = _CLIENT
    if client is None:
        return
    try:
        client.flush()
    except Exception:
        return
