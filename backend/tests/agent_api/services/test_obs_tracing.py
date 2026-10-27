"""The tracing facade in `helpdesk/obs/tracing.py`.

Written before the module exists. Expected implementation surface (the
implementer must provide exactly this, re-exported from ``helpdesk.obs``):

``helpdesk.obs.tracing``
    ``TURN_TRACE_NAME = "chat_turn"`` — the trace/root-span name of one turn.
    ``class TraceClient(Protocol)`` — the five SDK methods this facade uses:
        ``start_as_current_observation(*, name, as_type="span")`` returning a
        context manager, ``update_current_span(*, metadata=None)``,
        ``get_current_trace_id()``, ``get_current_observation_id()``,
        ``flush()``.
    ``def configure_tracing(*, client=None, settings=None) -> None`` — a given
        ``client`` is installed (the seam every test here drives); otherwise a
        ``settings`` carrying BOTH ``langfuse_public_key`` and
        ``langfuse_secret_key`` builds a ``Langfuse``; otherwise tracing is
        DISABLED and no client object is constructed at all. Never raises.
    ``def tracing_enabled() -> bool``
    ``@contextmanager def turn_trace(name=TURN_TRACE_NAME, *, attributes=None)``
    ``@contextmanager def span(name, *, attributes=None)``
    ``def set_trace_attributes(attributes) -> None`` — metadata onto the
        currently active span.
    ``def current_trace_id() -> str | None`` · ``def current_span_id() -> str | None``
    ``def flush() -> None``
    Every one of them swallows whatever the client raises (fail
    open) and passes ``attributes`` to the client as ``metadata``, dropping
    the entries whose value is ``None``.

``helpdesk.core.settings.Settings`` gains ``langfuse_public_key: str | None =
None``, ``langfuse_secret_key: str | None = None`` and ``langfuse_host: str =
"https://cloud.langfuse.com"``.

Cost and isolation: NO backing service and no network. The facade is driven
against a recording fake or a raising fake, and the one case about the real
SDK replaces ``Langfuse.__init__`` so that "was a client constructed" is a
recorded fact rather than an outbound connection. The autouse fixture below
disables tracing after every case, so no installed client leaks into the next
one.
"""

import re
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from langfuse import Langfuse
from langfuse._client.attributes import LangfuseOtelSpanAttributes
from pydantic import ValidationError

from helpdesk.agent_api.services.tracing import (
    TURN_TRACE_NAME,
    configure_tracing,
    current_span_id,
    current_trace_id,
    flush,
    set_trace_attributes,
    span,
    tracing_enabled,
    turn_trace,
)
from helpdesk.shared.core.config import Settings

TRACE_ID_PATTERN = re.compile(r"[0-9a-f]{32}")
"""What the SDK's trace id is: 32 lowercase hex characters, no dashes.

The same 32 the W3C `traceparent` carries, which is why
`test_gateway_traceparent.py`'s active-span cases can splice this value
straight into a header."""

SPAN_ID_PATTERN = re.compile(r"[0-9a-f]{16}")
"""And the observation id: 16 lowercase hex characters."""

# Obviously fake, tests only: these are not credentials, and
# nothing here reaches Langfuse.
FAKE_PUBLIC_KEY = "pk-fake-not-a-key"
FAKE_SECRET_KEY = "sk-fake-not-a-key"
FAKE_HOST = "https://langfuse.invalid"

# >= 32 bytes, so the settings length gate passes. Obviously fake.
FAKE_JWT_SECRET = "obviously-fake-tracing-test-jwt-secret-0123456789"


# --- the recording fake -------------------------------------------------------


@dataclass
class RecordedSpan:
    """One observation the facade opened, with what it was told about it."""

    name: str
    as_type: str
    trace_id: str
    span_id: str
    metadata: dict[str, Any] = field(default_factory=dict)
    public: bool = False
    children: list["RecordedSpan"] = field(default_factory=list)


class FakeTraceClient:
    """A `TraceClient` that records the tree instead of exporting it.

    Nesting is a plain stack rather than a context variable: the facade is
    what owns the SDK's context, so a fake that kept its own would be
    asserting on itself. A root observation mints a fresh trace id, and every
    child inherits its root's — which is what makes "the resume is its own
    trace" assertable as two different ids.
    """

    def __init__(self) -> None:
        self.roots: list[RecordedSpan] = []
        self.flushes = 0
        self.mark_calls = 0
        self.closed: list[str] = []
        self._stack: list[RecordedSpan] = []

    @contextmanager
    def start_as_current_observation(
        self, *, name: str, as_type: str = "span"
    ) -> Iterator[RecordedSpan]:
        parent = self._stack[-1] if self._stack else None
        recorded = RecordedSpan(
            name=name,
            as_type=as_type,
            trace_id=uuid.uuid4().hex if parent is None else parent.trace_id,
            span_id=uuid.uuid4().hex[:16],
        )
        if parent is None:
            self.roots.append(recorded)
        else:
            parent.children.append(recorded)
        self._stack.append(recorded)
        try:
            yield recorded
        finally:
            self._stack.pop()
            self.closed.append(name)

    def update_current_span(self, *, metadata: Mapping[str, Any] | None = None) -> None:
        if metadata is not None and self._stack:
            self._stack[-1].metadata.update(metadata)

    def set_current_trace_as_public(self) -> None:
        self.mark_calls += 1
        if self._stack:
            self._stack[-1].public = True

    def get_current_trace_id(self) -> str | None:
        return self._stack[-1].trace_id if self._stack else None

    def get_current_observation_id(self) -> str | None:
        return self._stack[-1].span_id if self._stack else None

    def flush(self) -> None:
        self.flushes += 1

    @property
    def only_root(self) -> RecordedSpan:
        assert len(self.roots) == 1, f"expected one trace, recorded {len(self.roots)}"
        return self.roots[0]


class RaisingTraceClient:
    """A `TraceClient` whose every method fails, as a broken SDK would.

    `RuntimeError` rather than a Langfuse type on purpose: the facade may not
    know what the SDK raises, so an implementation catching one library class
    would pass here and still take a turn down in production.
    """

    def __init__(self) -> None:
        self.calls = 0

    def start_as_current_observation(self, *, name: str, as_type: str = "span") -> Any:
        self.calls += 1
        raise RuntimeError("the trace client is broken")

    def update_current_span(self, *, metadata: Mapping[str, Any] | None = None) -> None:
        self.calls += 1
        raise RuntimeError("the trace client is broken")

    def set_current_trace_as_public(self) -> None:
        self.calls += 1
        raise RuntimeError("the trace client is broken")

    def get_current_trace_id(self) -> str | None:
        self.calls += 1
        raise RuntimeError("the trace client is broken")

    def get_current_observation_id(self) -> str | None:
        self.calls += 1
        raise RuntimeError("the trace client is broken")

    def flush(self) -> None:
        self.calls += 1
        raise RuntimeError("the trace client is broken")


@dataclass(frozen=True)
class ClientConstruction:
    """One `Langfuse(...)` that the facade actually performed."""

    kwargs: dict[str, Any]


@pytest.fixture
def constructions(monkeypatch: pytest.MonkeyPatch) -> list[ClientConstruction]:
    """Every real client construction, recorded and neutered.

    `Langfuse.__init__` rather than the name the facade imports: patching the
    class's own initialiser records the construction whichever way the module
    imported the symbol, and nothing dials anywhere.
    """
    recorded: list[ClientConstruction] = []

    def _record(self: Langfuse, **kwargs: Any) -> None:
        recorded.append(ClientConstruction(kwargs=dict(kwargs)))

    monkeypatch.setattr(Langfuse, "__init__", _record)
    return recorded


@pytest.fixture(autouse=True)
def _tracing_disabled_after_every_case() -> Iterator[None]:
    yield
    configure_tracing()


def _settings(**langfuse: str | bool | None) -> Settings:
    """Settings with the three Langfuse fields set exactly as the case says.

    `model_copy` rather than constructor arguments so an operator's own
    `.env` cannot configure the "unconfigured" case out from under it.
    """
    base = Settings(
        database_url="postgresql+psycopg://fake:fake@localhost:5432/fake",
        redis_url="redis://localhost:6379/0",
        qdrant_url="http://localhost:6333",
        jwt_secret=FAKE_JWT_SECRET,
    )
    fields: dict[str, Any] = {"langfuse_public_key": None, "langfuse_secret_key": None}
    fields.update(langfuse)
    return base.model_copy(update=fields)


def _body() -> str:
    """The one thing every no-op case checks still happened."""
    return "the block ran"


# --- the constants ------------------------------------------------------------


def test_the_turn_trace_name_is_exactly_chat_turn() -> None:
    """Catches a root-span name invented at the call site or renamed to the
    service ("agent-api", "run_turn"): the trace name is what a Langfuse view
    filters one chat turn by, so a drifting name makes every saved view of
    this project select nothing."""
    assert TURN_TRACE_NAME == "chat_turn"


def test_the_three_langfuse_settings_carry_exactly_the_planned_defaults() -> None:
    """Catches a `langfuse_host` left required (every existing deployment's
    settings load then fails on a field nobody set) or defaulted to some other
    endpoint, and keys defaulted to `""` rather than `None` — an empty string
    is truthy enough for a careless `is not None` check, so the facade would
    build a client with no credentials and every turn would pay an export
    that cannot succeed.

    Read off `model_fields` rather than an instance: the default is the thing
    under test, and an instance would take whatever the environment says."""
    assert Settings.model_fields["langfuse_host"].default == "https://cloud.langfuse.com"
    assert Settings.model_fields["langfuse_public_key"].default is None
    assert Settings.model_fields["langfuse_secret_key"].default is None


# --- unconfigured: no client, no trace, and the body still runs ---------------


def test_settings_without_keys_construct_no_client_at_all(
    constructions: list[ClientConstruction],
) -> None:
    """Catches a facade that builds a `Langfuse` first and decides afterwards
    whether to use it: an unconfigured deployment would then construct an SDK
    client (and its exporter thread) on every startup, which is exactly the
    "opens no client" half of the unconfigured case — a `tracing_enabled()`
    that answered False would hide it."""
    configure_tracing(settings=_settings())

    assert constructions == []
    assert tracing_enabled() is False


def test_an_unconfigured_facade_traces_nothing_and_still_runs_the_block() -> None:
    """Catches a no-op path that skips the body — a `turn_trace` implemented
    as "return early when disabled" without yielding at all, which turns every
    unconfigured turn into a turn that never ran a node.

    `current_trace_id()` is the other half: with no client there is no trace,
    so a facade inventing an id would put a trace id on log lines and on the
    `traceparent` header that names no trace in any store."""
    configure_tracing(settings=_settings())

    with turn_trace():
        outer = _body()
        with span("a-node"):
            inner = _body()
            trace_id = current_trace_id()
            span_id = current_span_id()
        set_trace_attributes({"decision": "done"})

    assert outer == "the block ran"
    assert inner == "the block ran"
    assert trace_id is None
    assert span_id is None
    assert current_trace_id() is None


def test_both_keys_configured_build_exactly_one_client_on_the_configured_host(
    constructions: list[ClientConstruction],
) -> None:
    """Catches a facade that ignores `langfuse_host` and posts to the SDK's
    own default endpoint — a self-hosted deployment would then ship its traces
    to a service it never configured — and one that builds a client per span
    rather than once per `configure_tracing`.

    A public key with no secret is the boundary row: half a credential is not
    a configuration, and building a client from it means an exporter retrying
    a 401 behind every turn."""
    configure_tracing(
        settings=_settings(
            langfuse_public_key=FAKE_PUBLIC_KEY,
            langfuse_secret_key=FAKE_SECRET_KEY,
            langfuse_host=FAKE_HOST,
        )
    )

    assert len(constructions) == 1
    assert constructions[0].kwargs["host"] == FAKE_HOST
    assert constructions[0].kwargs["public_key"] == FAKE_PUBLIC_KEY
    assert constructions[0].kwargs["secret_key"] == FAKE_SECRET_KEY
    assert tracing_enabled() is True


@pytest.mark.parametrize(
    ("public_key", "secret_key"),
    [
        pytest.param(FAKE_PUBLIC_KEY, None, id="public-key-only"),
        pytest.param(None, FAKE_SECRET_KEY, id="secret-key-only"),
        pytest.param(None, None, id="neither-key"),
    ],
)
def test_a_half_configured_deployment_builds_no_client(
    public_key: str | None,
    secret_key: str | None,
    constructions: list[ClientConstruction],
) -> None:
    """Catches a configured-check written as `or` instead of `and`: one key
    without the other builds a client that can only fail authentication, and
    the failure surfaces as an exporter warning behind every turn rather than
    as the disabled facade the operator would expect."""
    configure_tracing(
        settings=_settings(langfuse_public_key=public_key, langfuse_secret_key=secret_key)
    )

    assert constructions == []
    assert tracing_enabled() is False


# --- fail open: a broken client never reaches the caller ----------------------


def test_a_client_whose_every_method_raises_never_reaches_the_caller() -> None:
    """Catches a facade that lets the SDK's exception out:
    a Langfuse outage would then fail the chat turn it was
    only supposed to describe, and every node inside the block would be
    skipped. The client here raises from `start_as_current_observation`, so
    only a swallow AROUND the context manager keeps the body running — a
    try/except inside the `with` would not."""
    client = RaisingTraceClient()
    configure_tracing(client=client)

    with turn_trace(attributes={"tenant_id": "fake"}):
        outer = _body()
        with span("a-node", attributes={"model_alias": "agent-default"}):
            inner = _body()
        set_trace_attributes({"decision": "done"})
        trace_id = current_trace_id()
        span_id = current_span_id()
    flush()

    assert outer == "the block ran"
    assert inner == "the block ran"
    assert trace_id is None
    assert span_id is None
    # Premise, read off the fake: the facade really did call it, so the
    # swallow above is what kept the block alive rather than a disabled client.
    assert client.calls > 0


# --- the recording client: names, nesting, attributes -------------------------


def test_the_names_and_attributes_reach_the_client_nested_in_call_order() -> None:
    """Catches a facade that opens every span as a root — spans nested in the
    source would come back as a flat list, so a tool span could not be shown
    under the node that made it — and one that passes `attributes` under some
    other key than `metadata`, which loses every attribute silently.

    The order is asserted as a list, not a set: the child spans are
    the nodes "in the order it ran them"."""
    client = FakeTraceClient()
    configure_tracing(client=client)

    with turn_trace(attributes={"tenant_id": "fake-tenant"}):
        with span("first_node"), span("tool:search_policy"):
            pass
        with span("second_node", attributes={"model_alias": "agent-default"}):
            pass

    root = client.only_root
    assert root.name == TURN_TRACE_NAME
    assert root.metadata == {"tenant_id": "fake-tenant"}
    assert [child.name for child in root.children] == ["first_node", "second_node"]
    assert [child.name for child in root.children[0].children] == ["tool:search_policy"]
    assert root.children[1].children == []
    assert root.children[1].metadata == {"model_alias": "agent-default"}


def test_set_trace_attributes_writes_onto_the_span_that_is_active() -> None:
    """Catches a `set_trace_attributes` that always writes onto the ROOT (or
    onto whatever it cached at `configure_tracing` time): the node spans carry
    `model_alias` and `prompt_version` through this same call, so a
    root-targeting implementation would stack all three nodes' prompt versions
    onto the trace and leave the node spans bare."""
    client = FakeTraceClient()
    configure_tracing(client=client)

    with turn_trace():
        with span("extract_intent"):
            set_trace_attributes({"prompt_version": "fake-digest"})
        set_trace_attributes({"decision": "done"})

    root = client.only_root
    assert root.metadata == {"decision": "done"}
    assert root.children[0].metadata == {"prompt_version": "fake-digest"}


def test_an_attribute_whose_value_is_none_is_dropped_rather_than_sent() -> None:
    """Catches a facade that forwards `None` through to the SDK. `risk_class`
    is `None` on every turn no tool classified, so a forwarded null would put
    a `risk_class: null` on those traces — a field that reads as "classified
    as nothing" where the truth is "never classified"."""
    client = FakeTraceClient()
    configure_tracing(client=client)

    with turn_trace(attributes={"tenant_id": "fake-tenant", "risk_class": None}):
        with span("plan_tools", attributes={"model_alias": "agent-strong", "prompt_version": None}):
            pass
        set_trace_attributes({"decision": "done", "risk_class": None})

    root = client.only_root
    assert root.metadata == {"tenant_id": "fake-tenant", "decision": "done"}
    assert root.children[0].metadata == {"model_alias": "agent-strong"}


def test_the_current_ids_are_hex_of_the_spec_lengths_inside_a_trace_and_none_outside() -> None:
    """Catches a `current_trace_id` returning the SDK's own object, a dashed
    uuid or an uppercase id: the value is spliced into a `traceparent`, whose
    grammar is 32 lowercase hex, so anything else is a header the console
    answers 422 to on every call. And it must be `None` outside a trace —
    an id retained after the block would attribute a startup line, or the next
    unrelated call, to a turn that has ended."""
    client = FakeTraceClient()
    configure_tracing(client=client)

    assert current_trace_id() is None
    assert current_span_id() is None

    with turn_trace():
        trace_id = current_trace_id()
        root_span_id = current_span_id()
        with span("a-node"):
            nested_trace_id = current_trace_id()
            nested_span_id = current_span_id()

    assert trace_id is not None
    assert TRACE_ID_PATTERN.fullmatch(trace_id) is not None
    assert root_span_id is not None
    assert SPAN_ID_PATTERN.fullmatch(root_span_id) is not None
    # One trace, two observations: the nested span shares the trace and has an
    # id of its own, which is what makes a `traceparent` name a real parent.
    assert nested_trace_id == trace_id
    assert nested_span_id != root_span_id
    assert current_trace_id() is None
    assert current_span_id() is None


def test_two_traces_do_not_share_a_trace_id() -> None:
    """Catches a facade holding ONE trace for the process (a client-level
    trace opened at `configure_tracing`): every turn the service ever runs
    would then be one unreadable trace, and the resume-is-its-own-turn rule
    could not hold."""
    client = FakeTraceClient()
    configure_tracing(client=client)

    with turn_trace():
        first = current_trace_id()
    with turn_trace():
        second = current_trace_id()

    assert first is not None
    assert second is not None
    assert first != second
    assert len(client.roots) == 2


def test_flush_is_forwarded_to_the_installed_client() -> None:
    """Catches a `flush()` that is a no-op stub: the lifespan's shutdown call
    is the only thing that gets the last turn's spans out of the batching
    exporter, so a stubbed flush loses every trace of the final requests a
    process served."""
    client = FakeTraceClient()
    configure_tracing(client=client)

    flush()

    assert client.flushes == 1


# --- public traces -------------------------------------------------------------

_PUBLIC_ENV = "PUBLIC_TRACE_LANGFUSE"


def _settings_from_env() -> Settings:
    """Settings where the environment set by the case wins over any env file."""
    return Settings(
        database_url="postgresql+psycopg://fake:fake@localhost:5432/fake",
        redis_url="redis://localhost:6379/0",
        qdrant_url="http://localhost:6333",
        jwt_secret=FAKE_JWT_SECRET,
    )


def _open_a_turn_of_four_spans() -> None:
    """A root, two spans under it, and a third nested one: four in all."""
    with turn_trace():
        with span("first_node"), span("tool:search_policy"):
            pass
        with span("second_node"):
            pass


def test_the_public_trace_setting_is_off_when_missing() -> None:
    """Catches a default of True (or a required field): a deployment that never
    set the switch would publish every trace to anyone holding a link."""
    assert Settings.model_fields["public_trace_langfuse"].default is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [pytest.param("true", True, id="true"), pytest.param("false", False, id="false")],
)
def test_the_public_trace_setting_reads_true_and_false(
    raw: str, expected: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a field read from the wrong env name, or one that ignores the
    value and always answers its default: the demo env would then not turn the
    switch on, or the switch could never be turned off."""
    monkeypatch.setenv(_PUBLIC_ENV, raw)

    assert _settings_from_env().public_trace_langfuse is expected


def test_a_public_trace_value_that_is_not_a_bool_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a field typed as a plain string (or a loose parser that turns
    any word into True): a typo like "maybe" would then publish traces instead
    of stopping the service at start."""
    monkeypatch.setenv(_PUBLIC_ENV, "maybe")

    with pytest.raises(ValidationError):
        _settings_from_env()


def test_when_public_every_span_of_a_turn_is_marked_not_only_the_root() -> None:
    """Catches an implementation that marks only the root span: Langfuse keeps
    the value from the span that started last, so one unmarked later span
    turns the trace private again. Also catches a double mark on a span."""
    client = FakeTraceClient()
    configure_tracing(client=client, public=True)

    _open_a_turn_of_four_spans()

    root = client.only_root
    spans = [root, *root.children, *root.children[0].children]
    assert len(spans) == 4
    assert [s.public for s in spans] == [True, True, True, True]
    assert client.mark_calls == 4


def test_when_not_public_no_span_is_marked_at_all() -> None:
    """Catches an implementation that sends a mark of false (or marks anyway)
    when the switch is off: off must send nothing, since Langfuse keeps the
    last value it was given."""
    client = FakeTraceClient()
    configure_tracing(client=client, public=False)

    _open_a_turn_of_four_spans()

    assert client.mark_calls == 0
    assert client.only_root.public is False


def test_settings_with_public_on_reach_the_client_built_from_keys(
    constructions: list[ClientConstruction], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a `configure_tracing` that builds the client from settings but
    drops the public switch (only honouring the `public` keyword): the demo env
    would turn the setting on and still send private traces."""
    marked: list[int] = []
    opened: list[str] = []

    @contextmanager
    def _fake_observation(self: Langfuse, *, name: str, as_type: str = "span") -> Iterator[None]:
        opened.append(name)
        yield

    def _fake_mark(self: Langfuse) -> None:
        marked.append(1)

    def _fake_update(self: Langfuse, *, metadata: Mapping[str, Any] | None = None) -> None:
        return None

    monkeypatch.setattr(Langfuse, "start_as_current_observation", _fake_observation)
    monkeypatch.setattr(Langfuse, "update_current_span", _fake_update)
    monkeypatch.setattr(Langfuse, "set_current_trace_as_public", _fake_mark)

    configure_tracing(
        settings=_settings(
            langfuse_public_key=FAKE_PUBLIC_KEY,
            langfuse_secret_key=FAKE_SECRET_KEY,
            public_trace_langfuse=True,
        )
    )
    with span("x"):
        pass

    assert len(constructions) == 1
    assert opened == ["x"]
    assert marked == [1]


def test_a_failing_public_mark_never_reaches_the_caller() -> None:
    """Catches a mark call left outside the swallow: a Langfuse hiccup would
    fail the chat turn, or leave the span open because the exit was skipped."""

    class FailingMarkClient(FakeTraceClient):
        def set_current_trace_as_public(self) -> None:
            self.mark_calls += 1
            raise RuntimeError("the mark is broken")

    client = FailingMarkClient()
    configure_tracing(client=client, public=True)

    with span("a-node"):
        result = _body()

    assert result == "the block ran"
    # Premise: the mark really was tried, so the swallow is what kept it quiet.
    assert client.mark_calls == 1
    assert client.closed == ["a-node"]


def test_the_sdk_span_still_offers_the_public_mark() -> None:
    """Catches an SDK upgrade that renames the public-mark method or the
    `langfuse.trace.public` attribute: the marks would then go nowhere and
    every trace would turn private without any test failing."""
    assert LangfuseOtelSpanAttributes.TRACE_PUBLIC == "langfuse.trace.public"
    assert hasattr(Langfuse, "set_current_trace_as_public")


def test_env_example_names_public_trace_langfuse_with_no_value() -> None:
    """Catches a new setting missing from `.env.example` (the list an operator
    fills when going live) or listed with a value in it."""
    example = Path(__file__).resolve().parents[3] / ".env.example"

    assert f"{_PUBLIC_ENV}=" in example.read_text().splitlines()
