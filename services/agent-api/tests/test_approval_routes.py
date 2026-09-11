"""An earlier change §6: the approval queue and the two decisions, over HTTP.

Surface under test (written before it exists; every case below is red on one
of these names until steps 2, 3 and 4 land):

``agent_api.deps`` (widened by step 2, decision C)
    ``STATUS_FOR_CODE: Mapping[ErrorCode, int]``, ``typed_refusal(code) ->
    JSONResponse`` and ``call_tool_route(authorization, tool, arguments) ->
    JSONResponse``, moved out of ``me_routes`` so 23e, 23f and 24 share one
    typed-refusal map. ``call_tool_route`` reaches ``build_tool_caller``
    through THIS module's global, which is why every case here patches
    ``deps.build_tool_caller`` and why §8 moves the one line in
    ``test_me_routes.py``.

``agent_api.graph.keys`` (step 3)
    ``derive_decision_key(*, approver_user_id: uuid.UUID, request_id:
    uuid.UUID, verb: str) -> str`` — ``uuid5(KEY_NAMESPACE, approver ∥
    request ∥ verb)`` over the module's existing ``\\x1f`` separator, length
    checked by ``check_idempotency_key`` before it is returned.

``agent_api.approval_routes`` (step 4)
    ``router`` with ``GET /me/approvals`` (optional ``limit``),
    ``POST /approvals/{request_id}/approve`` (optional ``comment``) and
    ``POST /approvals/{request_id}/reject`` (required ``reason``), registered
    in ``main.py``.

Shapes this file fixes that the plan left open (a project rule and 55;
these belong in 23c's Deviations):
- the two VERBS in the key material are the path segments, ``"approve"`` and
  ``"reject"``. Both the route cases and the pure key cases below read the
  same two constants of this module, so whatever the implementation spells
  them, the key a route sends and the key ``derive_decision_key`` returns are
  compared against ONE value; the implementer owes these two strings.
- the refusal envelope is ``{"code": "<ErrorCode value>"}``, agent-api's own
  shape, and the bearer refusal is a literal ``401`` rather than a row of
  ``STATUS_FOR_CODE`` (whose ``FORBIDDEN`` is the matrix's 403) — 23a's
  reasoning, unchanged by the move into ``deps``.
- a non-uuid ``request_id`` is FastAPI's own ``422``: §1 says the path
  parameter is a ``uuid.UUID``, so the refusal is the framework's and no
  second validator is written for it. What the case actually pins is that no
  tool, no key and no query is reached.
- the decision bodies are JSON objects (``{"comment": …}`` / ``{"reason":
  …}``), and the comment/reason bounds stay the TOOL's (decision E), so the
  boundary cases here drive a caller that applies the tool's own
  ``COMMENT_MAX_LENGTH`` / ``REASON_MAX_LENGTH`` and answers the typed
  ``VALIDATION`` the way the real MCP server does. The route's job — the only
  thing these two rows can fail on — is dressing that code as **400** and not
  declaring a narrower bound of its own that answers FastAPI's untyped 422 for
  a comment the tool accepts.

Cost and isolation: NO backing service at all, exactly as
``test_me_routes.py``. The lifespan is never entered — these three routes read
nothing off ``app.state`` — so nothing here opens a socket, a pool or a Redis
connection, and the key cases are pure. Fixtures live in this module rather
than a conftest, for the same reason they do there.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from agent_api import deps
from agent_api.graph.keys import KEY_NAMESPACE, derive_decision_key
from agent_api.main import app
from agent_api.mcp_client import (
    TRANSPORT_FAILURE_MESSAGE,
    RemoteForbiddenError,
    RemoteInternalError,
    RemoteToolDisabledError,
    RemoteToolError,
    RemoteValidationError,
    ToolCaller,
)
from helpdesk.auth.jwt import issue_token
from helpdesk.core.settings import Settings
from helpdesk.errors import ErrorCode
from helpdesk.models.idempotency import KEY_LENGTH
from pydantic import BaseModel

APPROVALS_PATH = "/me/approvals"

LIST_PENDING_APPROVALS = "list_pending_approvals"
APPROVE_REQUEST = "approve_request"
REJECT_REQUEST = "reject_request"

# The two verbs the derived key is taken over, and the two path segments.
APPROVE_VERB = "approve"
REJECT_VERB = "reject"

# `graph/keys.py`'s own separator, repeated here as the literal it is: the
# module keeps it private, and the key material is the thing these cases pin.
SEPARATOR = "\x1f"

# The tool's bounds (`mcp_server.tools.approvals`), written out because
# agent-api cannot import the mcp-server package. a project rule wants the
# values; decision E says these two are the only validator, so the route must
# neither repeat them more narrowly nor refuse what they accept.
COMMENT_MAX_LENGTH = 2000
REASON_MAX_LENGTH = 2000
REASON_MIN_LENGTH = 1

# Obviously fake, tests only: none of these is a credential, and
# nothing in this file connects to any of these hosts.
TEST_JWT_SECRET = "approval-routes-test-jwt-secret-0123456789"
OTHER_JWT_SECRET = "another-approval-routes-fake-secret-01234"
MCP_URL = "http://mcp.invalid:8081/mcp"
MCP_HOST = "mcp.invalid"
MCP_PATH = "/mcp"
DATABASE_URL = "postgresql+asyncpg://fake:fake@db.invalid:5432/fake_approvals"
REDIS_URL = "redis://redis.invalid:6379/0"
QDRANT_URL = "http://qdrant.invalid:6333"

TENANT_ID = uuid.UUID("11111111-1111-5111-8111-111111111111")
APPROVER_ID = uuid.UUID("22222222-2222-5222-8222-222222222222")
OTHER_APPROVER_ID = uuid.UUID("2a2a2a2a-2a2a-5a2a-8a2a-2a2a2a2a2a2a")
DEPARTMENT_ID = uuid.UUID("33333333-3333-5333-8333-333333333333")
SESSION_ID = uuid.UUID("44444444-4444-5444-8444-444444444444")
REQUEST_ID = uuid.UUID("66666666-6666-5666-8666-666666666666")
OTHER_REQUEST_ID = uuid.UUID("6a6a6a6a-6a6a-5a6a-8a6a-6a6a6a6a6a6a")

APPROVE_PATH = f"/approvals/{REQUEST_ID}/approve"
REJECT_PATH = f"/approvals/{REQUEST_ID}/reject"

# A token issued nine hours ago: `issue_token` gives an 8-hour `exp`, so this
# one verifies its signature and fails on expiry alone.
EXPIRED_ISSUED_AT = datetime.now(UTC) - timedelta(hours=9)

QUEUE_PAYLOAD: Mapping[str, Any] = {
    "approvals": [
        {
            "request_id": str(REQUEST_ID),
            "requester_user_id": "88888888-8888-5888-8888-888888888888",
            "target_user_id": "99999999-9999-5999-8999-999999999999",
            "role_id": "77777777-7777-5777-8777-777777777777",
            "justification": "Obviously fake justification.",
            "requested_expiry_days": 30,
            "risk_class": "elevated",
            "decision": "approval",
            "status": "pending",
            "created_at": "2026-06-02T10:15:00Z",
            "my_slot": "department_manager",
            "approvals": [{"slot": "department_manager", "user_id": str(APPROVER_ID)}],
            "role_slug": "payroll-read",
            "role_name": "Fake Payroll Read",
            "system_slug": "payroll",
            "system_name": "Fake Payroll",
            "target_display_name": "Fake Person One",
            "requester_display_name": "Fake Person Two",
            "decision_reasons": ["NOT_IN_TEMPLATE"],
            "citations": [],
        }
    ]
}
"""One widened queue row, exactly as the tool answers it. The route must hand
this back UNCHANGED, so the case asserting equality is what catches a router
that reshapes, filters or renames on the way out."""

APPROVE_PAYLOAD: Mapping[str, Any] = {
    "request_id": str(REQUEST_ID),
    "status": "approved",
    "slot": "department_manager",
    "chain_complete": True,
    "grant_id": "55555555-5555-5555-8555-555555555555",
    "expires_at": "2099-03-01T12:00:00Z",
    "failure_code": None,
}

REJECT_PAYLOAD: Mapping[str, Any] = {
    "request_id": str(REQUEST_ID),
    "status": "rejected",
    "slot": "department_manager",
}


# --- the scripted seam --------------------------------------------------------


class ScriptedToolCaller:
    """A `ToolCaller` answering one scripted payload per call, in order.

    The same stub shape `test_me_routes.py` and `test_chat_route.py` use (plan
    17b's seam): a call past the last scripted answer fails the test outright,
    which is how "the route opened no session at all" is asserted as a hard
    stop rather than as a count read afterwards.
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


class BoundCheckingToolCaller:
    """The MCP server's own input validation, standing in for the wire.

    Decision E: the tool's `COMMENT_MAX_LENGTH` and `REASON_MAX_LENGTH` are
    the only validator, so an over-long comment or an empty reason crosses
    back as a typed `VALIDATION` rather than being refused in agent-api. This
    caller answers exactly that, and records in `decisions` only the calls it
    actually ANSWERED — which is what "no decision recorded" means on a seam
    that never reaches a database.
    """

    def __init__(self, answer: Mapping[str, Any]) -> None:
        self._answer = answer
        self.decisions: list[dict[str, Any]] = []
        self.entered = 0

    async def __aenter__(self) -> ToolCaller:
        self.entered += 1
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def call_tool(self, name: str, arguments: BaseModel) -> Mapping[str, Any]:
        sent = arguments.model_dump(mode="json")
        comment = sent.get("comment")
        reason = sent.get("reason")
        if isinstance(comment, str) and len(comment) > COMMENT_MAX_LENGTH:
            raise RemoteValidationError("comment is too long")
        if name == REJECT_REQUEST and (
            not isinstance(reason, str)
            or not (REASON_MIN_LENGTH <= len(reason) <= REASON_MAX_LENGTH)
        ):
            raise RemoteValidationError("reason is out of bounds")
        self.decisions.append(sent)
        return self._answer


@pytest.fixture
def approvals_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Obviously fake settings. Nothing here is dialled: no lifespan is entered."""
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    monkeypatch.setenv("QDRANT_URL", QDRANT_URL)
    monkeypatch.setenv("JWT_SECRET", TEST_JWT_SECRET)
    monkeypatch.setenv("MCP_URL", MCP_URL)


def _token(
    *,
    user_id: uuid.UUID = APPROVER_ID,
    secret: str = TEST_JWT_SECRET,
    issued_at: datetime | None = None,
) -> str:
    """One verified-looking token for one fake approver and one conversation."""
    return issue_token(
        {
            "sub": str(user_id),
            "tenant_id": str(TENANT_ID),
            "department_id": str(DEPARTMENT_ID),
            "principals": ["manager"],
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


def _drive(scenario: Callable[[httpx.AsyncClient], Awaitable[httpx.Response]]) -> httpx.Response:
    async def runner() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await scenario(client)

    return asyncio.run(runner())


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _request(
    *,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    caller: ToolCaller,
    headers: Mapping[str, str] | None = None,
    body: Mapping[str, Any] | None = None,
) -> tuple[httpx.Response, CallerSpy]:
    """One call against the app, with the tool caller replaced at the seam.

    The seam is `deps.build_tool_caller` (decision C moved `_call` there), and
    the lifespan is NOT entered: these three routes read nothing off
    `app.state`, so entering it would buy a psycopg pool, a Redis client and a
    gateway client that no case here uses.
    """
    spy = CallerSpy(caller)
    monkeypatch.setattr(deps, "build_tool_caller", spy.build)

    async def scenario(client: httpx.AsyncClient) -> httpx.Response:
        return await client.request(
            method, path, headers=dict(headers or {}), json=None if body is None else dict(body)
        )

    return _drive(scenario), spy


# The three routes, as one table: every refusal below is asserted on all of
# them, because a guard present on two routes and missing on the third is
# exactly the hole a per-route case cannot see.
ROUTES: tuple[tuple[str, str, str, Mapping[str, Any] | None], ...] = (
    ("queue", "GET", APPROVALS_PATH, None),
    ("approve", "POST", APPROVE_PATH, {"comment": "Obviously fake comment."}),
    ("reject", "POST", REJECT_PATH, {"reason": "Obviously fake reason."}),
)
ROUTE_IDS = [case[0] for case in ROUTES]


# --- the happy paths ----------------------------------------------------------


def test_the_queue_route_answers_the_tools_output_unchanged(
    approvals_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a route that reshapes, filters or renames the tool's structured
    output on the way out (§1: "returns the tool's structured output
    unchanged"), and one that calls a different tool.

    The bearer half is a project rule and decision A: the token the caller
    presented is forwarded VERBATIM, so RBAC, the kill switch and the audit
    event all apply to this read. A route that minted its own service token,
    or reused a configured one, fails on the identity assertion alone; a route
    that passed the approver's id as an ARGUMENT fails on the key-set
    assertion, which is what a project rule forbids on a queue whose whole
    filter is "waiting for me".
    """
    caller = ScriptedToolCaller(QUEUE_PAYLOAD)
    token = _token()

    response, spy = _request(
        monkeypatch=monkeypatch,
        method="GET",
        path=f"{APPROVALS_PATH}?limit=25",
        caller=caller,
        headers=_auth(token),
    )

    assert response.status_code == 200
    assert response.json() == QUEUE_PAYLOAD
    assert caller.names == [LIST_PENDING_APPROVALS]
    _name, arguments = caller.calls[0]
    assert arguments == {"limit": 25}
    assert spy.bearer_tokens == [token]


def test_the_queue_route_sends_no_limit_when_none_is_asked_for(
    approvals_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a route that repeats the tool's `LIMIT_DEFAULT` as a number of
    its own (a second place for 50 to drift from, a project rule) and one
    that sends `limit: null` to a tool whose bound is 1..200 inclusive and
    would answer VALIDATION for it — the page would then show an empty queue
    for a caller with rows waiting.
    """
    caller = ScriptedToolCaller(QUEUE_PAYLOAD)

    response, _spy = _request(
        monkeypatch=monkeypatch,
        method="GET",
        path=APPROVALS_PATH,
        caller=caller,
        headers=_auth(_token()),
    )

    assert response.status_code == 200
    _name, arguments = caller.calls[0]
    assert arguments == {}


def test_the_approve_route_answers_the_tools_output_under_the_callers_own_bearer(
    approvals_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mutation half of a project rule and 6 at once.

    Catches a route that reshapes the decision's output, one that calls the
    wrong tool, one that forwards anything but the caller's own bearer, and —
    the assertion no other case here makes — one that passes an IDENTITY
    argument: the arguments are asserted as an EXACT key set, so an
    `approver_user_id` the browser could set fails, and so does a missing
    `idempotency_key`, which would leave a double-clicked Approve filing two
    decisions.
    """
    caller = ScriptedToolCaller(APPROVE_PAYLOAD)
    token = _token()

    response, spy = _request(
        monkeypatch=monkeypatch,
        method="POST",
        path=APPROVE_PATH,
        caller=caller,
        headers=_auth(token),
        body={"comment": "Obviously fake comment."},
    )

    assert response.status_code == 200
    assert response.json() == APPROVE_PAYLOAD
    assert caller.names == [APPROVE_REQUEST]
    _name, arguments = caller.calls[0]
    assert set(arguments) == {"request_id", "comment", "idempotency_key"}
    assert arguments["request_id"] == str(REQUEST_ID)
    assert arguments["comment"] == "Obviously fake comment."
    assert arguments["idempotency_key"] == derive_decision_key(
        approver_user_id=APPROVER_ID, request_id=REQUEST_ID, verb=APPROVE_VERB
    )
    assert spy.bearer_tokens == [token]


def test_the_reject_route_answers_the_tools_output_under_the_callers_own_bearer(
    approvals_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same four kills on the second decision, plus the one that separates
    it: the reason is forwarded as the tool's `reason` argument and the key is
    derived over the REJECT verb, so a route that reused the approve key would
    replay an approval's stored response for a rejection — the worst failure
    this derivation exists to prevent.
    """
    caller = ScriptedToolCaller(REJECT_PAYLOAD)
    token = _token()

    response, spy = _request(
        monkeypatch=monkeypatch,
        method="POST",
        path=REJECT_PATH,
        caller=caller,
        headers=_auth(token),
        body={"reason": "Obviously fake reason."},
    )

    assert response.status_code == 200
    assert response.json() == REJECT_PAYLOAD
    assert caller.names == [REJECT_REQUEST]
    _name, arguments = caller.calls[0]
    assert set(arguments) == {"request_id", "reason", "idempotency_key"}
    assert arguments["reason"] == "Obviously fake reason."
    assert arguments["idempotency_key"] == derive_decision_key(
        approver_user_id=APPROVER_ID, request_id=REQUEST_ID, verb=REJECT_VERB
    )
    assert arguments["idempotency_key"] != derive_decision_key(
        approver_user_id=APPROVER_ID, request_id=REQUEST_ID, verb=APPROVE_VERB
    )
    assert spy.bearer_tokens == [token]


def test_two_approvals_of_one_request_with_different_comments_send_one_key(
    approvals_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Decision B, the whole reason the key is derived rather than sent.

    Catches a key that includes the comment in its material, and a key minted
    fresh per call (`uuid4`): a double-clicked Approve would then arrive under
    two keys and file two decisions on one request. Only the key can flip this
    case — both calls carry the same bearer, the same path and the same verb,
    and differ in the comment alone.
    """
    caller = ScriptedToolCaller(APPROVE_PAYLOAD, APPROVE_PAYLOAD)
    token = _token()

    for comment in ("Obviously fake comment one.", "Obviously fake comment two."):
        response, _spy = _request(
            monkeypatch=monkeypatch,
            method="POST",
            path=APPROVE_PATH,
            caller=caller,
            headers=_auth(token),
            body={"comment": comment},
        )
        assert response.status_code == 200

    first, second = (arguments for _name, arguments in caller.calls)
    assert first["comment"] != second["comment"]
    assert first["idempotency_key"] == second["idempotency_key"]


# --- the refusals -------------------------------------------------------------


BAD_BEARERS: tuple[tuple[str, Mapping[str, str]], ...] = (
    ("no-header", {}),
    ("wrong-scheme", {"Authorization": "Basic ZmFrZTpmYWtl"}),
    ("empty-bearer", {"Authorization": "Bearer "}),
)


@pytest.mark.parametrize(("_id", "method", "path", "body"), ROUTES, ids=ROUTE_IDS)
@pytest.mark.parametrize(
    "headers", [case[1] for case in BAD_BEARERS], ids=[c[0] for c in BAD_BEARERS]
)
def test_an_absent_or_malformed_bearer_is_a_typed_401_and_opens_no_session(
    approvals_env: None,
    monkeypatch: pytest.MonkeyPatch,
    _id: str,
    method: str,
    path: str,
    body: Mapping[str, Any] | None,
    headers: Mapping[str, str],
) -> None:
    """Catches a route with no token check at all, and one that checks the
    token AFTER opening the MCP session: the caller is scripted with NOTHING,
    so a route that entered it or called a tool fails outright rather than
    showing up as a count afterwards.

    The token check is the only guard on this path — the body is well formed,
    the path parameter is a real uuid, and there is no rate limit and no DB
    read — so deleting it is what flips every row here.
    """
    caller = ScriptedToolCaller()

    response, spy = _request(
        monkeypatch=monkeypatch,
        method=method,
        path=path,
        caller=caller,
        headers=headers,
        body=body,
    )

    assert response.status_code == 401
    assert response.json() == {"code": ErrorCode.FORBIDDEN.value}
    assert caller.entered == 0
    assert caller.calls == []
    assert spy.bearer_tokens == []


@pytest.mark.parametrize(("_id", "method", "path", "body"), ROUTES, ids=ROUTE_IDS)
def test_a_forged_bearer_is_a_typed_401_and_opens_no_session(
    approvals_env: None,
    monkeypatch: pytest.MonkeyPatch,
    _id: str,
    method: str,
    path: str,
    body: Mapping[str, Any] | None,
) -> None:
    """Catches a route that reads the claims without VERIFYING the signature:
    this token is well formed and carries a real tenant, a real user and a
    real `sid` — it is signed with the wrong secret and nothing else, so only
    the verification can refuse it. A route that parsed the claims and trusted
    them would approve somebody's request on the say-so of a token anybody can
    write.
    """
    caller = ScriptedToolCaller()

    response, spy = _request(
        monkeypatch=monkeypatch,
        method=method,
        path=path,
        caller=caller,
        headers=_auth(_token(secret=OTHER_JWT_SECRET)),
        body=body,
    )

    assert response.status_code == 401
    assert response.json() == {"code": ErrorCode.FORBIDDEN.value}
    assert caller.entered == 0
    assert spy.bearer_tokens == []


@pytest.mark.parametrize(("_id", "method", "path", "body"), ROUTES, ids=ROUTE_IDS)
def test_an_expired_bearer_is_a_typed_401_and_opens_no_session(
    approvals_env: None,
    monkeypatch: pytest.MonkeyPatch,
    _id: str,
    method: str,
    path: str,
    body: Mapping[str, Any] | None,
) -> None:
    """Catches a verification that checks the SIGNATURE and not the expiry:
    this token is signed with the right secret and differs from the happy
    path's only in being nine hours old against an eight-hour `exp`, so the
    expiry comparison is the only thing that can refuse it. A stale token left
    deciding requests is a session that never ends.
    """
    caller = ScriptedToolCaller()

    response, spy = _request(
        monkeypatch=monkeypatch,
        method=method,
        path=path,
        caller=caller,
        headers=_auth(_token(issued_at=EXPIRED_ISSUED_AT)),
        body=body,
    )

    assert response.status_code == 401
    assert response.json() == {"code": ErrorCode.FORBIDDEN.value}
    assert caller.entered == 0
    assert spy.bearer_tokens == []


def test_the_shared_status_map_answers_a_matrix_refusal_403_and_a_kill_switch_503() -> None:
    """a project rule on the map decision C moves into `deps`.

    Catches the map copied from `chat_routes`, where FORBIDDEN is 401: on
    these routes a FORBIDDEN can only come from the RBAC matrix, and §1 says a
    matrix refusal is a 403. `TOOL_DISABLED` is pinned beside it because a
    tripped kill switch is "this decision is unavailable right now" and not a
    permission answer, and the missing `INTERNAL` row is what makes an
    unreachable server fall through to 500.
    """
    assert deps.STATUS_FOR_CODE[ErrorCode.FORBIDDEN] == 403
    assert deps.STATUS_FOR_CODE[ErrorCode.TOOL_DISABLED] == 503
    assert deps.STATUS_FOR_CODE[ErrorCode.VALIDATION] == 400
    assert ErrorCode.INTERNAL not in deps.STATUS_FOR_CODE


@pytest.mark.parametrize(
    ("error_class", "code", "status"),
    [
        pytest.param(RemoteForbiddenError, ErrorCode.FORBIDDEN, 403, id="forbidden-403"),
        pytest.param(RemoteToolDisabledError, ErrorCode.TOOL_DISABLED, 503, id="tool-disabled-503"),
    ],
)
@pytest.mark.parametrize(("_id", "method", "path", "body"), ROUTES, ids=ROUTE_IDS)
def test_a_tool_refusal_answers_its_own_typed_code(
    approvals_env: None,
    monkeypatch: pytest.MonkeyPatch,
    _id: str,
    method: str,
    path: str,
    body: Mapping[str, Any] | None,
    error_class: type[RemoteToolError],
    code: ErrorCode,
    status: int,
) -> None:
    """Catches a route that lets the tool's `ToolError` escape: FastAPI answers
    an untyped 500, so a permission decision the caller could act on, and an
    admin's kill switch the page should name, both read as an outage.

    The bearer here VERIFIES and the body is valid, so neither the token gate
    nor any validation can decide these rows — only the `except ToolError`
    branch and the shared map can.
    """
    caller = RefusingToolCaller(error_class(code.value))

    response, _spy = _request(
        monkeypatch=monkeypatch,
        method=method,
        path=path,
        caller=caller,
        headers=_auth(_token()),
        body=body,
    )

    assert response.status_code == status
    assert response.json() == {"code": code.value}
    assert caller.entered == 1


@pytest.mark.parametrize(("_id", "method", "path", "body"), ROUTES, ids=ROUTE_IDS)
def test_an_unreachable_mcp_server_is_one_typed_internal_naming_neither_host_nor_path(
    approvals_env: None,
    monkeypatch: pytest.MonkeyPatch,
    _id: str,
    method: str,
    path: str,
    body: Mapping[str, Any] | None,
) -> None:
    """Catches two failures at once: a handshake failure left to propagate (an
    untyped 500 with a traceback body), and a body that carries the client's
    message — `TRANSPORT_FAILURE_MESSAGE` is opaque on purpose, and a route
    rendering the exception's `str` would leak the MCP host and path to a
    browser. `INTERNAL` has no row in the map, so the 500 here also pins that
    the fall-through is the unreachable case and not a forgotten row.
    """
    caller = UnreachableToolCaller()

    response, _spy = _request(
        monkeypatch=monkeypatch,
        method=method,
        path=path,
        caller=caller,
        headers=_auth(_token()),
        body=body,
    )

    assert response.status_code == 500
    assert response.json() == {"code": ErrorCode.INTERNAL.value}
    assert MCP_HOST not in response.text
    assert MCP_PATH not in response.text
    assert TRANSPORT_FAILURE_MESSAGE not in response.text


@pytest.mark.parametrize("verb", [APPROVE_VERB, REJECT_VERB], ids=[APPROVE_VERB, REJECT_VERB])
def test_a_non_uuid_request_id_reaches_no_tool_and_no_key(
    approvals_env: None, monkeypatch: pytest.MonkeyPatch, verb: str
) -> None:
    """§1's second input-validation criterion. Catches a `request_id` declared
    as a `str` and passed straight through: it would reach the tool as a
    string the MCP input model refuses, but it would FIRST reach
    `derive_decision_key`, which would happily digest `"../../etc"` into a
    perfectly valid key and file it against nothing.

    The bearer verifies here, so the token gate cannot produce this refusal —
    only the path parameter's type can.
    """
    caller = ScriptedToolCaller()

    response, spy = _request(
        monkeypatch=monkeypatch,
        method="POST",
        path=f"/approvals/not-a-uuid/{verb}",
        caller=caller,
        headers=_auth(_token()),
        body={"comment": "Obviously fake comment."}
        if verb == APPROVE_VERB
        else {"reason": "Obviously fake reason."},
    )

    assert response.status_code == 422
    assert caller.entered == 0
    assert caller.calls == []
    assert spy.bearer_tokens == []


# --- the comment and reason bounds (decision E) -------------------------------


def test_a_comment_of_exactly_the_tools_maximum_is_accepted(
    approvals_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The INCLUSIVE end of §1's bound, and the case decision E exists for.

    Catches a route that re-declares the cap on a body model of its own: a
    `max_length=2000` written as `<` , a `constr(max_length=1999)`, or any
    second opinion, refuses this comment with FastAPI's untyped 422 while the
    tool would have accepted it. Nothing else in this file sends a comment at
    the boundary, and the refusal case below cannot tell an inclusive bound
    from an exclusive one on its own.
    """
    caller = BoundCheckingToolCaller(APPROVE_PAYLOAD)
    comment = "x" * COMMENT_MAX_LENGTH

    response, _spy = _request(
        monkeypatch=monkeypatch,
        method="POST",
        path=APPROVE_PATH,
        caller=caller,
        headers=_auth(_token()),
        body={"comment": comment},
    )

    assert response.status_code == 200
    assert len(caller.decisions) == 1
    assert caller.decisions[0]["comment"] == comment


def test_a_comment_one_character_over_the_maximum_is_a_typed_400_with_no_decision(
    approvals_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other end of the same bound. Catches a route that answers the
    tool's typed `VALIDATION` as an untyped 500 or as FastAPI's 422 — the page
    has one sentence per the design code and nothing to say for either — and a
    route that swallows the refusal and answers 200, which would tell an
    approver their decision was filed when nothing was.
    """
    caller = BoundCheckingToolCaller(APPROVE_PAYLOAD)

    response, _spy = _request(
        monkeypatch=monkeypatch,
        method="POST",
        path=APPROVE_PATH,
        caller=caller,
        headers=_auth(_token()),
        body={"comment": "x" * (COMMENT_MAX_LENGTH + 1)},
    )

    assert response.status_code == 400
    assert response.json() == {"code": ErrorCode.VALIDATION.value}
    assert caller.decisions == []


def test_an_empty_reason_is_a_typed_400_with_no_decision(
    approvals_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`REASON_MIN_LENGTH` is 1, so an empty reason is the lower boundary of
    the same bound. Catches a route that drops an empty string to `None` on
    the way out — the tool would then refuse a MISSING field and the browser
    would be told the request was malformed rather than that a reason is
    required — and the same 400-not-500 mapping as above, on the route where a
    rejection with no recorded reason is an audit record with a hole in it.
    """
    caller = BoundCheckingToolCaller(REJECT_PAYLOAD)

    response, _spy = _request(
        monkeypatch=monkeypatch,
        method="POST",
        path=REJECT_PATH,
        caller=caller,
        headers=_auth(_token()),
        body={"reason": ""},
    )

    assert response.status_code == 400
    assert response.json() == {"code": ErrorCode.VALIDATION.value}
    assert caller.decisions == []


def test_a_reason_of_exactly_the_tools_maximum_is_accepted(
    approvals_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The inclusive upper end on the reject route, for the same kill as the
    comment case: a second bound declared here would refuse at 2000 a reason
    the tool accepts, and the page would lose a long rejection somebody wrote.
    """
    caller = BoundCheckingToolCaller(REJECT_PAYLOAD)
    reason = "y" * REASON_MAX_LENGTH

    response, _spy = _request(
        monkeypatch=monkeypatch,
        method="POST",
        path=REJECT_PATH,
        caller=caller,
        headers=_auth(_token()),
        body={"reason": reason},
    )

    assert response.status_code == 200
    assert len(caller.decisions) == 1
    assert caller.decisions[0]["reason"] == reason


# --- the derived key, pure (decision B) ---------------------------------------


def test_the_same_approver_request_and_verb_derive_the_same_key_every_time() -> None:
    """The property the whole decision rests on. Catches a derivation that
    reaches a clock or a random source (`uuid4`, `datetime.now`): the second
    Approve of a double click would then file a second decision instead of
    replaying the first one's stored response.
    """
    first = derive_decision_key(
        approver_user_id=APPROVER_ID, request_id=REQUEST_ID, verb=APPROVE_VERB
    )
    second = derive_decision_key(
        approver_user_id=APPROVER_ID, request_id=REQUEST_ID, verb=APPROVE_VERB
    )

    assert first == second


@pytest.mark.parametrize(
    ("approver", "request_id", "verb"),
    [
        pytest.param(OTHER_APPROVER_ID, REQUEST_ID, APPROVE_VERB, id="another-approver"),
        pytest.param(APPROVER_ID, OTHER_REQUEST_ID, APPROVE_VERB, id="another-request"),
        pytest.param(APPROVER_ID, REQUEST_ID, REJECT_VERB, id="another-verb"),
    ],
)
def test_changing_any_one_of_the_three_values_changes_the_key(
    approver: uuid.UUID, request_id: uuid.UUID, verb: str
) -> None:
    """§5's last risk, one value at a time. Each row is the failure the value
    prevents: without the approver two approvers deciding one request share a
    key and the second replays the first's answer; without the request id one
    approver's second decision replays their first, on a DIFFERENT request;
    without the verb an Approve and a Reject on one row collapse into whichever
    was filed first.
    """
    baseline = derive_decision_key(
        approver_user_id=APPROVER_ID, request_id=REQUEST_ID, verb=APPROVE_VERB
    )

    assert (
        derive_decision_key(approver_user_id=approver, request_id=request_id, verb=verb) != baseline
    )


def test_the_key_is_the_uuid5_of_the_three_values_under_the_modules_namespace() -> None:
    """Catches a derivation over the right three values under the WRONG
    namespace or joined with a different separator: it would be stable, unique
    and completely different from the key `graph/keys.py` documents, so every
    other case in this file passes while the module has two derivations under
    two namespaces (decision B's whole reason for putting this function in
    that module).

    The separator is asserted by construction: a join on `\\x1f` is the only
    one under which the pair below cannot collide with any other pair of
    uuids, and a derivation concatenating the three values bare fails here.
    """
    expected = str(
        uuid.uuid5(KEY_NAMESPACE, SEPARATOR.join([str(APPROVER_ID), str(REQUEST_ID), APPROVE_VERB]))
    )

    assert (
        derive_decision_key(approver_user_id=APPROVER_ID, request_id=REQUEST_ID, verb=APPROVE_VERB)
        == expected
    )


def test_the_derived_key_fits_the_column_it_is_stored_in() -> None:
    """§1's length half: the key is length-checked by `check_idempotency_key`
    before it reaches the wire. Catches a derivation that returns the MATERIAL
    rather than a uuid over it — a string that grows with the verb and would
    abort the store AFTER the decision had run, as a database error instead of
    the code the design names.
    """
    key = derive_decision_key(approver_user_id=APPROVER_ID, request_id=REQUEST_ID, verb=REJECT_VERB)

    assert len(key) == len(str(uuid.uuid5(KEY_NAMESPACE, "fake")))
    assert 0 < len(key) <= KEY_LENGTH
