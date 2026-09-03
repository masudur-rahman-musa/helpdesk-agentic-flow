"""Agent API service. Health, login, audit verify; sessions land at row 19."""

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from agent_api import readiness
from agent_api.admin_routes import router as admin_router
from agent_api.auth_routes import router as auth_router

app = FastAPI(title="agent-api")
app.include_router(auth_router)
app.include_router(admin_router)


class HealthResponse(BaseModel):
    status: str


class ReadinessResponse(BaseModel):
    status: str
    checks: dict[str, bool]


@app.get("/healthz")
async def healthz() -> HealthResponse:
    return HealthResponse(status="ok")


@app.get("/readyz", response_model=ReadinessResponse)
async def readyz() -> JSONResponse:
    # Called via the module attribute so tests can fake individual checks.
    checks = {
        "postgres": await readiness.check_postgres(),
        "redis": await readiness.check_redis(),
        "qdrant": await readiness.check_qdrant(),
    }
    ready = all(checks.values())
    body = ReadinessResponse(status="ready" if ready else "not_ready", checks=checks)
    return JSONResponse(status_code=200 if ready else 503, content=body.model_dump())
