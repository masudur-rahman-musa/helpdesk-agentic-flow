"""The Qdrant client, redactor and embedder the RAG tools use.

Kept apart from the pipeline to avoid an import loop. The Qdrant and redaction
clients hold connection pools, so each is kept per event loop; a pool from a
dead loop would break. The embedder holds no connection, only loaded models,
so it is kept once per process to avoid reloading them.
"""

import asyncio
from weakref import WeakKeyDictionary

from qdrant_client import AsyncQdrantClient

from helpdesk.mcp_server.services.rag.embedding import Embedder, FastEmbedEmbedder
from helpdesk.mcp_server.services.rag.redaction_client import RedactionClient
from helpdesk.mcp_server.services.rag.vectors import create_client
from helpdesk.shared.core.config import Settings, load_settings

_SHARED_QDRANT: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, AsyncQdrantClient]] = (
    WeakKeyDictionary()
)

_SHARED_REDACTORS: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, RedactionClient]] = (
    WeakKeyDictionary()
)

_EMBEDDER: Embedder | None = None


def qdrant(settings: Settings) -> AsyncQdrantClient:
    """The shared Qdrant client for the running event loop.

    Built by `vectors.create_client`, which sets the timeout, so a dead store
    fails instead of hanging.
    """
    loop = asyncio.get_running_loop()
    clients = _SHARED_QDRANT.setdefault(loop, {})
    client = clients.get(settings.qdrant_url)
    if client is None:
        client = create_client(settings)
        clients[settings.qdrant_url] = client
    return client


def redactor(settings: Settings) -> RedactionClient:
    """The shared redaction gateway client for the running event loop.

    Pooled so each re-ingest does not pay for a new connection and TLS handshake.
    """
    loop = asyncio.get_running_loop()
    clients = _SHARED_REDACTORS.setdefault(loop, {})
    client = clients.get(settings.gateway_url)
    if client is None:
        client = RedactionClient.from_settings(settings)
        clients[settings.gateway_url] = client
    return client


def _build_embedder() -> Embedder:
    """Build the embedder. A separate name so tests can count model loads."""
    return FastEmbedEmbedder(load_settings().embedding_cache_dir)


def embedder() -> Embedder:
    """The one embedder, built on first use, so code that never searches loads no model."""
    global _EMBEDDER
    if _EMBEDDER is None:
        _EMBEDDER = _build_embedder()
    return _EMBEDDER


async def close_loop_clients() -> None:
    """Close and forget the running loop's Qdrant and gateway clients.

    The embedder is left alone; it belongs to the process, not the loop.
    """
    loop = asyncio.get_running_loop()
    for client in _SHARED_QDRANT.pop(loop, {}).values():
        await client.close()
    for redaction_client in _SHARED_REDACTORS.pop(loop, {}).values():
        await redaction_client.aclose()


__all__ = ["close_loop_clients", "embedder", "qdrant", "redactor"]
