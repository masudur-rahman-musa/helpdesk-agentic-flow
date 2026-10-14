"""Tests for agent-api's locked CORS policy.

API under test:
    agent_api.main.app -- FastAPI application, with Starlette's
        `CORSMiddleware` mounted at import time from
        `helpdesk.core.settings.web_origin_from_env()`, with
        `allow_credentials=False`,
        `allow_methods=["GET", "POST", "OPTIONS"]` and
        `allow_headers=["authorization", "content-type", "accept"]`.

Named for its service per a project rule; no basename collides in the tree.
Every case drives the app over `httpx.ASGITransport` against `/healthz`, the
shape `test_health.py` already uses: no lifespan runs, so no backing service is
touched and no dependency check is reached.

`/healthz` is the route because it decides nothing about CORS. The middleware
answers before any route does, so the policy asserted here is the policy every
route gets; picking an authenticated route would let a 401 from the auth
dependency stand in for a refusal the CORS layer never made.
"""

import asyncio

import httpx
import pytest
from starlette.middleware.cors import SAFELISTED_HEADERS

from helpdesk.agent_api.api.router import app

ALLOWED_ORIGIN = "http://localhost:3000"
"""The configured origin, written out rather than imported.

Importing `DEFAULT_WEB_ORIGIN` here and comparing the response to it would pass
for whatever value the settings module chose, including a wildcard."""

DISALLOWED_ORIGIN = "https://evil.example"
"""Any other origin. Not a neighbour of the allowed one: a middleware matching
on a prefix, a suffix or a substring still refuses this."""


def _request(method: str, path: str, headers: dict[str, str]) -> httpx.Response:
    async def _run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, headers=headers)

    return asyncio.run(_run())


def _preflight(origin: str, request_headers: str | None = None) -> httpx.Response:
    headers = {"Origin": origin, "Access-Control-Request-Method": "POST"}
    if request_headers is not None:
        headers["Access-Control-Request-Headers"] = request_headers
    return _request("OPTIONS", "/healthz", headers)


def test_the_configured_origin_is_echoed_exactly_and_never_a_wildcard() -> None:
    """Catches an app with no CORS middleware mounted at all, and an app that
    answers `access-control-allow-origin: *`.

    Both are the shapes a browser call from the web app survives, so neither
    shows up in any other suite: with no middleware the browser refuses the
    response and the server logs a clean 200, and with `*` every site on the
    internet may read agent-api's answers. This case is the reason the foreign
    origin case below can claim a kill - deleting the middleware entirely makes
    that one pass, and this one fail.
    """
    response = _request("GET", "/healthz", {"Origin": ALLOWED_ORIGIN})

    assert response.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
    assert response.headers["access-control-allow-origin"] != "*"


def test_a_foreign_origin_gets_no_allow_origin_header() -> None:
    """Catches a middleware mounted with `allow_origins=["*"]` or with
    `allow_origin_regex` wide enough to match anything.

    Deleting the `allow_origins` restriction alone - the control under test -
    is what flips this row: the middleware is still mounted, the request is
    still a simple GET, and the only thing that changes is which origins are
    echoed. Deleting the whole middleware instead makes this case pass
    vacuously, which is exactly why
    `test_the_configured_origin_is_echoed_exactly_and_never_a_wildcard` runs
    beside it and fails in that case.
    """
    response = _request("GET", "/healthz", {"Origin": DISALLOWED_ORIGIN})

    assert "access-control-allow-origin" not in response.headers


def test_a_preflight_from_the_configured_origin_allows_exactly_the_planned_methods() -> None:
    """Catches a preflight answered with the method set a `CORSMiddleware`
    built without `allow_methods` gives (`GET` only), and one built with
    `allow_methods=["*"]` (which answers every verb, `DELETE` and `PUT`
    included).

    The set is asserted as a whole rather than by membership, so a widening is
    red as well as a narrowing: a middleware that allows `PUT` has no route to
    serve it, and the browser preflight is the only place that widening is
    ever visible.
    """
    response = _preflight(ALLOWED_ORIGIN)

    assert response.status_code == 200
    allowed = {
        method.strip() for method in response.headers["access-control-allow-methods"].split(",")
    }
    assert allowed == {"GET", "POST", "OPTIONS"}


def test_a_preflight_allows_exactly_the_planned_headers() -> None:
    """Catches a middleware left on `allow_headers`'s default, and one built
    with `allow_headers=["*"]`.

    `authorization` is the header the kill rests on: the web app's proxy sends
    it on every chat turn, and a preflight that does not name it fails the turn
    in the browser before any request reaches a route. A default `allow_headers`
    answers the safelist alone, so `authorization` is missing and this row is
    red. `["*"]` makes Starlette MIRROR whatever was requested, so the answer
    would be the three requested headers with no safelist at all - also red
    against an exact set, where a membership check would pass.

    The expected set is the three planned headers UNION the installed
    Starlette's own `SAFELISTED_HEADERS`, rendered from that constant rather
    than copied. `CORSMiddleware` adds the safelist to
    whatever it is given (`cors.py`: `sorted(SAFELISTED_HEADERS |
    set(allow_headers))`), so section 1's literal "exactly authorization,
    content-type and accept" is unreachable through decision A's middleware;
    this is 27a's one deviation and the plan owes the line. Note the
    consequence: `accept` and `content-type` are safelisted, so only
    `authorization` is observably granted by the configuration.
    """
    response = _preflight(ALLOWED_ORIGIN, "authorization,content-type,accept")

    allowed = {
        header.strip().lower()
        for header in response.headers["access-control-allow-headers"].split(",")
    }
    expected = {"authorization", "content-type", "accept"} | {
        header.lower() for header in SAFELISTED_HEADERS
    }
    assert allowed == expected


def test_a_preflight_from_a_foreign_origin_is_refused() -> None:
    """Catches an origin check applied to simple requests but not to the
    preflight path - the two run through different branches of the middleware,
    so a diff that hardcodes the allowed origin on one and a wildcard on the
    other leaves the foreign-origin GET case green while a foreign site still
    gets a preflight telling it `POST` is permitted.

    The refusal asserted is the absence of `access-control-allow-origin`, not a
    status code: what stops the browser is the missing header, and a refusal
    that answered 200 with no allow headers would be equally safe.
    """
    response = _preflight(DISALLOWED_ORIGIN)

    assert "access-control-allow-origin" not in response.headers


@pytest.mark.parametrize(
    "preflight",
    [False, True],
    ids=["a simple request", "a preflight"],
)
def test_no_response_carries_allow_credentials(preflight: bool) -> None:
    """Catches a middleware built with `allow_credentials=True` (decision C1
    says False).

    Nothing else in the tree would notice: the browser talks only to the Next
    origin and the bearer is attached server-side by the proxy, so no browser
    request to agent-api carries credentials and every functional case stays
    green either way. Both request shapes are covered because Starlette emits
    the credentials header on the preflight and the simple response from
    separate code paths.
    """
    if preflight:
        response = _preflight(ALLOWED_ORIGIN, "authorization,content-type,accept")
    else:
        response = _request("GET", "/healthz", {"Origin": ALLOWED_ORIGIN})

    assert "access-control-allow-credentials" not in response.headers
