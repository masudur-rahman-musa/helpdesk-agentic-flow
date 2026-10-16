"""Verify the login token once, for every request, before any route runs.

A big public app checks who is knocking at the door, not at each room. This
plain-ASGI middleware does exactly that: it verifies the bearer (signature and
expiry) once per request and turns away anything without a valid token with the
same literal 401 `{"code": "FORBIDDEN"}` absent, malformed, forged and expired
all get. So a new route that forgets its own check is still safe, because
nothing unauthenticated reaches it.

Only a short open list skips the check: `/healthz`, `/readyz`, `/metrics` and
`/auth/login` (`OPEN_PATHS`), matched EXACTLY, so `/metricsX` and
`/auth/login/x` are still turned away. A CORS preflight (`OPTIONS`) skips it too,
since a browser sends it with no token.

A plain ASGI middleware rather than `BaseHTTPMiddleware`, and the same shape as
`TelemetryContextMiddleware`: it runs the downstream app in the SAME task, so
the verified claims and the raw bearer it puts on `request.state` are what the
routes read. Wired between telemetry and CORS in `router.py`: inside CORS, so a
preflight is still answered by CORS and a blocked request still carries CORS
headers; outside the telemetry bind, so a blocked request is turned away before
any context is bound.

Routes read identity from `request.state` through `core.deps.request_claims`
and the raw bearer through `core.deps.request_bearer`; they never re-read the
header. The MCP server, under the forwarded bearer, remains the
only place that decides what the caller may do.

Pinned by `test_auth_middleware.py`: the no-bearer, forged and expired cases
fail if the verify is deleted; the preflight case fails if this is ordered
outside CORS; the open-list case fixes the four paths as an exact set.
"""

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from helpdesk.agent_api.core.deps import bearer, claims, unauthenticated
from helpdesk.shared.core.config import load_settings

OPEN_PATHS = frozenset({"/healthz", "/readyz", "/metrics", "/auth/login"})
"""The exact paths that skip the token check: the two probes, the metrics scrape
and login itself. Matched by equality and never by prefix, so a path that only
looks open is still turned away. Pinned as this exact set by
`test_auth_middleware.py::test_the_open_list_is_exactly_health_ready_metrics_and_login`."""


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

        token = bearer(Headers(scope=scope).get("authorization"))
        verified = claims(token, load_settings().jwt_secret)
        if token is None or verified is None:
            await unauthenticated()(scope, receive, send)
            return

        state = scope.setdefault("state", {})
        state["auth_bearer"] = token
        state["auth_claims"] = verified
        await self.app(scope, receive, send)
