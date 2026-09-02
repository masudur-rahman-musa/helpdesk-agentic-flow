"""MCP server service. Only the health endpoint until row 10."""

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="mcp-server")


class HealthResponse(BaseModel):
    status: str


@app.get("/healthz")
async def healthz() -> HealthResponse:
    return HealthResponse(status="ok")
