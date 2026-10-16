"""An earlier change: one context built in dispatch, handed to every tool.

Surface under test (written before it exists):

``mcp_server.tools.context``
    ``ToolClients`` — a frozen dataclass wrapping ``settings`` with lazy
    accessors ``redis()``, ``qdrant()``, ``redactor()`` and ``embedder()``.
    ``ToolContext`` — a frozen dataclass carrying ``session``, ``identity``,
    ``directory``, ``now``, ``settings`` and ``clients``.

``mcp_server.tools.registry.ToolSpec.handler`` — now
``Callable[[ToolContext, I], Awaitable[O]]``: a handler takes the one context
and the validated arguments, nothing else. ``dispatch`` builds the context
once per call and hands it down.

Seams these tests rely on (the implementation honors them):
- dispatch imports ``SimulatedDirectory`` at module level and builds the
  directory as ``SimulatedDirectory(session)``, read through the module
  attribute, so a fake can be swapped in.
- dispatch captures ``now`` with the module-level ``datetime`` exactly once and
  reuses that one instant for identity, ``ctx.now`` and the audit stamp.
- ``ctx.clients.embedder()`` loads a model only when a handler asks for it; the
  load goes through ``rag_clients._build_embedder``.

No pytest-asyncio: each case is a sync function driving one ``asyncio.run``
through the pipeline test's ``_dispatch`` helper shape.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from helpdesk.mcp_server.core import rag_clients
from helpdesk.mcp_server.tools import dispatch as pipeline
from helpdesk.mcp_server.tools.context import ToolContext
from helpdesk.mcp_server.tools.dispatch import dispatch
from helpdesk.mcp_server.tools.registry import REGISTRY, ToolSpec
from helpdesk.shared.auth import rbac
from tests.support.mcp import (  # noqa: F401 - fixtures are resolved by name
    SeededMcp,
    bearer,
    mcp_env,
    mcp_pg,
    mcp_redis_base_url,
    mcp_redis_url,
    seeded_mcp_db,
    token_for,
)

# The one tool the synthetic specs authorise, as in the pipeline test.
SPEC_TOOL = rbac.Tool.GET_ME

# A fixed instant a fake clock can hand back, far from any real "now" so a test
# that reads the real clock instead of the context is visibly wrong.
FIXED_NOW = datetime(2031, 3, 14, 15, 9, 26, tzinfo=UTC)


class NoArgs(BaseModel):
    pass


class Echo(BaseModel):
    ok: bool


class _Capture:
    """A handler that records the one context it was handed.

    The synthetic specs mirror the pipeline test's ``_ok_handler``/``_spec``,
    but with the new ``(ctx, args)`` signature: the whole point of an earlier change is
    that a handler receives a ``ToolContext`` and nothing else beside the args.
    """

    def __init__(self) -> None:
        self.ctx: ToolContext | None = None

    async def __call__(self, ctx: ToolContext, args: Any) -> Echo:
        self.ctx = ctx
        return Echo(ok=True)


def _spec(
    handler: Callable[[ToolContext, Any], Awaitable[Echo]],
    *,
    input_model: type[BaseModel] = NoArgs,
    event_type: str = "tool.succeeded",
) -> ToolSpec[Any, Any]:
    """A synthetic registry entry, so the pipeline can be tested apart from a tool."""
    return ToolSpec(
        name=SPEC_TOOL.value,
        tool=SPEC_TOOL,
        description="synthetic spec, tests only",
        input_model=input_model,
        output_model=Echo,
        event_type=event_type,
        mutating=False,
        handler=handler,
    )


def _request(headers: Mapping[str, str]) -> Request:
    """A Starlette request carrying exactly these headers, no server needed."""
    scope: dict[str, Any] = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/mcp",
        "raw_path": b"/mcp",
        "query_string": b"",
        "root_path": "",
        "headers": [(name.lower().encode(), value.encode()) for name, value in headers.items()],
        "client": ("127.0.0.1", 54321),
        "server": ("127.0.0.1", 8000),
    }
    return Request(scope)


def _dispatch(
    spec: ToolSpec[Any, Any],
    headers: Mapping[str, str],
    arguments: Mapping[str, Any] | None = None,
) -> Any:
    async def scenario() -> Any:
        try:
            return await dispatch(spec, _request(headers), dict(arguments or {}))
        finally:
            await pipeline.close_shared_connections()

    return asyncio.run(scenario())


class _FakeClock:
    """A `datetime` stand-in whose `now(tz)` is fixed and counts its reads.

    Only `now` is overridden; everything else proxies to the real `datetime`,
    so a module that uses `datetime.now(UTC)` for the clock but `datetime` for
    other things still works. Counting the reads is how the "one capture
    reused" claim is pinned: a fresh read per use would count more than one.
    """

    def __init__(self, fixed: datetime) -> None:
        self._fixed = fixed
        self.now_calls = 0

    def now(self, tz: Any = None) -> datetime:
        self.now_calls += 1
        return self._fixed

    def __getattr__(self, name: str) -> Any:
        return getattr(datetime, name)


def test_dispatch_builds_one_context_and_hands_it_to_the_handler(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches a dispatch that still calls `handler(session, identity, args)`
    instead of building a `ToolContext`: the handler must receive one context
    carrying this call's session, identity, directory, clock, settings and
    clients, not three loose positional arguments."""
    capture = _Capture()
    token = token_for(mcp_env.tenant_a)

    result = _dispatch(_spec(capture), bearer(token))

    assert result.is_error is False, result.content
    ctx = capture.ctx
    assert ctx is not None, "the handler was never handed a context"
    assert isinstance(ctx, ToolContext)
    assert isinstance(ctx.session, AsyncSession)
    assert ctx.identity.user_id == mcp_env.tenant_a.user_id
    assert ctx.identity.tenant_id == mcp_env.tenant_a.tenant_id
    assert ctx.directory is not None
    assert ctx.now is not None
    assert ctx.settings is not None
    assert ctx.clients is not None


def test_a_handler_reads_now_from_the_context(
    mcp_env: SeededMcp,  # noqa: F811
    monkeypatch: Any,
) -> None:
    """Catches a dispatch that reads the clock more than once (a fresh
    `datetime.now(UTC)` for identity, another for `ctx.now`, another for the
    audit stamp) instead of capturing one instant and threading it: the
    handler's `ctx.now` must be the captured instant AND the clock must be read
    exactly once."""
    clock = _FakeClock(FIXED_NOW)
    monkeypatch.setattr(pipeline, "datetime", clock)
    capture = _Capture()
    # The token is minted at the same instant the clock is pinned to, because
    # dispatch now checks identity against that one captured `now`: a token
    # issued at the real wall clock would read as expired at FIXED_NOW.
    token = token_for(mcp_env.tenant_a, now=FIXED_NOW)

    result = _dispatch(_spec(capture), bearer(token))

    assert result.is_error is False, result.content
    assert capture.ctx is not None
    assert capture.ctx.now == FIXED_NOW
    assert clock.now_calls == 1, (
        f"the clock was read {clock.now_calls} times; one capture must be reused "
        "for identity, ctx.now and the audit stamp"
    )


def test_a_handler_reads_the_directory_from_the_context(
    mcp_env: SeededMcp,  # noqa: F811
    monkeypatch: Any,
) -> None:
    """Catches a handler (or a dispatch) that builds its own
    `SimulatedDirectory(session)` instead of taking `ctx.directory`: with the
    constructor monkeypatched to a sentinel, the context's directory must BE
    that sentinel, so nothing downstream built one of its own."""
    sentinel = object()

    def fake_directory(session: Any) -> Any:
        return sentinel

    monkeypatch.setattr(pipeline, "SimulatedDirectory", fake_directory)
    capture = _Capture()
    token = token_for(mcp_env.tenant_a)

    result = _dispatch(_spec(capture), bearer(token))

    assert result.is_error is False, result.content
    assert capture.ctx is not None
    assert capture.ctx.directory is sentinel


def test_the_embedder_is_not_built_for_a_tool_that_never_asks_for_it(
    mcp_env: SeededMcp,  # noqa: F811
    monkeypatch: Any,
) -> None:
    """Catches a dispatch that eagerly builds every client when it assembles
    `ctx.clients`: a call whose handler never touches `ctx.clients.embedder()`
    must load no model, so the model constructor is called zero times."""
    loads = 0

    def counting_build() -> Any:
        nonlocal loads
        loads += 1
        raise AssertionError("the embedder must not be built for this call")

    monkeypatch.setattr(rag_clients, "_build_embedder", counting_build)
    monkeypatch.setattr(rag_clients, "_EMBEDDER", None)
    capture = _Capture()  # never calls ctx.clients.embedder()
    token = token_for(mcp_env.tenant_a)

    result = _dispatch(_spec(capture), bearer(token))

    assert result.is_error is False, result.content
    assert loads == 0


# --- the static scan: no tool module reaches for a dependency itself ----------

# The substrings that would mean a tool module built its own dependency instead
# of taking it from the context. A handler that reads settings, calls the real
# clock, builds a directory, reaches into `rag_clients`, or opens Redis has not
# been moved onto the context.
FORBIDDEN_MARKERS: tuple[str, ...] = (
    "load_settings",
    "datetime.now(",
    "SimulatedDirectory(",
    "rag_clients.",
    "get_redis(",
)


def _handler_module_files() -> list[Path]:
    """The `.py` file of every registered handler's module, de-duplicated."""
    modules = {spec.handler.__module__ for spec in REGISTRY.values()}
    paths: list[Path] = []
    for module_name in sorted(modules):
        module = __import__(module_name, fromlist=["__file__"])
        file = module.__file__
        assert file is not None, module_name
        paths.append(Path(file))
    return paths


def _first_marker_in(source: str) -> str | None:
    for marker in FORBIDDEN_MARKERS:
        if marker in source:
            return marker
    return None


def test_no_tool_module_reads_settings_the_clock_the_directory_or_a_client() -> None:
    """Catches a tool module that still reaches for a dependency on its own —
    reads settings, calls the real clock, builds a `SimulatedDirectory`, dips
    into `rag_clients`, or opens Redis — instead of taking everything from
    `ctx`. A planted hit proves the scan is not a silent no-op."""
    # The scan logic must flag a planted marker, or the whole test is vacuous.
    planted = "x = SimulatedDirectory(session)  # planted"
    assert _first_marker_in(planted) == "SimulatedDirectory("

    files = _handler_module_files()
    assert files, "no handler modules were found in the registry"
    offenders: dict[str, str] = {}
    for path in files:
        marker = _first_marker_in(path.read_text(encoding="utf-8"))
        if marker is not None:
            offenders[path.name] = marker
    assert offenders == {}, (
        f"these tool modules still reach for a dependency themselves: {offenders}"
    )
