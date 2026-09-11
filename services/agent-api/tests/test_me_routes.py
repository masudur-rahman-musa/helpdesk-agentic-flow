"""An earlier change §6: `GET /me/requests` and `GET /me/access`, the app driven in process.

Surface under test (written before it exists; every case below is red on one
of these names until steps 2 and 3 land):

``agent_api.deps`` (new)
    ``build_tool_caller(settings: Settings, bearer_token: str) -> ToolCaller``,
    ``_bearer`` and ``_claims``, moved out of ``chat_routes`` so two routers
    share one seam (decision C). ``chat_routes`` keeps all three as module
    globals, which is why the three existing monkeypatch sites still bind and
    why §8 edits no old test.

``agent_api.me_routes`` (new)
    ``router`` with ``GET /me/requests`` (optional ``status``, optional
    ``limit``) and ``GET /me/access``, both calling the MCP tools
    ``list_my_requests`` and ``list_my_access`` through the MODULE attribute
    ``me_routes.build_tool_caller`` — the same seam ``chat_routes`` uses, so
    these cases drive a scripted caller and open no socket.
    ``STATUS_FOR_CODE: Mapping[ErrorCode, int]`` — this router's own map.
``agent_api.main``
    the router registered beside the other three.

Shapes this file fixes that the plan left open (a project rule and 55;
these belong in 23a's Deviations):
- the refusal envelope is ``{"code": "<ErrorCode value>"}``, agent-api's own
  shape from ``chat_routes._typed``, and never free text.
- ``STATUS_FOR_CODE[ErrorCode.FORBIDDEN]`` is **403** here, deliberately NOT
  the 401 ``chat_routes.STATUS_FOR_CODE`` carries: on this router a FORBIDDEN
  can only come from the RBAC matrix, and §1 says a matrix refusal is a 403.
  The bearer refusal is therefore answered as a literal 401 rather than
  through the map — the two are different facts ("you did not prove who you
  are" against "you may not read this") and one map cannot say both.
- an unreachable MCP server is ``500 {"code": "INTERNAL"}``: ``INTERNAL`` has
  no row in the map and falls through to 500, exactly as it does on ``/chat``.
- both routes read their bearer from the ``Authorization`` header, and both
  answer JSON (``application/json``), never a stream.

Cost and isolation: NO backing service at all. The app's
lifespan is deliberately not entered — these two routes read nothing off
``app.state``, they only call an MCP tool — so nothing here opens a socket, a
pool or a Redis connection, and the settings are obviously fake values set per
case. Fixtures live in this module rather than a conftest, because a second
``conftest`` under ``services/agent-api/tests`` would collide with
``packages/helpdesk/tests/conftest.py`` under the repo-wide ``mypy`` run.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from agent_api import deps, me_routes
from agent_api.main import app
from agent_api.mcp_client import (
    TRANSPORT_FAILURE_MESSAGE,
    RemoteForbiddenError,
    RemoteInternalError,
    RemoteToolError,
    ToolCaller,
)
from helpdesk.auth.jwt import issue_token
from helpdesk.core.settings import Settings
from helpdesk.errors import ErrorCode
from pydantic import BaseModel

REQUESTS_PATH = "/me/requests"
ACCESS_PATH = "/me/access"

LIST_MY_REQUESTS = "list_my_requests"
LIST_MY_ACCESS = "list_my_access"

# Obviously fake, tests only: none of these is a credential, and
# nothing in this file connects to any of these hosts.
TEST_JWT_SECRET = "me-routes-test-jwt-secret-0123456789abcdef"
OTHER_JWT_SECRET = "another-me-routes-fake-secret-0123456789"
MCP_URL = "http://mcp.invalid:8081/mcp"
MCP_HOST = "mcp.invalid"
MCP_PATH = "/mcp"
DATABASE_URL = "postgresql+asyncpg://fake:fake@db.invalid:5432/fake_me_routes"
REDIS_URL = "redis://redis.invalid:6379/0"
QDRANT_URL = "http://qdrant.invalid:6333"

TENANT_ID = uuid.UUID("11111111-1111-5111-8111-111111111111")
USER_ID = uuid.UUID("22222222-2222-5222-8222-222222222222")
DEPARTMENT_ID = uuid.UUID("33333333-3333-5333-8333-333333333333")
SESSION_ID = uuid.UUID("44444444-4444-5444-8444-444444444444")
GRANT_ID = uuid.UUID("55555555-5555-5555-8555-555555555555")
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
"""One widened row, exactly as the tool answers it. The route must hand this
back UNCHANGED, so the case asserting equality is what catches a router that
reshapes, filters or renames on the way out."""

ACCESS_PAYLOAD: Mapping[str, Any] = {
    "grants": [
        {
            "grant_id": str(GRANT_ID),
            "role_slug": "payroll-read",
            "role_name": "Fake Payroll Read",
            "privilege": "read",
            "system_slug": "payroll",
            "system_name": "Fake Payroll",
            "granted_at": "2026-01-05T09:00:00Z",
            "expires_at": "2099-03-01T12:00:00Z",
        }
    ]
}


# --- the scripted seam --------------------------------------------------------


class ScriptedToolCaller:
    """A `ToolCaller` answering one scripted payload per call, in order.

    The same stub shape `test_chat_route.py` uses: a call
    past the last scripted answer fails the test outright, which is how "the
    route opened no session at all" is asserted as a hard stop rather than as
    a count read afterwards.
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


class RefusingToolCaller:
    """A caller whose session opens and whose tool call raises a typed error."""

    def __init__(self, error: RemoteToolError) -> None:
        self._error = error
        self.calls: list[str] = []
        self.entered = 0

    async def __aenter__(self) -> ToolCaller:
        self.entered += 1
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def call_tool(self, name: str, arguments: BaseModel) -> Mapping[str, Any]:
        self.calls.append(name)
        raise self._error


class UnreachableToolCaller:
    """A caller whose handshake fails, exactly as `McpToolCaller.__aenter__` does."""

    def __init__(self) -> None:
        self.entered = 0

    async def __aenter__(self) -> ToolCaller:
        self.entered += 1
        raise RemoteInternalError(TRANSPORT_FAILURE_MESSAGE)

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def call_tool(self, name: str, arguments: BaseModel) -> Mapping[str, Any]:
        raise AssertionError("a caller whose handshake failed must never be called")


@pytest.fixture
def me_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Obviously fake settings. Nothing here is dialled: no lifespan is entered."""
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    monkeypatch.setenv("QDRANT_URL", QDRANT_URL)
    monkeypatch.setenv("JWT_SECRET", TEST_JWT_SECRET)
    monkeypatch.setenv("MCP_URL", MCP_URL)


def _token(
    *,
    secret: str = TEST_JWT_SECRET,
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


class CallerSpy:
    """Records the bearer `build_tool_caller` was handed, and hands back a stub."""

    def __init__(self, caller: ToolCaller) -> None:
        self._caller = caller
        self.bearer_tokens: list[str] = []

    def build(self, settings: Settings, bearer_token: str) -> ToolCaller:
        self.bearer_tokens.append(bearer_token)
        return self._caller


def _get(
    *,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    caller: ToolCaller,
    headers: Mapping[str, str] | None = None,
) -> tuple[httpx.Response, CallerSpy]:
    """One GET against the app, with the tool caller replaced at the seam.

    The lifespan is NOT entered: these routes read nothing off `app.state`, so
    entering it would buy a psycopg pool, a Redis client and a gateway client
    that no case here uses (and three sockets this file promises not to open).
    """
    spy = CallerSpy(caller)
    monkeypatch.setattr(deps, "build_tool_caller", spy.build)

    async def scenario(client: httpx.AsyncClient) -> httpx.Response:
        return await client.get(path, headers=dict(headers or {}))

    return _drive(scenario), spy


def _drive(scenario: Callable[[httpx.AsyncClient], Awaitable[httpx.Response]]) -> httpx.Response:
    async def runner() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await scenario(client)

    return asyncio.run(runner())


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- the happy paths ----------------------------------------------------------


def test_the_requests_route_answers_the_tools_output_unchanged(
    me_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a route that reshapes, filters or renames the tool's structured
    output on the way out (§1: "return the tool's structured output
    unchanged"), a route that calls a different tool, and a route that drops
    the query parameters instead of passing them as the tool's arguments — a
    `/requests?status=pending` answering every status is a filter the page
    would render as fact.

    The bearer half is a project rule and decision A: the token the caller
    presented is forwarded VERBATIM to `build_tool_caller`, so RBAC, the kill
    switch and the audit event all apply to this read. A route that minted its
    own service token, or reused a configured one, fails on the identity
    assertion alone.
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
    name, arguments = caller.calls[0]
    assert name == LIST_MY_REQUESTS
    assert arguments["status"] == "pending"
    assert arguments["limit"] == 25
    assert spy.bearer_tokens == [token]


def test_the_access_route_answers_the_tools_output_unchanged(
    me_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same three kills for the second route: the wrong tool name, a
    reshaped body, and a bearer that is not the caller's own.

    `list_my_access` takes no arguments at all (its rows are one user's live
    grants), so this case also catches a route that invents one — an argument
    naming a user on a self-scoped tool is exactly what a project rule forbids.
    """
    caller = ScriptedToolCaller(ACCESS_PAYLOAD)
    token = _token()

    response, spy = _get(
        monkeypatch=monkeypatch, path=ACCESS_PATH, caller=caller, headers=_auth(token)
    )

    assert response.status_code == 200
    assert response.json() == ACCESS_PAYLOAD
    assert caller.names == [LIST_MY_ACCESS]
    _name, arguments = caller.calls[0]
    assert "user_id" not in arguments
    assert "target_user_id" not in arguments
    assert spy.bearer_tokens == [token]


def test_the_requests_route_passes_no_status_and_no_filter_when_none_is_asked_for(
    me_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a route that hardcodes a status (a `/requests` page silently
    showing only `pending` rows), and one that sends a stray string where the
    tool's optional filter is expected.

    The `limit` half is deliberately NOT asserted: §1 puts the default on the
    tool's own input model (`REQUESTS_LIMIT_DEFAULT = 50`), pinned in
    `test_mcp_access_read_tools.py`, so a route repeating the number here
    would be a second place for it to drift from.
    """
    caller = ScriptedToolCaller(REQUESTS_PAYLOAD)

    response, _spy = _get(
        monkeypatch=monkeypatch, path=REQUESTS_PATH, caller=caller, headers=_auth(_token())
    )

    assert response.status_code == 200
    _name, arguments = caller.calls[0]
    assert arguments.get("status") is None


# --- the refusals -------------------------------------------------------------


BAD_BEARERS: tuple[tuple[str, Mapping[str, str]], ...] = (
    ("no-header", {}),
    ("wrong-scheme", {"Authorization": "Basic ZmFrZTpmYWtl"}),
    ("empty-bearer", {"Authorization": "Bearer "}),
)


@pytest.mark.parametrize("path", [REQUESTS_PATH, ACCESS_PATH], ids=["requests", "access"])
@pytest.mark.parametrize(
    "headers", [case[1] for case in BAD_BEARERS], ids=[c[0] for c in BAD_BEARERS]
)
def test_an_absent_or_malformed_bearer_is_a_typed_401_and_opens_no_session(
    me_env: None, monkeypatch: pytest.MonkeyPatch, path: str, headers: Mapping[str, str]
) -> None:
    """Catches a route with no token check at all, and one that checks the
    token AFTER opening the MCP session: the caller is scripted with NOTHING,
    so a route that entered it or called a tool fails outright rather than
    showing up as a count afterwards.

    The token check is the only guard on this path — there is no rate limit, no
    body to validate and no DB read — so deleting it is what flips every row
    here.
    """
    caller = ScriptedToolCaller()

    response, spy = _get(monkeypatch=monkeypatch, path=path, caller=caller, headers=headers)

    assert response.status_code == 401
    assert response.json() == {"code": ErrorCode.FORBIDDEN.value}
    assert caller.entered == 0
    assert caller.calls == []
    assert spy.bearer_tokens == []


@pytest.mark.parametrize("path", [REQUESTS_PATH, ACCESS_PATH], ids=["requests", "access"])
def test_a_forged_bearer_is_a_typed_401_and_opens_no_session(
    me_env: None, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """Catches a route that reads the claims without VERIFYING the signature:
    this token is well formed and carries a real tenant, a real user and a real
    `sid` — it is signed with the wrong secret and nothing else, so only the
    verification can refuse it. A route that parsed the claims and trusted them
    would answer 200 with somebody's data.
    """
    caller = ScriptedToolCaller()

    response, spy = _get(
        monkeypatch=monkeypatch,
        path=path,
        caller=caller,
        headers=_auth(_token(secret=OTHER_JWT_SECRET)),
    )

    assert response.status_code == 401
    assert response.json() == {"code": ErrorCode.FORBIDDEN.value}
    assert caller.entered == 0
    assert spy.bearer_tokens == []


@pytest.mark.parametrize("path", [REQUESTS_PATH, ACCESS_PATH], ids=["requests", "access"])
def test_an_expired_bearer_is_a_typed_401_and_opens_no_session(
    me_env: None, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """Catches a verification that checks the SIGNATURE and not the expiry:
    this token is signed with the right secret and differs from the happy
    path's only in being nine hours old against an eight-hour `exp`, so the
    expiry comparison is the only thing that can refuse it. A stale token left
    working is a session that never ends.
    """
    caller = ScriptedToolCaller()

    response, spy = _get(
        monkeypatch=monkeypatch,
        path=path,
        caller=caller,
        headers=_auth(_token(issued_at=EXPIRED_ISSUED_AT)),
    )

    assert response.status_code == 401
    assert response.json() == {"code": ErrorCode.FORBIDDEN.value}
    assert caller.entered == 0
    assert spy.bearer_tokens == []


def test_the_status_map_dresses_forbidden_as_403_and_not_as_chats_401() -> None:
    """Catches the map copied wholesale from `chat_routes`, where FORBIDDEN is
    401: on this router a FORBIDDEN can only come from the RBAC matrix, and
    §1 says a matrix refusal is a 403 and never a 500. a project rule wants
    the value, and the pair asserted together is what stops the two routers
    from being "fixed" into agreeing.
    """
    assert me_routes.STATUS_FOR_CODE[ErrorCode.FORBIDDEN] == 403
    assert ErrorCode.INTERNAL not in me_routes.STATUS_FOR_CODE


@pytest.mark.parametrize("path", [REQUESTS_PATH, ACCESS_PATH], ids=["requests", "access"])
def test_a_tool_refusal_answers_its_own_typed_code(
    me_env: None, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """Catches a route that lets the tool's `ToolError` escape: FastAPI answers
    an untyped 500, so a permission decision the caller could act on reads as
    an outage they cannot. The bearer here VERIFIES, so the token gate cannot
    decide this case — only the `except ToolError` branch and its own map can.
    """
    caller = RefusingToolCaller(RemoteForbiddenError(ErrorCode.FORBIDDEN.value))

    response, _spy = _get(
        monkeypatch=monkeypatch, path=path, caller=caller, headers=_auth(_token())
    )

    assert response.status_code == 403
    assert response.json() == {"code": ErrorCode.FORBIDDEN.value}
    assert caller.entered == 1


@pytest.mark.parametrize("path", [REQUESTS_PATH, ACCESS_PATH], ids=["requests", "access"])
def test_an_unreachable_mcp_server_is_one_typed_internal_naming_neither_host_nor_path(
    me_env: None, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """Catches two failures at once: a handshake failure left to propagate
    (an untyped 500 with a traceback body), and a body that carries the
    client's message — `TRANSPORT_FAILURE_MESSAGE` is opaque on purpose, and a
    route rendering the exception's `str` would leak the MCP host and path to
    a browser. `INTERNAL` has no row in the map, so 500 here also pins that
    the fall-through is the unreachable case rather than a forgotten row.
    """
    caller = UnreachableToolCaller()

    response, _spy = _get(
        monkeypatch=monkeypatch, path=path, caller=caller, headers=_auth(_token())
    )

    assert response.status_code == 500
    assert response.json() == {"code": ErrorCode.INTERNAL.value}
    body = response.text
    assert MCP_HOST not in body
    assert MCP_PATH not in body
    assert TRANSPORT_FAILURE_MESSAGE not in body
