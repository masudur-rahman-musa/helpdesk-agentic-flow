"""Langfuse tracing behind a small wrapper that never fails its caller.

Client errors are swallowed. Tracing is off unless both keys are set. Only
names, ids and counts are recorded, never a message; `None` values are dropped.
"""

from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager, suppress
from typing import TYPE_CHECKING, Any, Protocol, cast

from langfuse import Langfuse

if TYPE_CHECKING:
    from helpdesk.shared.core.config import Settings

TURN_TRACE_NAME = "chat_turn"
"""The trace name every turn uses, so a saved Langfuse view finds them all."""

_SPAN_TYPE = "span"
"""Plain spans; a `generation` would ask for cost fields this project does not record."""


class TraceClient(Protocol):
    """The SDK methods this wrapper uses, so tests can use a fake client."""

    def start_as_current_observation(
        self, *, name: str, as_type: str = _SPAN_TYPE
    ) -> AbstractContextManager[Any]: ...

    def update_current_span(self, *, metadata: Mapping[str, Any] | None = None) -> None: ...

    def get_current_trace_id(self) -> str | None: ...

    def get_current_observation_id(self) -> str | None: ...

    def set_current_trace_as_public(self) -> None: ...

    def flush(self) -> None: ...


_CLIENT: TraceClient | None = None
"""The installed client, or `None` when tracing is off."""

_PUBLIC = False
"""Whether every span is marked public. Off sends no mark at all."""


def configure_tracing(
    *,
    client: TraceClient | None = None,
    settings: "Settings | None" = None,
    public: bool | None = None,
) -> None:
    """Install `client`, or build one from `settings` with both keys, or turn tracing off.

    `public` wins over `settings.public_trace_langfuse`; both missing means off.
    """
    global _CLIENT, _PUBLIC
    if public is not None:
        _PUBLIC = public
    else:
        _PUBLIC = settings.public_trace_langfuse if settings is not None else False
    if client is not None:
        _CLIENT = client
        return
    if settings is not None and settings.langfuse_public_key and settings.langfuse_secret_key:
        try:
            # `Langfuse` has the methods `TraceClient` lists, but is not a structural match.
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


def _mark_public(client: TraceClient) -> None:
    """Mark the span just opened public, when the switch is on.

    Every span, not only the root: Langfuse keeps the flag of the span that
    started last and reads a missing one as false. Pinned by
    `test_when_public_every_span_of_a_turn_is_marked_not_only_the_root`.
    """
    if not _PUBLIC:
        return
    try:
        client.set_current_trace_as_public()
    except Exception:
        return


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
    """Open one span around this block, or run it untraced.

    Only the client's own errors are swallowed; the block's errors are raised.
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
    _mark_public(client)
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
    """The root span of one turn, which is that turn's trace."""
    with span(name, attributes=attributes):
        yield


def set_trace_attributes(attributes: Mapping[str, Any]) -> None:
    """Write attributes onto whichever span is active now, not the root."""
    client = _CLIENT
    if client is None:
        return
    _write(client, attributes)


def current_trace_id() -> str | None:
    """The active trace id as 32 lowercase hex, or `None`. It goes into a `traceparent`."""
    client = _CLIENT
    if client is None:
        return None
    try:
        return client.get_current_trace_id()
    except Exception:
        return None


def current_span_id() -> str | None:
    """The active span id as 16 lowercase hex, or `None`."""
    client = _CLIENT
    if client is None:
        return None
    try:
        return client.get_current_observation_id()
    except Exception:
        return None


def flush() -> None:
    """Send what the exporter holds. Shutdown only, never inside a turn."""
    client = _CLIENT
    if client is None:
        return
    try:
        client.flush()
    except Exception:
        return
