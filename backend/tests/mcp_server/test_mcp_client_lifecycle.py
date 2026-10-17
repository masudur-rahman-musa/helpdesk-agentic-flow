"""The MCP tools' clients are made and closed with the app.

Surface under test (written before it exists):

``mcp_server.tools.context.ToolClients``
    gains ``open_clients(settings)`` (a sync classmethod that warms the three
    pooled clients for the loop and returns a ``ToolClients``) and
    ``async def aclose()`` (closing the loop's pooled clients). It holds no
    client of its own; its accessors still delegate to the per-loop machinery,
    so a real-transport test that stubs ``rag_clients.qdrant`` drives the
    handler unchanged. These cases fake the BUILDERS those accessors use, so
    the loop caches the fakes and the close at shutdown is observable. Every
    one here is a fake; no real service is touched.

``mcp_server.main.create_app`` — its lifespan opens one ``ToolClients`` and
puts it on the MOUNTED transport app's state (``mcp_app.state.tool_clients``),
and closes it at shutdown. The embedder is NOT opened at startup; it stays
built on first use (the laziness).

``mcp_server.tools.dispatch.dispatch`` — reads the one copy off
``request.scope["app"].state.tool_clients``; with no app carrying one it builds
``ToolClients(settings)`` the old way, so stdio and the tests keep working.

No pytest-asyncio: each case is a sync function driving one ``asyncio.run``.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from pydantic import BaseModel
from starlette.routing import Mount

from helpdesk.mcp_server.core import rag_clients
from helpdesk.mcp_server.main import create_app
from helpdesk.mcp_server.tools import dispatch as pipeline
from helpdesk.mcp_server.tools.context import ToolClients, ToolContext
from helpdesk.mcp_server.tools.dispatch import dispatch
from helpdesk.mcp_server.tools.registry import ToolSpec
from helpdesk.shared.auth import rbac
from helpdesk.shared.core import redis as redis_module
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

# The one tool the synthetic specs authorise, as in the context/pipeline tests.
SPEC_TOOL = rbac.Tool.GET_ME


class NoArgs(BaseModel):
    pass


class Echo(BaseModel):
    ok: bool


class _Capture:
    """A handler that records the one context it was handed."""

    def __init__(self) -> None:
        self.ctx: ToolContext | None = None

    async def __call__(self, ctx: ToolContext, args: Any) -> Echo:
        self.ctx = ctx
        return Echo(ok=True)


def _spec(
    handler: Callable[[ToolContext, Any], Awaitable[Echo]],
) -> ToolSpec[Any, Any]:
    """A synthetic registry entry, so dispatch can be tested apart from a tool."""
    return ToolSpec(
        name=SPEC_TOOL.value,
        tool=SPEC_TOOL,
        description="synthetic spec, tests only",
        input_model=NoArgs,
        output_model=Echo,
        event_type="tool.succeeded",
        mutating=False,
        handler=handler,
    )


# --- fakes for the three opened clients ---------------------------------------
#
# Each records that it was opened and whether it was closed, so a lifespan that
# opens but never closes (or never opens) is visible without a real service.


class _FakeRedis:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class _FakeQdrant:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _FakeRedactor:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


def _fake_the_three_builders(monkeypatch: Any) -> dict[str, Any]:
    """Swap the builders the pooled accessors use, so the loop caches the fakes.

    `open_clients` warms the clients through `get_redis`, `rag_clients.qdrant`
    and `rag_clients.redactor`, each of which caches the one client it builds
    for the loop; `aclose` then closes whatever those loop caches hold. Faking
    the builders (not the accessors) lets the fakes land in those caches, so
    the close at shutdown is observable. No real Redis, Qdrant or gateway is
    built, so startup touches no service.
    """
    redis = _FakeRedis()
    qdrant = _FakeQdrant()
    redactor = _FakeRedactor()

    class _RedisBuilder:
        @staticmethod
        def from_url(*args: Any, **kwargs: Any) -> _FakeRedis:
            return redis

    class _RedactorBuilder:
        @staticmethod
        def from_settings(settings: Any) -> _FakeRedactor:
            return redactor

    monkeypatch.setattr(redis_module, "Redis", _RedisBuilder)
    monkeypatch.setattr(rag_clients, "create_client", lambda settings: qdrant)
    monkeypatch.setattr(rag_clients, "RedactionClient", _RedactorBuilder)
    return {"redis": redis, "qdrant": qdrant, "redactor": redactor}


def _mounted_app(app: Any) -> Any:
    """The sub-app of the `Mount` route at `/`, where the lifespan stores the copy."""
    return next(r.app for r in app.routes if isinstance(r, Mount))


def test_the_lifespan_opens_the_three_clients_and_closes_them(
    monkeypatch: Any,
) -> None:
    """Catches a lifespan that never opens the one copy (dispatch would then
    build a client per call) or opens it and never closes it (the three pooled
    connections leak for the life of the process): entering the lifespan must
    leave an open Redis, Qdrant and redactor on the mounted app's
    `tool_clients`, and leaving it must close all three."""
    fakes = _fake_the_three_builders(monkeypatch)

    async def scenario() -> None:
        app = create_app()
        mcp_app = _mounted_app(app)
        async with app.router.lifespan_context(app):
            clients = mcp_app.state.tool_clients
            assert isinstance(clients, ToolClients)
            # The three are open and none closed yet inside the lifespan.
            assert fakes["redis"].closed is False
            assert fakes["qdrant"].closed is False
            assert fakes["redactor"].closed is False
        # Leaving the lifespan closed every one.
        assert fakes["redis"].closed is True
        assert fakes["qdrant"].closed is True
        assert fakes["redactor"].closed is True

    asyncio.run(scenario())


def test_the_embedder_is_not_opened_at_startup(
    monkeypatch: Any,
) -> None:
    """Catches a lifespan that eagerly loads the ONNX embedder at boot (the
    embedder stays built on first use only): entering and
    leaving the lifespan must load no model, so the model constructor is called
    zero times. The three real clients are faked so startup touches no service."""
    _fake_the_three_builders(monkeypatch)
    loads = 0

    def counting_build() -> Any:
        nonlocal loads
        loads += 1
        raise AssertionError("the embedder must not be built at startup")

    monkeypatch.setattr(rag_clients, "_build_embedder", counting_build)
    monkeypatch.setattr(rag_clients, "_EMBEDDER", None)

    async def scenario() -> None:
        app = create_app()
        async with app.router.lifespan_context(app):
            pass

    asyncio.run(scenario())
    assert loads == 0


class _FakeApp:
    """A stand-in app carrying exactly a `state.tool_clients`, nothing else."""

    def __init__(self, tool_clients: ToolClients) -> None:
        self.state = type("_State", (), {"tool_clients": tool_clients})()


def _request_with_app(headers: Mapping[str, str], app: Any) -> Any:
    """A Starlette request whose scope carries this app under `"app"`."""
    from starlette.requests import Request

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
        "app": app,
    }
    return Request(scope)


def _bare_request(headers: Mapping[str, str]) -> Any:
    """A Starlette request with NO `"app"` in its scope (the stdio/test path)."""
    from starlette.requests import Request

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


def test_dispatch_hands_the_handler_the_apps_one_copy(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches a dispatch that builds its own `ToolClients(settings)` on every
    call instead of reading the one copy off the request's app: with a sentinel
    `ToolClients` on `scope["app"].state.tool_clients`, the handler's
    `ctx.clients` must BE that sentinel, so nothing under the app built one."""
    sentinel = ToolClients(mcp_env_settings())
    capture = _Capture()
    token = token_for(mcp_env.tenant_a)

    async def scenario() -> Any:
        try:
            return await dispatch(
                _spec(capture),
                _request_with_app(bearer(token), _FakeApp(sentinel)),
                {},
            )
        finally:
            await pipeline.close_shared_connections()

    result = asyncio.run(scenario())
    assert result.is_error is False, result.content
    assert capture.ctx is not None
    assert capture.ctx.clients is sentinel


def test_dispatch_falls_back_when_no_app_carries_a_copy(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches a dispatch that assumes an app is always present and crashes (or
    hands `None`) on the stdio/test path where the scope has no `"app"`: with no
    app, the handler must still get a `ToolContext` whose `ctx.clients` is a
    freshly built `ToolClients`, so stdio and the tests keep working."""
    capture = _Capture()
    token = token_for(mcp_env.tenant_a)

    async def scenario() -> Any:
        try:
            return await dispatch(_spec(capture), _bare_request(bearer(token)), {})
        finally:
            await pipeline.close_shared_connections()

    result = asyncio.run(scenario())
    assert result.is_error is False, result.content
    assert capture.ctx is not None
    assert isinstance(capture.ctx.clients, ToolClients)


def mcp_env_settings() -> Any:
    """The settings the server reads under `mcp_env` (its env is already set)."""
    from helpdesk.shared.core.config import load_settings

    return load_settings()
