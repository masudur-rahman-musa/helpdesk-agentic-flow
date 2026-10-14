"""The Qdrant client, the redactor and the embedder this service's RAG tools reach for.

The same home and the same reason as `redis_client.py`:
a handler cannot import `pipeline` for a process-level client, because
`pipeline` imports `registry`, which imports the handler. So the accessors
live here and the handler imports them.

**Two different lifetimes, deliberately**. The Qdrant
and redaction clients hold pooled connections, so each belongs to the event
loop that opened them and is keyed by `(loop, url)` exactly as `_redis` is; a
client cached per process
would hand a later loop a pool belonging to a dead one. The embedder holds no
connection and no loop affinity, only two loaded ONNX sessions, so it is
cached once per PROCESS — rebuilding it per loop would pay a model load on
every request. Getting the two backwards is the risk plan §5 names, and both
halves are pinned by
`test_mcp_search_policy.py::test_the_qdrant_client_is_one_per_loop_and_the_embedder_one_per_process`,
which counts constructor calls across two `asyncio.run` scenarios rather than
comparing object identity alone.

All three accessors are read through the module attribute by their caller
(`rag_clients.qdrant(...)`, `rag_clients.redactor(...)`,
`rag_clients.embedder()`), the way `main.py` reads `readiness.check_*`, so a
test can substitute any of them without a real store, a real gateway or a
real model.
"""

import asyncio
from weakref import WeakKeyDictionary

from qdrant_client import AsyncQdrantClient

from helpdesk.shared.core.settings import Settings, load_settings
from helpdesk.shared.rag.embedding import Embedder, FastEmbedEmbedder
from helpdesk.shared.rag.gateway import RedactionClient
from helpdesk.shared.rag.vectors import create_client

_SHARED_QDRANT: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, AsyncQdrantClient]] = (
    WeakKeyDictionary()
)

_SHARED_REDACTORS: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, RedactionClient]] = (
    WeakKeyDictionary()
)

_EMBEDDER: Embedder | None = None


def qdrant(settings: Settings) -> AsyncQdrantClient:
    """A process-shared Qdrant client for the running event loop.

    Built through `vectors.create_client`, never here: that is the one place
    the timeout is passed, and a client constructed
    in this module would carry the library's own default and turn an
    unreachable store into a request that never returns.

    Keyed by (event loop, URL) for the same reason `redis_client._redis` is.
    Entries drop with their loop.
    """
    loop = asyncio.get_running_loop()
    clients = _SHARED_QDRANT.setdefault(loop, {})
    client = clients.get(settings.qdrant_url)
    if client is None:
        client = create_client(settings)
        clients[settings.qdrant_url] = client
    return client


def redactor(settings: Settings) -> RedactionClient:
    """A process-shared redaction gateway client for the running event loop.

    Pooled beside `qdrant` and for the same reason: the
    client owns an `httpx.AsyncClient`, so building one per call would pay a
    new connection pool and a new TLS handshake on every re-ingest, and the
    pool it holds belongs to the loop that opened it.

    Built through `RedactionClient.from_settings`, never here, so the connect
    and read halves are the ones an earlier change pinned on the client itself rather than httpx's own defaults.

    Keyed by (event loop, gateway URL), the same key `qdrant` uses. Entries
    drop with their loop, and `close_loop_clients` closes them.
    """
    loop = asyncio.get_running_loop()
    clients = _SHARED_REDACTORS.setdefault(loop, {})
    client = clients.get(settings.gateway_url)
    if client is None:
        client = RedactionClient.from_settings(settings)
        clients[settings.gateway_url] = client
    return client


def _build_embedder() -> Embedder:
    """The uncached constructor, and the seam a test replaces.

    Separate from `embedder` so a test can count how often the models were
    actually loaded without paying an ONNX load itself; `embedder` reads this
    name through the module, so substituting it is enough.
    """
    return FastEmbedEmbedder(load_settings().embedding_cache_dir)


def embedder() -> Embedder:
    """The process's one embedder, built on first use.

    Cached per process rather than per loop: the models hold no connection, so
    the only thing a second instance buys is a second ONNX session load. Built
    lazily rather than at import so a service that never searches — and every
    test that fakes this accessor — never loads a model at all.
    """
    global _EMBEDDER
    if _EMBEDDER is None:
        _EMBEDDER = _build_embedder()
    return _EMBEDDER


async def close_loop_clients() -> None:
    """Close and forget the running loop's pooled Qdrant and gateway clients.

    Both pools are this loop's, so both are closed here; a redactor left open
    would keep its httpx pool alive past the loop that opened it. The
    embedder is deliberately not touched: it is not this loop's, and closing
    it would mean reloading the models for the next one.
    """
    loop = asyncio.get_running_loop()
    for client in _SHARED_QDRANT.pop(loop, {}).values():
        await client.close()
    for redaction_client in _SHARED_REDACTORS.pop(loop, {}).values():
        await redaction_client.aclose()


__all__ = ["close_loop_clients", "embedder", "qdrant", "redactor"]
