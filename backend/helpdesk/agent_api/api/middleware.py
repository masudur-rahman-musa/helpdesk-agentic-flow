"""Check the login token once per request, before any route runs.

Anything without a valid token gets the same 401, so a new route that forgets
its own check is still safe. Only `OPEN_PATHS`, the two eval paths while
they are switched on, and CORS preflights skip it.
Plain ASGI, so the claims it puts on `request.state` are what routes read.
It sits inside CORS (a 401 still carries CORS headers) and outside the
telemetry bind. The MCP server still decides what the caller may do.
"""

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from helpdesk.agent_api.core.deps import bearer, claims, unauthenticated
from helpdesk.shared.core.config import load_settings

OPEN_PATHS = frozenset({"/healthz", "/readyz", "/metrics", "/auth/login"})
"""The paths that skip the check. Matched exactly, never by prefix, so
`/metricsX` is still turned away."""


EVAL_OPEN_PATHS = frozenset({"/admin/test/reset", "/v1/eval/turn"})
"""The two routes the companion console calls with no token. Open only while
`EVAL_ROUTES_ENABLED` is true, and matched exactly like the paths above."""


class RequireAuthMiddleware:
    """Turn away every request with no valid token, before any route runs."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope["method"] == "OPTIONS" or scope["path"] in OPEN_PATHS:
            await self.app(scope, receive, send)
            return
        settings = load_settings()
        if scope["path"] in EVAL_OPEN_PATHS and settings.eval_routes_enabled:
            await self.app(scope, receive, send)
            return

        token = bearer(Headers(scope=scope).get("authorization"))
        verified = claims(token, settings.jwt_secret)
        if token is None or verified is None:
            await unauthenticated()(scope, receive, send)
            return

        state = scope.setdefault("state", {})
        state["auth_bearer"] = token
        state["auth_claims"] = verified
        await self.app(scope, receive, send)
