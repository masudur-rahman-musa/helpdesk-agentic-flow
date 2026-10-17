"""Tests for mcp-server health endpoint.

The health routes come from the shared registrar; the /healthz test checks
that the response shape stays the same.

API under test:
    mcp_server.main.app -- FastAPI application

Response shape:
    GET /healthz -> 200, {"status": "ok"}   (no dependency checks)
"""

import asyncio

import httpx

from helpdesk.mcp_server.main import app


def _get(path: str) -> httpx.Response:
    async def _run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(path)

    return asyncio.run(_run())


def test_healthz_returns_200() -> None:
    response = _get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
