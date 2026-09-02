"""Dependency reachability checks for /readyz.

Each check answers one question and never raises; the route maps False to
503. Connections are opened per call: readiness must probe the dependency
now, not report on a pooled connection from before it went down.
A gateway check is added at row 16.
"""

from helpdesk.core.settings import load_settings
from qdrant_client import AsyncQdrantClient
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


async def check_postgres() -> bool:
    engine = create_async_engine(load_settings().database_url)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
    finally:
        await engine.dispose()


async def check_redis() -> bool:
    client = Redis.from_url(load_settings().redis_url)
    try:
        await client.ping()
        return True
    except Exception:
        return False
    finally:
        await client.aclose()


async def check_qdrant() -> bool:
    client = AsyncQdrantClient(url=load_settings().qdrant_url, timeout=3)
    try:
        await client.get_collections()
        return True
    except Exception:
        return False
    finally:
        await client.close()
