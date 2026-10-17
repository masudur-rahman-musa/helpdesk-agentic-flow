"""The one context every MCP tool handler is handed, built per call by
`dispatch`, so tools build no session, clock or client of their own."""

from dataclasses import dataclass
from datetime import datetime

from qdrant_client import AsyncQdrantClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.mcp_server.core import rag_clients
from helpdesk.mcp_server.core.identity import Identity
from helpdesk.mcp_server.services.rag.embedding import Embedder
from helpdesk.mcp_server.services.rag.redaction_client import RedactionClient
from helpdesk.shared.access.directory import DirectoryProvider
from helpdesk.shared.core.config import Settings
from helpdesk.shared.core.redis import close_loop_clients as close_redis_loop_clients
from helpdesk.shared.core.redis import get_redis


@dataclass(frozen=True)
class ToolClients:
    """The process-pooled clients a tool may reach. The embedder is lazy, so
    a tool that never asks for it loads no model."""

    settings: Settings

    @classmethod
    def open_clients(cls, settings: Settings) -> "ToolClients":
        """Open Redis, Qdrant and the redactor once, when the app starts."""
        get_redis(settings)
        rag_clients.qdrant(settings)
        rag_clients.redactor(settings)
        return cls(settings)

    async def aclose(self) -> None:
        """Close the running loop's pooled clients at shutdown."""
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
    """Everything a tool handler needs for one call, built once in `dispatch`."""

    session: AsyncSession
    identity: Identity
    directory: DirectoryProvider
    now: datetime
    settings: Settings
    clients: ToolClients


__all__ = ["ToolClients", "ToolContext"]
