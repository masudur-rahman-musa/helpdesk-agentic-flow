"""The one context every MCP tool handler is handed.

`dispatch` builds one `ToolContext` per call and passes it to the handler, so a
tool reaches its session, the caller's identity, the directory, the call's
clock reading, the settings and the clients from one place and builds none of
its own. A tool is then easy to test with fakes: the one context is the only
thing a test has to supply, rather than a real directory, a real clock and a
real client each.

`now` is the single `datetime.now(UTC)` `dispatch` captured for this call and
reused for the identity check, this context and the audit stamp, the way the
scheduler threads `now=` from the edge: no tool body reads the clock, so a
call answers for one instant from end to end.

`ToolClients` keeps the 68a accessors and holds no client of its own: each
accessor is a thin pass to the per-loop machinery (`rag_clients`,
`helpdesk.shared.core.redis`) that owns the pooling and the timeouts, so a test
that stubs one of those accessors drives the handler unchanged. What an earlier change
adds is the app's two hooks: the mcp-server lifespan opens the three pooled
clients once at startup (`open_clients`) and closes them at shutdown
(`aclose`), so a client is created and closed with the app rather than built
lazily on the first call and never closed. The embedder is never opened at
startup and stays the lazy per-process one, which
`test_mcp_client_lifecycle.py::test_the_embedder_is_not_opened_at_startup` pins.
"""

from dataclasses import dataclass
from datetime import datetime

from qdrant_client import AsyncQdrantClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.mcp_server.core import rag_clients
from helpdesk.mcp_server.core.identity import Identity
from helpdesk.mcp_server.services.rag.embedding import Embedder
from helpdesk.mcp_server.services.rag.redaction_client import RedactionClient
from helpdesk.shared.core.config import Settings
from helpdesk.shared.core.redis import close_loop_clients as close_redis_loop_clients
from helpdesk.shared.core.redis import get_redis
from helpdesk.shared.services.access.directory import DirectoryProvider


@dataclass(frozen=True)
class ToolClients:
    """The process-pooled clients a tool may reach, each through its one pool.

    One accessor per client, each a thin pass to the module that owns the
    pooling and the timeouts (`rag_clients`, `helpdesk.shared.core.redis`), so
    the two lifetimes an earlier change fixed — Qdrant and the redactor per loop, the
    embedder per process — are unchanged, and a test that stubs an accessor
    drives the handler through its stub. `open_clients` and `aclose` are the
    app's hooks: the lifespan opens the pooled clients once at
    startup and closes them at shutdown, so a client is created and closed with
    the app. Lazy on purpose: a handler that never calls `embedder()` loads no
    ONNX model, pinned by
    `test_mcp_tool_context.py::test_the_embedder_is_not_built_for_a_tool_that_never_asks_for_it`.
    """

    settings: Settings

    @classmethod
    def open_clients(cls, settings: Settings) -> "ToolClients":
        """Open Redis, Qdrant and the redactor once, with the app.

        The three pooled accessors are called here so their clients are created
        when the server starts rather than on the first tool call; each caches
        the one per-loop client it already hands out, so this warms the very
        clients a handler then reads. The embedder is deliberately NOT opened —
        loading its ONNX models at every boot is waste, and an earlier change pins that
        laziness (`::test_the_embedder_is_not_opened_at_startup`). The returned
        copy carries only the settings; dispatch hands it to every handler, so
        a call under the server builds no client of its own. Pinned by
        `test_mcp_client_lifecycle.py::test_the_lifespan_opens_the_three_clients_and_closes_them`.
        """
        get_redis(settings)
        rag_clients.qdrant(settings)
        rag_clients.redactor(settings)
        return cls(settings)

    async def aclose(self) -> None:
        """Close the running loop's pooled clients, at shutdown.

        Closed through the same loop-close helpers `dispatch.close_shared_connections`
        uses, so the loop's one Redis, Qdrant and redactor are shut rather than
        left to leak for the life of the process (an earlier change's first risk, here
        for the mcp-server). The embedder holds no connection and is left be.
        Pinned by
        `test_mcp_client_lifecycle.py::test_the_lifespan_opens_the_three_clients_and_closes_them`.
        """
        await rag_clients.close_loop_clients()
        await close_redis_loop_clients()

    def redis(self) -> Redis:
        """The running loop's pooled Redis client."""
        return get_redis(self.settings)

    def qdrant(self) -> AsyncQdrantClient:
        """The running loop's pooled Qdrant client."""
        return rag_clients.qdrant(self.settings)

    def redactor(self) -> RedactionClient:
        """The running loop's pooled redaction-gateway client."""
        return rag_clients.redactor(self.settings)

    def embedder(self) -> Embedder:
        """The process's one embedder, built on first use."""
        return rag_clients.embedder()


@dataclass(frozen=True)
class ToolContext:
    """Everything a tool handler needs for one call, built once in `dispatch`.

    Frozen: a handler reads what it was handed and changes none of it. The
    directory is `SimulatedDirectory(session)` built over the same bound
    session, so a tool never builds its own, and `now` is the call's one
    instant.
    """

    session: AsyncSession
    identity: Identity
    directory: DirectoryProvider
    now: datetime
    settings: Settings
    clients: ToolClients


__all__ = ["ToolClients", "ToolContext"]
