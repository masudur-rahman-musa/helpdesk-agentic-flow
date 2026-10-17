"""The auth middleware checks the token once, for every request.

Surface under test (written before it exists; every case below is red on one
of these names until the implementation lands):

``helpdesk.agent_api.api.middleware`` (new module)
    ``RequireAuthMiddleware`` -- a plain-ASGI middleware the same shape as
    ``TelemetryContextMiddleware`` in ``helpdesk/agent_api/api/router.py``:
    it verifies the bearer (signature and expiry) once per request and turns
    away anything without a valid token with a literal 401 ``{"code":
    "FORBIDDEN"}``, before any route runs. Only the open paths and a CORS
    preflight skip it.
    ``OPEN_PATHS`` -- the set of exact paths that skip the check. This is the
    constant name chosen for the open-path set; the implementer must export it
    under exactly this name. It equals ``{"/healthz", "/readyz", "/metrics",
    "/auth/login"}``.

``helpdesk.agent_api.api.router.app``
    once the middleware is wired in between telemetry and CORS, a protected
    path with no valid token answers 401 before the route is reached, and a
    valid token reaches the route with its bearer and claims on
    ``request.state``.

Harness (copied from ``test_me_routes.py``): the app is driven over
``httpx.ASGITransport`` with NO lifespan entered, ``JWT_SECRET`` set per case,
and the scripted MCP tool caller patched at
``helpdesk.agent_api.services.tool_proxy.build_tool_caller`` so no socket opens.
All secrets and ids here are obviously fake, tests only.

Cost and isolation: no backing service. The lifespan is not
entered -- the one route a case here reaches (``/me/requests``) reads nothing
off ``app.state``; it only calls an MCP tool, which is the scripted seam.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from helpdesk.agent_api.api.middleware import OPEN_PATHS
from helpdesk.agent_api.api.router import app
from helpdesk.agent_api.services import tool_proxy as deps
from helpdesk.agent_api.services.mcp_client import ToolCaller
from helpdesk.shared.auth.jwt import issue_token
from helpdesk.shared.core.config import Settings
from helpdesk.shared.core.errors import ErrorCode

REQUESTS_PATH = "/me/requests"

# A check-free route added to the app at import of this module (below). It has
# NO token check of its own, so the ONLY thing that can refuse a request to it
# is the middleware under test. Every "must be 401" case drives this path so
# the middleware is the sole decider: the `/me/requests`
# route still carries its own `authenticated()` check today, and a case driving
# it would go green on that neighbouring guard whether or not the middleware is
# mounted -- exactly the vacuous test the project forbids. Once the change strips the
# route's own check and the middleware is wired in, both this path and
# `/me/requests` answer 401 from the one middleware.
CHECK_FREE_PATH = "/__plan64_check_free__"

LIST_MY_REQUESTS = "list_my_requests"

# Obviously fake, tests only: none of these is a credential, and
# nothing in this file connects to any of these hosts.
TEST_JWT_SIGNING_PHRASE = "auth-middleware-test-jwt-secret-0123456789abcdef"
OTHER_JWT_SECRET = "another-auth-middleware-fake-secret-0123456789"
MCP_URL = "http://mcp.invalid:8081/mcp"
DATABASE_URL = "postgresql+asyncpg://fake:fake@db.invalid:5432/fake_auth_mw"
REDIS_URL = "redis://redis.invalid:6379/0"
QDRANT_URL = "http://qdrant.invalid:6333"

# The origin the CORS middleware is wired to accept (its default, frozen at the
# import of `app`): used only by the preflight case.
ALLOWED_ORIGIN = "http://localhost:3000"

TENANT_ID = uuid.UUID("11111111-1111-5111-8111-111111111111")
USER_ID = uuid.UUID("22222222-2222-5222-8222-222222222222")
DEPARTMENT_ID = uuid.UUID("33333333-3333-5333-8333-333333333333")
SESSION_ID = uuid.UUID("44444444-4444-5444-8444-444444444444")
REQUEST_ID = uuid.UUID("66666666-6666-5666-8666-666666666666")

# A token issued nine hours ago: `issue_token` gives an 8-hour `exp`, so this
# one verifies its signature and fails on expiry alone.
EXPIRED_ISSUED_AT = datetime.now(UTC) - timedelta(hours=9)

REQUESTS_PAYLOAD: Mapping[str, Any] = {
    "requests": [
        {
            "request_id": str(REQUEST_ID),
            "target_user_id": str(USER_ID),
            "role_id": "77777777-7777-5777-8777-777777777777",
            "justification": "Obviously fake justification.",
            "requested_expiry_days": 30,
            "risk_class": "elevated",
            "decision": "approval",
            "status": "pending",
            "created_at": "2026-06-02T10:15:00Z",
            "role_slug": "payroll-read",
            "role_name": "Fake Payroll Read",
            "system_slug": "payroll",
            "system_name": "Fake Payroll",
            "target_display_name": "Fake Person One",
            "decision_reasons": ["NOT_IN_TEMPLATE"],
            "citations": [],
            "approvals": [{"slot": "department_manager", "user_id": None}],
            "grant_id": None,
            "expires_at": None,
        }
    ]
}


# --- the scripted seam --------------------------------------------------------


class ScriptedToolCaller:
    """A `ToolCaller` answering one scripted payload per call, in order.

    The same stub shape `test_me_routes.py` uses: a call past the last scripted
    answer fails the test outright, which is how "no route was reached" is
    asserted as a hard stop rather than as a count read afterwards. A middleware
    that turned a request away before the route means `call_tool` is never
    reached and `entered` stays 0.
    """

    def __init__(self, *answers: Mapping[str, Any]) -> None:
        self._answers = list(answers)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.entered = 0

    async def __aenter__(self) -> ToolCaller:
        self.entered += 1
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def call_tool(self, name: str, arguments: BaseModel) -> Mapping[str, Any]:
        self.calls.append((name, arguments.model_dump(mode="json")))
        index = len(self.calls) - 1
        if index >= len(self._answers):
            raise AssertionError(
                f"the route made {len(self.calls)} tool calls ({self.names}); "
                f"only {len(self._answers)} were scripted"
            )
        return self._answers[index]

    @property
    def names(self) -> list[str]:
        return [name for name, _arguments in self.calls]


class CallerSpy:
    """Records the bearer `build_tool_caller` was handed, and hands back a stub."""

    def __init__(self, caller: ToolCaller) -> None:
        self._caller = caller
        self.bearer_tokens: list[str] = []

    def build(self, settings: Settings, bearer_token: str) -> ToolCaller:
        self.bearer_tokens.append(bearer_token)
        return self._caller


@pytest.fixture
def mw_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Obviously fake settings. Nothing here is dialled: no lifespan is entered."""
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    monkeypatch.setenv("QDRANT_URL", QDRANT_URL)
    monkeypatch.setenv("JWT_SECRET", TEST_JWT_SIGNING_PHRASE)
    monkeypatch.setenv("MCP_URL", MCP_URL)


def _token(
    *,
    secret: str = TEST_JWT_SIGNING_PHRASE,
    issued_at: datetime | None = None,
) -> str:
    """One verified-looking token for one fake person and one conversation."""
    return issue_token(
        {
            "sub": str(USER_ID),
            "tenant_id": str(TENANT_ID),
            "department_id": str(DEPARTMENT_ID),
            "principals": ["employee"],
            "sid": str(SESSION_ID),
        },
        secret,
        datetime.now(UTC) if issued_at is None else issued_at,
    )


@app.get(CHECK_FREE_PATH)
async def _plan64_check_free() -> dict[str, str]:
    """A route with NO token check of its own, added for these tests.

    Its whole point is to have no guard but the middleware: a request that
    reaches it answers 200, so any 401 on this path can only have come from the
    middleware standing in front of every route. Private name so it collides
    with nothing a real case reads; it stays on the module-global `app`, which
    is harmless because nothing else drives this path.
    """
    return {"ok": "yes"}


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _drive(scenario: Callable[[httpx.AsyncClient], Awaitable[httpx.Response]]) -> httpx.Response:
    async def runner() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await scenario(client)

    return asyncio.run(runner())


def _get(
    *,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    caller: ToolCaller,
    headers: Mapping[str, str] | None = None,
) -> tuple[httpx.Response, CallerSpy]:
    """One GET against the app, with the tool caller replaced at the seam.

    The lifespan is NOT entered: the routes a case here reaches read nothing
    off `app.state`, so entering it would buy a pool, a Redis client and a
    gateway client no case uses (and three sockets this file promises not to
    open).
    """
    spy = CallerSpy(caller)
    monkeypatch.setattr(deps, "build_tool_caller", spy.build)

    async def scenario(client: httpx.AsyncClient) -> httpx.Response:
        return await client.get(path, headers=dict(headers or {}))

    return _drive(scenario), spy


# --- the cases ----------------------------------------------------------------


def test_a_protected_path_with_no_bearer_is_401_forbidden_and_reaches_no_route(
    mw_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches an app with no middleware at all, or one whose check runs after
    the route: a protected path with no header must be turned away BEFORE the
    route runs.

    The 401 is read off `CHECK_FREE_PATH`, a route with no guard but the
    middleware, so only the middleware can produce it. The
    `/me/requests` leg then pins "reaches no route": its caller is scripted with
    NOTHING, so a request that got through to the route would open the session
    and fail the case outright. Driving `/me/requests` for the 401 DECISION
    would be vacuous today -- that route still runs its own `authenticated()`
    check, which would 401 a no-bearer request whether or not the middleware is
    mounted; the check-free path is what makes the middleware the sole decider.
    """
    protected, spy = _get(
        monkeypatch=monkeypatch, path=REQUESTS_PATH, caller=ScriptedToolCaller(), headers={}
    )
    check_free, _spy = _get(
        monkeypatch=monkeypatch, path=CHECK_FREE_PATH, caller=ScriptedToolCaller(), headers={}
    )

    assert check_free.status_code == 401
    assert check_free.json() == {"code": ErrorCode.FORBIDDEN.value}
    assert protected.status_code == 401
    assert protected.json() == {"code": ErrorCode.FORBIDDEN.value}
    assert spy.bearer_tokens == []


def test_a_brand_new_route_with_no_own_check_is_still_blocked(
    mw_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a middleware that only blocks the paths that happen to have their
    own check today: a route with no token check of its own must still be
    refused when it carries no bearer.

    A throwaway 200 route is added to the app. It has no bearer handling at all,
    so if it answers 200 the block-by-default is not there; only the middleware
    standing in front of every path can make it 401. The path
    is obviously private so it pollutes nothing a real case reads.
    """

    @app.get("/__throwaway_plan64__")
    async def _throwaway() -> dict[str, str]:
        return {"ok": "yes"}

    caller = ScriptedToolCaller()

    response, _spy = _get(
        monkeypatch=monkeypatch, path="/__throwaway_plan64__", caller=caller, headers={}
    )

    assert response.status_code == 401
    assert response.json() == {"code": ErrorCode.FORBIDDEN.value}


def test_the_open_list_is_exactly_health_ready_metrics_and_login() -> None:
    """Catches an open list that is too wide (a path that should be guarded slips
    in) or too narrow (a probe or login that must stay open is missing).

    The set is asserted as a whole, so an added or dropped member flips it
    (an exact value). A band or a prefix rule could not pin the
    exact four.
    """
    assert {"/healthz", "/readyz", "/metrics", "/auth/login"} == OPEN_PATHS


def test_each_open_path_is_reachable_without_a_bearer(
    mw_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a middleware that blocks a path that must stay open: each of the
    open paths must reach its route with no bearer.

    `/healthz` and `/metrics` answer 200; `POST /auth/login` with a bad body
    reaches the route and is answered by FastAPI's own 422 for the missing
    fields -- proving the request got past the middleware to the route
    rather than being turned away at 401. None of the three is a 401, which is
    the single thing the middleware would wrongly do to an open path.
    """

    async def scenario(client: httpx.AsyncClient) -> dict[str, httpx.Response]:
        health = await client.get("/healthz")
        metrics = await client.get("/metrics")
        login = await client.post("/auth/login", json={"not": "a valid login body"})
        return {"health": health, "metrics": metrics, "login": login}

    async def runner() -> dict[str, httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await scenario(client)

    results = asyncio.run(runner())

    assert results["health"].status_code == 200
    assert results["metrics"].status_code == 200
    assert results["login"].status_code == 422
    for response in results.values():
        assert response.status_code != 401


def test_a_path_that_only_looks_open_is_still_blocked(
    mw_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a middleware matching the open list by prefix instead of exact
    path: `/metricsX` and `/auth/login/x` only LOOK like open paths, and a
    prefix or `startswith` check would wave them through.

    Both carry no bearer, so an exact-match open list turns them away with 401;
    a loose match would let them reach a route (or a 404) that is not a 401.
    These two differ from the real open paths only in the trailing characters,
    so only the exact-match rule decides them.
    """
    caller = ScriptedToolCaller()

    metrics_x, _spy_a = _get(monkeypatch=monkeypatch, path="/metricsX", caller=caller, headers={})
    login_x, _spy_b = _get(monkeypatch=monkeypatch, path="/auth/login/x", caller=caller, headers={})

    assert metrics_x.status_code == 401
    assert metrics_x.json() == {"code": ErrorCode.FORBIDDEN.value}
    assert login_x.status_code == 401
    assert login_x.json() == {"code": ErrorCode.FORBIDDEN.value}


def test_an_expired_or_forged_token_is_401_like_an_absent_one(
    mw_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a middleware that reads the claims without verifying them, and one
    that verifies the signature but not the expiry.

    The forged token is well formed and carries a real tenant, user and `sid`;
    it is signed with the wrong secret and nothing else, so only signature
    verification can refuse it. The expired token is signed with the RIGHT
    secret and differs from a good one only in being nine hours old against an
    eight-hour `exp`, so only the expiry comparison can refuse it. Both are
    driven at `CHECK_FREE_PATH`, whose only guard is the middleware, so each
    401 can only have come from the middleware's own verify;
    a token that `/me/requests` would 401 via its own check could not tell the
    two apart. Each must answer the same 401 `{"code":"FORBIDDEN"}` an absent
    bearer gets.
    """
    forged, forged_spy = _get(
        monkeypatch=monkeypatch,
        path=CHECK_FREE_PATH,
        caller=ScriptedToolCaller(),
        headers=_auth(_token(secret=OTHER_JWT_SECRET)),
    )
    expired, expired_spy = _get(
        monkeypatch=monkeypatch,
        path=CHECK_FREE_PATH,
        caller=ScriptedToolCaller(),
        headers=_auth(_token(issued_at=EXPIRED_ISSUED_AT)),
    )

    assert forged.status_code == 401
    assert forged.json() == {"code": ErrorCode.FORBIDDEN.value}
    assert forged_spy.bearer_tokens == []

    assert expired.status_code == 401
    assert expired.json() == {"code": ErrorCode.FORBIDDEN.value}
    assert expired_spy.bearer_tokens == []


def test_a_valid_token_reaches_the_route_and_its_bearer_is_forwarded(
    mw_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a middleware that refuses a GOOD token (nothing would reach the
    route) or one that stashes the wrong bearer on `request.state` for the route
    to forward.

    A valid token reaches `/me/requests`, the scripted caller answers, and the
    bearer the seam was handed is the SAME token the request carried (the
    identity rule). The status filter from the query still reaches the tool, proving the
    route actually ran rather than being short-circuited. The kill fires once
    the plan strips the route's own header read and the route forwards the
    bearer the middleware put on `request.state`: a middleware that refused a
    good token, or stashed a bearer other than the caller's own, flips the
    status or the forwarded-token assertion. Green before the plan lands (the
    route still reads the header itself); this is the acceptance guard that the
    good-token path survives the move to `request.state`.
    """
    caller = ScriptedToolCaller(REQUESTS_PAYLOAD)
    token = _token()

    response, spy = _get(
        monkeypatch=monkeypatch,
        path=f"{REQUESTS_PATH}?status=pending&limit=25",
        caller=caller,
        headers=_auth(token),
    )

    assert response.status_code == 200
    assert response.json() == REQUESTS_PAYLOAD
    assert caller.names == [LIST_MY_REQUESTS]
    _name, arguments = caller.calls[0]
    assert arguments["status"] == "pending"
    assert arguments["limit"] == 25
    assert spy.bearer_tokens == [token]


def test_a_cors_preflight_to_a_protected_path_is_not_blocked() -> None:
    """Catches a middleware ordered OUTSIDE CORS, so it turns away the browser's
    preflight before CORS can answer it.

    A browser sends `OPTIONS` with no token, so if the auth check ran first the
    preflight would be a 401 and the real request would never be sent. The CORS
    middleware is outermost and short-circuits the preflight: the response
    carries the allow-origin header and is never a 401. The allowed origin is
    the one the CORS middleware was wired to accept.
    """

    async def scenario(client: httpx.AsyncClient) -> httpx.Response:
        return await client.options(
            REQUESTS_PATH,
            headers={
                "Origin": ALLOWED_ORIGIN,
                "Access-Control-Request-Method": "GET",
            },
        )

    response = _drive(scenario)

    assert response.status_code != 401
    assert response.headers.get("access-control-allow-origin") == ALLOWED_ORIGIN
