"""The MCP tool proxy every signed-in read route goes through."""

from collections.abc import Callable, Mapping
from typing import Any

from fastapi import Request
from pydantic import BaseModel

from helpdesk.agent_api.core.deps import request_bearer, request_claims
from helpdesk.agent_api.services.mcp_client import McpToolCaller, ToolCaller, TracingToolCaller
from helpdesk.shared.auth.jwt import TokenClaims
from helpdesk.shared.core.config import Settings, load_settings


def build_tool_caller(settings: Settings, bearer_token: str) -> ToolCaller:
    """The request's MCP caller, built per request since a session lasts one turn.

    Forwards the caller's own token, and wraps each call in a tracing span.
    """
    return TracingToolCaller(McpToolCaller(url=settings.mcp_url, bearer_token=bearer_token))


async def proxy_tool(
    request: Request,
    tool: str,
    arguments: BaseModel,
    extend: Callable[[TokenClaims], Mapping[str, Any]] | None = None,
) -> Mapping[str, Any]:
    """Call one tool with the caller's own token and return its body.

    A refusal is raised as its typed code. `extend` adds keys worked out from the
    verified claims, on success only. `build_tool_caller` is looked up on this
    module so tests can swap it.
    """
    token = request_bearer(request)
    verified_claims = request_claims(request)

    async with build_tool_caller(load_settings(), token) as tools:
        payload: Mapping[str, Any] = await tools.call_tool(tool, arguments)
    body = dict(payload)
    if extend is not None:
        body.update(extend(verified_claims))
    return body
