"""The kill switch board and the toggle, over HTTP.

Surface under test (written before it exists; every case below is red on one
of these names until steps 1 and 2 land):

``agent_api.killswitch_routes``
    ``router`` with ``GET /admin/killswitch`` (no parameter at all, tool
    ``list_kill_switches``) and ``POST /admin/killswitch`` (a body of exactly
    ``tool_name``, ``enabled``, ``scope``, ``reason``, tool
    ``set_kill_switch``), registered in ``main.py``. Both go through
    ``agent_api.deps``' existing seam - ``authenticated``, ``unauthenticated``,
    ``typed_refusal``, ``call_tool_route`` and ``STATUS_FOR_CODE``
    - which is why every case here patches ``deps.build_tool_caller`` and none
    of them opens a socket.

``agent_api.deps``
    ``STATUS_FOR_CODE`` gains ``ErrorCode.POLICY_BLOCKED: 409`` (the fifth row; the map later grew
    to all eight codes, incl
    ``INTERNAL`` 500).

Shapes this file fixes that the plan left open:
- the tool names are ``list_kill_switches`` and ``set_kill_switch``
, their own registry names.
- the refusal envelope is ``{"code": "<ErrorCode value>"}`` and the bearer
  refusal a literal ``401``, the usual shape, unchanged.
- the board's arguments are an EXACTLY empty dict, the zero-argument form
  ``me_routes`` uses; ``list_kill_switches`` declares no field and forbids
  extras, so any key at all crosses back as a ``VALIDATION`` from the far side.
- the set route's arguments are exactly the five keys of
  ``SetKillSwitchInput``: the four the browser names plus an
  ``idempotency_key`` the ROUTE derives (the browser no longer picks it), and no tenant or user id.
- ``scope`` on the wire out of the BROWSER is the literal ``"global"`` or the
  literal ``"tenant"``; ``scope`` on the wire INTO the tool is ``"global"`` or
  the token's own tenant id, rendered as its string. The
  substitution is asserted against the uuid read back off the fixture's own
  token rather than against a module literal.
- the two client-supplied strings that land in a store keep the TOOL's own
  bounds, imported from ``helpdesk.killswitch`` and never retyped:
  ``TOOL_NAME_MAX_LENGTH`` is 64 and ``REASON_MAX_LENGTH`` is 500. Both limits require the refusal
  to be
  the TYPED ``400 VALIDATION`` and never FastAPI's untyped 422, and to make no
  tool call at all - so the refusal rows below drive a caller scripted with NO
  answers, and the accepted rows drive a caller that applies
  ``SetKillSwitchInput`` itself, which is the only way "what the route sent is
  storable" can be asserted without an MCP server.
- ``enabled`` describes the SWITCH and ``stopped`` describes the TOOL. The polarity cases drive a
caller composing
  ``SetKillSwitchOutput`` the way the tool does, with a sentinel
  ``changed_at`` the route has no way to invent, so a route answering a body
  of its own rather than the tool's fails them.

Cost and isolation: NO backing service at all, exactly as
``test_audit_routes.py``. The lifespan is never entered - neither route reads
anything off ``app.state`` - so nothing here opens a socket, a pool or a Redis
connection. Fixtures live in this module rather than a conftest, for the same
reason they do there.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from pydantic import BaseModel, ValidationError

from helpdesk.agent_api.api.router import app
from helpdesk.agent_api.core import deps
from helpdesk.agent_api.services import tool_proxy
from helpdesk.agent_api.services.mcp_client import (
    TRANSPORT_FAILURE_MESSAGE,
    RemoteForbiddenError,
    RemoteInternalError,
    RemotePolicyBlockedError,
    RemoteToolDisabledError,
    RemoteToolError,
    RemoteValidationError,
    ToolCaller,
)
from helpdesk.mcp_server.tools.killswitch import SetKillSwitchInput
from helpdesk.shared.auth.jwt import issue_token, verify_token
from helpdesk.shared.core.config import Settings
from helpdesk.shared.core.errors import ErrorCode
from helpdesk.shared.models.idempotency import KEY_LENGTH
from helpdesk.shared.services.killswitch import REASON_MAX_LENGTH, TOOL_NAME_MAX_LENGTH

LIST_KILL_SWITCHES = "list_kill_switches"
SET_KILL_SWITCH = "set_kill_switch"

KILLSWITCH_PATH = "/admin/killswitch"

# Obviously fake, tests only: none of these is a credential, and
# nothing in this file connects to any of these hosts.
TEST_JWT_SIGNING_PHRASE = "killswitch-routes-test-jwt-secret-0123456789"
OTHER_JWT_SECRET = "another-killswitch-routes-fake-secret-01234"
MCP_URL = "http://mcp.invalid:8081/mcp"
MCP_HOST = "mcp.invalid"
MCP_PATH = "/mcp"
DATABASE_URL = "postgresql+asyncpg://fake:fake@db.invalid:5432/fake_killswitch"
REDIS_URL = "redis://redis.invalid:6379/0"
QDRANT_URL = "http://qdrant.invalid:6333"

TENANT_ID = uuid.UUID("11111111-1111-5111-8111-111111111111")
ADMIN_ID = uuid.UUID("22222222-2222-5222-8222-222222222222")
DEPARTMENT_ID = uuid.UUID("33333333-3333-5333-8333-333333333333")
SESSION_ID = uuid.UUID("44444444-4444-5444-8444-444444444444")

# A token issued nine hours ago: `issue_token` gives an 8-hour `exp`, so this
# one verifies its signature and fails on expiry alone.
EXPIRED_ISSUED_AT = datetime.now(UTC) - timedelta(hours=9)

# The two literals on the browser's side of the wire, and the
# one the tool answers a platform-wide switch with.
GLOBAL_SCOPE = "global"
TENANT_SCOPE = "tenant"

# A tool name and a reason well inside both bounds, for every case that is not
# about the bounds themselves. The name matches the shape every registered tool
# has, so nothing here depends on a tool name the registry would refuse.
A_TOOL_NAME = "grant_access"
A_REASON = "Obviously fake reason, tests only."

# The instant the staged caller stamps a change with. The route cannot invent
# this value, which is what makes the polarity cases fail against a route
# answering a body it assembled itself.
CHANGED_AT = "2026-09-13T10:15:00Z"

BOARD_PAYLOAD: Mapping[str, Any] = {
    "switches": [
        {
            "tool": "grant_access",
            "protected": False,
            "stopped": False,
            "global_state": None,
            "tenant_state": None,
            "unreadable": False,
        },
        {
            "tool": "revoke_access",
            "protected": True,
            "stopped": True,
            "global_state": {
                "reason": "Obviously fake platform stop.",
                "actor": str(ADMIN_ID),
                "at": "2026-09-12T08:00:00Z",
            },
            "tenant_state": {
                "reason": "Obviously fake tenant stop.",
                "actor": str(ADMIN_ID),
                "at": "2026-09-12T09:00:00Z",
            },
            "unreadable": False,
        },
    ]
}
"""One board exactly as `list_kill_switches` answers it: the envelope, and two
`KillSwitchRow`s carrying their six fields, one clean and one stopped on both
layers. The route must hand this back UNCHANGED, so the case asserting
equality is what catches a router that flattens the two layers into one
effective state, drops `protected` or renames a field on the way out."""


# --- the scripted seam --------------------------------------------------------


class ScriptedToolCaller:
    """A `ToolCaller` answering one scripted payload per call, in order.

    The same stub shape `test_me_routes.py`, `test_approval_routes.py`,
    `test_review_routes.py` and `test_audit_routes.py` use: a
    call past the last scripted answer fails the test outright, which is how
    "the route opened no session at all" is asserted as a hard stop rather than
    as a count read afterwards.
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


class SwitchingToolCaller:
    """`set_kill_switch`'s own input model and its own output composition.

    The input half is a known risk: `SetKillSwitchInput` is the one
    authority on whether a `tool_name` and a `reason` are storable, so an
    accepted boundary row can only pass if what the route SENT satisfies the
    tool's model - which is what "the pre-check is the tool's own" means when
    no MCP server is running.

    The output half is read off `killswitch.py`: `stopped` is the
    ECHO of `enabled`, and `changed_at` is the instant the tool stamped the
    change with. The sentinel below is a value the route has no way to invent,
    so a route rendering a body of its own instead of the tool's fails the
    polarity cases rather than agreeing with them by accident.
    """

    def __init__(self) -> None:
        self.answered: list[dict[str, Any]] = []
        self.entered = 0

    async def __aenter__(self) -> ToolCaller:
        self.entered += 1
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def call_tool(self, name: str, arguments: BaseModel) -> Mapping[str, Any]:
        sent = arguments.model_dump(mode="json")
        try:
            validated = SetKillSwitchInput.model_validate(sent)
        except ValidationError as exc:
            raise RemoteValidationError("the switch arguments are out of bounds") from exc
        self.answered.append(sent)
        return {
            "tool_name": validated.tool_name,
            "stopped": validated.enabled,
            "scope": GLOBAL_SCOPE if validated.scope == GLOBAL_SCOPE else str(validated.scope),
            "changed_at": CHANGED_AT,
        }


@pytest.fixture
def killswitch_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Obviously fake settings. Nothing here is dialled: no lifespan is entered."""
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    monkeypatch.setenv("QDRANT_URL", QDRANT_URL)
    monkeypatch.setenv("JWT_SECRET", TEST_JWT_SIGNING_PHRASE)
    monkeypatch.setenv("MCP_URL", MCP_URL)


def _token(
    *,
    user_id: uuid.UUID = ADMIN_ID,
    secret: str = TEST_JWT_SIGNING_PHRASE,
    issued_at: datetime | None = None,
) -> str:
    """One verified-looking token for one fake platform admin and one session."""
    return issue_token(
        {
            "sub": str(user_id),
            "tenant_id": str(TENANT_ID),
            "department_id": str(DEPARTMENT_ID),
            "principals": ["platform_admin"],
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


def _body(
    *,
    tool_name: str = A_TOOL_NAME,
    enabled: bool = True,
    scope: str = GLOBAL_SCOPE,
    reason: str = A_REASON,
) -> dict[str, Any]:
    """The browser's half of a toggle: exactly the four keys, by default valid."""
    return {"tool_name": tool_name, "enabled": enabled, "scope": scope, "reason": reason}


def _get(
    *,
    monkeypatch: pytest.MonkeyPatch,
    caller: ToolCaller,
    headers: Mapping[str, str] | None = None,
) -> tuple[httpx.Response, CallerSpy]:
    """One board read against the app, with the tool caller replaced at the seam.

    The seam is `deps.build_tool_caller` (`call_tool_route` is built there), and the lifespan is NOT
    entered: this route reads nothing off
    `app.state`, so entering it would buy a psycopg pool, a Redis client and a
    gateway client that no case here uses.
    """
    spy = CallerSpy(caller)
    monkeypatch.setattr(tool_proxy, "build_tool_caller", spy.build)

    async def scenario(client: httpx.AsyncClient) -> httpx.Response:
        return await client.get(KILLSWITCH_PATH, headers=dict(headers or {}))

    return _drive(scenario), spy


def _post(
    *,
    monkeypatch: pytest.MonkeyPatch,
    caller: ToolCaller,
    headers: Mapping[str, str] | None = None,
    body: Mapping[str, Any] | None = None,
) -> tuple[httpx.Response, CallerSpy]:
    """One toggle against the app, through the same patched seam."""
    spy = CallerSpy(caller)
    monkeypatch.setattr(tool_proxy, "build_tool_caller", spy.build)

    async def scenario(client: httpx.AsyncClient) -> httpx.Response:
        return await client.post(
            KILLSWITCH_PATH,
            headers=dict(headers or {}),
            json=None if body is None else dict(body),
        )

    return _drive(scenario), spy


def _tenant_id_off_the_token(token: str) -> str:
    """The tenant the FIXTURE's own token carries, read back through the verifier.

    The expected value of the `"tenant"` substitution is read
    off the claims the route itself will derive it from, never written as a
    second literal beside `TENANT_ID`.
    """
    return verify_token(token, TEST_JWT_SIGNING_PHRASE, datetime.now(UTC)).tenant_id


# --- the board (GET) ----------------------------------------------------------


def test_the_board_route_answers_the_tools_output_unchanged(
    killswitch_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a route that reshapes, filters or renames `list_kill_switches`'
    structured output on the way out (the tool "returns the tool's structured
    output unchanged", each row carrying its six fields), and one that calls a
    different tool.

    The reshape that matters most here is collapsing `global_state` and
    `tenant_state` into one effective state: the tool carries both
    because an admin clearing the tenant switch has to be told the global one
    still holds the tool down, and a route that merged them would leave the
    page unable to say so.

    The bearer half is the identity-from-token rule: the token the caller
    presented is forwarded VERBATIM, so RBAC and the kill switch apply to this
    read. A route that minted its own service token, or reused a configured
    one, fails on the identity assertion alone.
    """
    caller = ScriptedToolCaller(BOARD_PAYLOAD)
    token = _token()

    response, spy = _get(monkeypatch=monkeypatch, caller=caller, headers=_auth(token))

    assert response.status_code == 200
    assert response.json() == BOARD_PAYLOAD
    assert caller.names == [LIST_KILL_SWITCHES]
    assert spy.bearer_tokens == [token]


def test_the_board_route_sends_an_exactly_empty_argument_set(
    killswitch_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second requirement, the identity-from-token rule, as an EXACT key set.

    Catches a route that passes the caller's tenant, user or a tool name along
    as an argument. `ListKillSwitchesInput` declares no field and forbids
    extras, so a key riding along would cross back as a `VALIDATION` and the
    board would answer an error on every load - and a `tenant_id` in
    particular is the browser asking to read another tenant's board, which the
    token is the only authority on (nothing here depends on a
    cross-tenant guard, because the argument must not exist at all).
    """
    caller = ScriptedToolCaller(BOARD_PAYLOAD)

    response, _spy = _get(monkeypatch=monkeypatch, caller=caller, headers=_auth(_token()))

    assert response.status_code == 200
    _name, arguments = caller.calls[0]
    assert arguments == {}


# --- the toggle (POST), its arguments -----------------------------------------


def test_the_set_route_sends_exactly_five_keys_for_a_global_switch(
    killswitch_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The third, fourth and seventh criteria at once.

    Catches three routes: one that sends NO `idempotency_key` (`SetKillSwitchInput` is a
    `MutatingToolInput` and forbids extras, so the
    toggle would answer `VALIDATION` on every click), one that passes the
    caller's tenant or user id along as an argument, and one that
    translates `"global"` into the token's tenant - which would turn an admin's
    platform-wide stop into one tenant's and leave every other tenant running a
    tool somebody decided to stop (the second risk).

    The argument set is asserted as the exact dict, so a field riding along
    fails the case too.
    """
    caller = ScriptedToolCaller(
        {"tool_name": A_TOOL_NAME, "stopped": True, "scope": GLOBAL_SCOPE, "changed_at": CHANGED_AT}
    )
    token = _token()

    response, spy = _post(
        monkeypatch=monkeypatch,
        caller=caller,
        headers=_auth(token),
        body=_body(scope=GLOBAL_SCOPE),
    )

    assert response.status_code == 200
    assert caller.names == [SET_KILL_SWITCH]
    name, arguments = caller.calls[0]
    assert name == SET_KILL_SWITCH
    key = arguments.pop("idempotency_key")
    assert isinstance(key, str)
    assert 1 <= len(key) <= KEY_LENGTH
    assert arguments == {
        "tool_name": A_TOOL_NAME,
        "enabled": True,
        "scope": GLOBAL_SCOPE,
        "reason": A_REASON,
    }
    assert spy.bearer_tokens == [token]


def test_the_set_route_substitutes_the_tokens_own_tenant_for_the_literal_tenant(
    killswitch_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fourth requirement, the other direction.

    Catches a route that forwards the literal string `"tenant"` (which
    `SetKillSwitchInput` refuses, so a tenant-scoped toggle would never work at
    all), one that substitutes a CONSTANT or a configured tenant rather than
    this caller's, and one that translates `"tenant"` into `"global"` - which
    would take every tenant's tool down when an admin meant their own (section
    5's second risk).

    The expected uuid is read back off the fixture's own token through
    `verify_token`, never written as a literal beside it, so
    a route reading the tenant from anywhere but the verified claims fails the
    case however the fixture is later re-seeded.
    """
    caller = ScriptedToolCaller(
        {
            "tool_name": A_TOOL_NAME,
            "stopped": True,
            "scope": str(TENANT_ID),
            "changed_at": CHANGED_AT,
        }
    )
    token = _token()

    response, _spy = _post(
        monkeypatch=monkeypatch,
        caller=caller,
        headers=_auth(token),
        body=_body(scope=TENANT_SCOPE),
    )

    assert response.status_code == 200
    _name, arguments = caller.calls[0]
    assert arguments["scope"] == _tenant_id_off_the_token(token)
    assert set(arguments) == {"tool_name", "enabled", "scope", "reason", "idempotency_key"}


@pytest.mark.parametrize("enabled", [True, False], ids=["stop-the-tool", "let-it-run"])
def test_the_set_route_answers_the_polarity_the_tool_decided(
    killswitch_env: None, monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    """Both directions.

        Catches a route that NEGATES `enabled` between the body and the tool
        argument: `enabled` describes the switch and `stopped` describes the tool
    , so an inverted route would stop a tool when an admin
        meant to release it - the worst possible direction for this particular
        control, and the one 24d's toggle inherits from here.

        It also catches a route that renders a body of its OWN rather than the
        tool's: the staged caller stamps `changed_at` with a sentinel instant the
        route has no way to invent, so a route assembling the answer from the
        request it just received cannot produce this body at all.
    """
    caller = SwitchingToolCaller()

    response, _spy = _post(
        monkeypatch=monkeypatch,
        caller=caller,
        headers=_auth(_token()),
        body=_body(enabled=enabled),
    )

    assert response.status_code == 200
    assert response.json() == {
        "tool_name": A_TOOL_NAME,
        "stopped": enabled,
        "scope": GLOBAL_SCOPE,
        "changed_at": CHANGED_AT,
    }
    assert len(caller.answered) == 1
    assert caller.answered[0]["enabled"] is enabled


# --- the one bound this router owns: the scope enum ---------------------------


OUT_OF_ENUM_SCOPES: tuple[tuple[str, str], ...] = (
    ("a-well-formed-uuid", str(TENANT_ID)),
    ("another-tenants-uuid", "99999999-9999-5999-8999-999999999999"),
    ("the-wrong-case", "GLOBAL"),
    ("a-trailing-space", "tenant "),
    ("the-empty-string", ""),
    ("a-word-of-its-own", "everything"),
)


@pytest.mark.parametrize(
    "scope",
    [case[1] for case in OUT_OF_ENUM_SCOPES],
    ids=[case[0] for case in OUT_OF_ENUM_SCOPES],
)
def test_a_scope_outside_the_two_literals_is_a_typed_400_and_opens_no_session(
    killswitch_env: None, monkeypatch: pytest.MonkeyPatch, scope: str
) -> None:
    """A known risk.

    The two uuid rows are the dangerous ones: a route that accepted a tenant id
    from the BODY would let the browser name the tenant a switch is written
    for, which the identity-from-token rule gives to the token alone. The tool would refuse a
    foreign tenant `FORBIDDEN`, but the caller's own tenant would be honoured -
    so a route forwarding a uuid is a client-supplied tenant that works, and
    nothing further down would ever report it.

    The other rows catch a route that compares the scope loosely (case-folded,
    stripped or by prefix) rather than against the two literals.

    Every row also catches the refusal answered as FastAPI's untyped 422: the
    body model is this router's, so a route that let the framework refuse the
    field would answer a `detail`/`loc` body where the spec's
    `VALIDATION` dressed as a 400 is what 24d has a sentence for. The caller is
    scripted with NOTHING, so a route that entered it or called a tool fails
    outright rather than showing up as a count afterwards.
    """
    caller = ScriptedToolCaller()

    response, spy = _post(
        monkeypatch=monkeypatch,
        caller=caller,
        headers=_auth(_token()),
        body=_body(scope=scope),
    )

    assert response.status_code == 400
    assert response.json() == {"code": ErrorCode.VALIDATION.value}
    assert caller.entered == 0
    assert caller.calls == []
    assert spy.bearer_tokens == []


def test_a_tenant_id_riding_along_in_the_body_is_a_typed_400_and_opens_no_session(
    killswitch_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fourth requirement, last sentence ("a tenant id in the body is
    not accepted by the model at all").

    Catches a body model declared `extra="ignore"`: the toggle would answer
    **200** for a request that named a tenant, so an admin who believed they
    had stopped a tool for one tenant would be told they had, while the switch
    was written wherever `scope` pointed. A refusal is the only honest answer
    to a field this route will not honour.

    The four legal keys here are all valid and the bearer verifies, so only the
    extra key can decide this row.
    """
    caller = ScriptedToolCaller()

    response, _spy = _post(
        monkeypatch=monkeypatch,
        caller=caller,
        headers=_auth(_token()),
        body={**_body(), "tenant_id": str(TENANT_ID)},
    )

    assert response.status_code == 400
    assert response.json() == {"code": ErrorCode.VALIDATION.value}
    assert caller.entered == 0
    assert caller.calls == []


# --- the derived key ----------------------------------------------------------


def _sent_keys(
    monkeypatch: pytest.MonkeyPatch, token: str, *bodies: Mapping[str, Any]
) -> list[str]:
    """The idempotency key the route sent for each body, one fresh call each."""
    keys: list[str] = []
    for body in bodies:
        caller = ScriptedToolCaller(
            {
                "tool_name": A_TOOL_NAME,
                "stopped": True,
                "scope": GLOBAL_SCOPE,
                "changed_at": CHANGED_AT,
            }
        )
        response, _spy = _post(
            monkeypatch=monkeypatch, caller=caller, headers=_auth(token), body=body
        )
        assert response.status_code == 200
        keys.append(str(caller.calls[0][1]["idempotency_key"]))
    return keys


def test_the_set_route_derives_the_key_and_the_body_still_forbids_one(
    killswitch_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches four routes. One that forwards no key (the tool now demands one,
    so every click would answer `VALIDATION`). One that mints a fresh key per
    call (`uuid4`), so a double-clicked toggle runs twice instead of replaying.
    One whose key leaves out the tool, the direction or the scope, so a clear
    would replay the stored STOP (or a tenant toggle the platform-wide one).
    And one that lets the browser name the key: the body model must keep
    `extra="forbid"`, because a browser-chosen key is a replay the caller picks.

    Each difference below changes exactly one body field and the same bearer is
    used throughout, so only the key's material can flip a row.
    """
    token = _token()
    base = _body()

    same_a, same_b, other_tool, other_direction, other_scope = _sent_keys(
        monkeypatch,
        token,
        base,
        base,
        _body(tool_name="revoke_access"),
        _body(enabled=False),
        _body(scope=TENANT_SCOPE),
    )

    assert same_a == same_b, "the same toggle twice must derive the same key"
    assert len({same_a, other_tool, other_direction, other_scope}) == 4

    caller = ScriptedToolCaller()
    response, _spy = _post(
        monkeypatch=monkeypatch,
        caller=caller,
        headers=_auth(token),
        body={**base, "idempotency_key": "fake-browser-chosen-key"},
    )

    assert response.status_code == 400
    assert response.json() == {"code": ErrorCode.VALIDATION.value}
    assert caller.entered == 0
    assert caller.calls == []


# --- the two stored strings, pinned THROUGH the route -------------------------


def test_a_tool_name_of_exactly_sixty_four_characters_is_accepted(
    killswitch_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The INCLUSIVE end of `TOOL_NAME_MAX_LENGTH`, which is 64 (the constant is IMPORTED
    from `helpdesk.killswitch`, never retyped).

    Catches a route that re-declares the cap one short of the store's own - a
    `max_length=63`, or any second opinion narrower than `key_for`'s - which
    would refuse a name the tool and the Redis key both accept. Nothing else in
    this file sends a value at a cap, and the refusal row below cannot tell an
    inclusive bound from an exclusive one on its own.

    The staged caller applies `SetKillSwitchInput` itself, so the row also
    pins that what the route SENT is storable by the tool rather than merely
    accepted here.
    """
    name = "a" * TOOL_NAME_MAX_LENGTH
    caller = SwitchingToolCaller()

    response, _spy = _post(
        monkeypatch=monkeypatch,
        caller=caller,
        headers=_auth(_token()),
        body=_body(tool_name=name),
    )

    assert TOOL_NAME_MAX_LENGTH == 64
    assert response.status_code == 200
    assert len(caller.answered) == 1
    assert caller.answered[0]["tool_name"] == name


def test_a_tool_name_one_character_over_the_cap_is_a_typed_400_with_no_call(
    killswitch_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other end of the same boundary, one character outside it.

    Catches a route with no pre-check at all: the name would travel to the far
    side, where the tool's model refuses it - but the route must give the typed
    400 and NO tool call, because `tool_name` is a Redis key segment and a
    client-supplied string that lands in a store gets its pre-check before any
    effect runs. It also catches the refusal answered as
    FastAPI's untyped 422, which 24d has no sentence for.

    The caller is scripted with NOTHING, so entering it or calling a tool fails
    the case outright.
    """
    caller = ScriptedToolCaller()

    response, _spy = _post(
        monkeypatch=monkeypatch,
        caller=caller,
        headers=_auth(_token()),
        body=_body(tool_name="a" * (TOOL_NAME_MAX_LENGTH + 1)),
    )

    assert response.status_code == 400
    assert response.json() == {"code": ErrorCode.VALIDATION.value}
    assert caller.entered == 0
    assert caller.calls == []


@pytest.mark.parametrize("length", [1, REASON_MAX_LENGTH], ids=["reason-1", "reason-500"])
def test_the_inclusive_ends_of_the_reason_bound_are_accepted(
    killswitch_env: None, monkeypatch: pytest.MonkeyPatch, length: int
) -> None:
    """Both INCLUSIVE ends of `REASON_MAX_LENGTH`, which is 500 (the constant is IMPORTED
    from `helpdesk.killswitch`).

    Catches a route re-declaring either end narrower than the store's - a
    `min_length=2` or a `max_length=499` - each of which refuses a reason the
    stored `SwitchState` accepts, with an untyped 422 and on the one surface
    where the reason is the whole audit record of why a tool was stopped.
    """
    reason = "r" * length
    caller = SwitchingToolCaller()

    response, _spy = _post(
        monkeypatch=monkeypatch,
        caller=caller,
        headers=_auth(_token()),
        body=_body(reason=reason),
    )

    assert REASON_MAX_LENGTH == 500
    assert response.status_code == 200
    assert len(caller.answered) == 1
    assert caller.answered[0]["reason"] == reason


@pytest.mark.parametrize(
    "reason", ["", "r" * (REASON_MAX_LENGTH + 1)], ids=["reason-0", "reason-501"]
)
def test_a_reason_outside_the_bound_is_a_typed_400_with_no_call(
    killswitch_env: None, monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    """One step outside each end of the same bound.

    Catches a route that forwards a reason the store cannot hold and answers
    the far side's refusal as an untyped 500 or as FastAPI's 422, and a route
    that TRUNCATES the over-long one into the cap - which would store a
    sentence the admin did not write as the reason a tool is off. The empty row
    catches a route that drops a blank reason to `None` or to a default of its
    own, so a stop would be recorded with nobody's explanation.

    The caller is scripted with NOTHING, so no effect can have run.
    """
    caller = ScriptedToolCaller()

    response, _spy = _post(
        monkeypatch=monkeypatch,
        caller=caller,
        headers=_auth(_token()),
        body=_body(reason=reason),
    )

    assert response.status_code == 400
    assert response.json() == {"code": ErrorCode.VALIDATION.value}
    assert caller.entered == 0
    assert caller.calls == []


# --- the refusals, over both verbs --------------------------------------------


VERBS: tuple[tuple[str, str], ...] = (("board", "GET"), ("set", "POST"))
VERB_IDS = [case[0] for case in VERBS]


def _call(
    *,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    caller: ToolCaller,
    headers: Mapping[str, str] | None = None,
) -> tuple[httpx.Response, CallerSpy]:
    """The same request on whichever of the two verbs a row is parametrised over."""
    if method == "GET":
        return _get(monkeypatch=monkeypatch, caller=caller, headers=headers)
    return _post(monkeypatch=monkeypatch, caller=caller, headers=headers, body=_body())


BAD_BEARERS: tuple[tuple[str, Mapping[str, str]], ...] = (
    ("no-header", {}),
    ("wrong-scheme", {"Authorization": "Basic ZmFrZTpmYWtl"}),
    ("empty-bearer", {"Authorization": "Bearer "}),
)


@pytest.mark.parametrize(
    "headers", [case[1] for case in BAD_BEARERS], ids=[case[0] for case in BAD_BEARERS]
)
@pytest.mark.parametrize(("_id", "method"), VERBS, ids=VERB_IDS)
def test_an_absent_or_malformed_bearer_is_a_typed_401_and_opens_no_session(
    killswitch_env: None,
    monkeypatch: pytest.MonkeyPatch,
    _id: str,
    method: str,
    headers: Mapping[str, str],
) -> None:
    """The ninth requirement, on both verbs. Catches a route with no token
    check at all, and one that checks the token AFTER opening the MCP session:
    the caller is scripted with NOTHING, so a route that entered it or called a
    tool fails outright rather than showing up as a count afterwards.

    No kill claim on the set row: that path verifies the
    token twice - once in the route for the claims the tenant substitution is
    read from, once inside `call_tool_route` - so the two copies cannot be
    killed separately and these rows pin the ANSWER rather than either branch.
    On the board row `deps.call_tool_route` calls `authenticated` exactly once
    and the route derives no identity of its own, so deleting that one check is
    the only thing that flips it.
    """
    caller = ScriptedToolCaller()

    response, spy = _call(monkeypatch=monkeypatch, method=method, caller=caller, headers=headers)

    assert response.status_code == 401
    assert response.json() == {"code": ErrorCode.FORBIDDEN.value}
    assert caller.entered == 0
    assert caller.calls == []
    assert spy.bearer_tokens == []


@pytest.mark.parametrize(("_id", "method"), VERBS, ids=VERB_IDS)
def test_a_forged_bearer_is_a_typed_401_and_opens_no_session(
    killswitch_env: None, monkeypatch: pytest.MonkeyPatch, _id: str, method: str
) -> None:
    """Catches a route that reads the claims without VERIFYING the signature:
    this token is well formed and carries a real tenant, a real user, a real
    `sid` and the `platform_admin` principal - it is signed with the wrong
    secret and nothing else, so only the verification can refuse it. A route
    that parsed the claims and trusted them would let anybody who can write a
    JSON object stop every tool on the platform.

    Same note as the row above: no kill is claimed on the set
    verb, where two copies of the check run.
    """
    caller = ScriptedToolCaller()

    response, spy = _call(
        monkeypatch=monkeypatch,
        method=method,
        caller=caller,
        headers=_auth(_token(secret=OTHER_JWT_SECRET)),
    )

    assert response.status_code == 401
    assert response.json() == {"code": ErrorCode.FORBIDDEN.value}
    assert caller.entered == 0
    assert spy.bearer_tokens == []


@pytest.mark.parametrize(("_id", "method"), VERBS, ids=VERB_IDS)
def test_an_expired_bearer_is_a_typed_401_and_opens_no_session(
    killswitch_env: None, monkeypatch: pytest.MonkeyPatch, _id: str, method: str
) -> None:
    """Catches a verification that checks the SIGNATURE and not the expiry: this
    token is signed with the right secret and differs from the happy path's only
    in being nine hours old against an eight-hour `exp`, so the expiry
    comparison is the only thing that can refuse it. A stale token left able to
    trip the platform's kill switch is a session that never ends.

    Same note as the two rows above.
    """
    caller = ScriptedToolCaller()

    response, spy = _call(
        monkeypatch=monkeypatch,
        method=method,
        caller=caller,
        headers=_auth(_token(issued_at=EXPIRED_ISSUED_AT)),
    )

    assert response.status_code == 401
    assert response.json() == {"code": ErrorCode.FORBIDDEN.value}
    assert caller.entered == 0
    assert spy.bearer_tokens == []


@pytest.mark.parametrize(
    ("error_class", "code", "status"),
    [
        pytest.param(RemoteForbiddenError, ErrorCode.FORBIDDEN, 403, id="forbidden-403"),
        pytest.param(RemotePolicyBlockedError, ErrorCode.POLICY_BLOCKED, 409, id="policy-409"),
        pytest.param(RemoteToolDisabledError, ErrorCode.TOOL_DISABLED, 503, id="disabled-503"),
    ],
)
@pytest.mark.parametrize(("_id", "method"), VERBS, ids=VERB_IDS)
def test_a_tool_refusal_answers_its_own_typed_code(
    killswitch_env: None,
    monkeypatch: pytest.MonkeyPatch,
    _id: str,
    method: str,
    error_class: type[RemoteToolError],
    code: ErrorCode,
    status: int,
) -> None:
    """The tenth requirement, one code per row, on both verbs.

    Catches a route that lets the tool's `ToolError` escape: FastAPI answers an
    untyped 500, so a matrix refusal the caller could act on ("you may not
    touch the kill switch"), a switch tripped on `list_kill_switches` itself
    ("try again later") and an admin aiming `set_kill_switch` at
    `set_kill_switch` ("the platform refuses to let you lock yourself out") all
    read as one outage.

    The `POLICY_BLOCKED` row is the one that cannot pass
    today: with no row in the shared map the code falls through to 500, which
    tells an admin the platform broke when the platform refused - the one
    message that would send somebody looking for an outage.

    The bearer VERIFIES here and the body is valid, so neither the token gate
    nor any validation can decide these rows - only the `except ToolError`
    branch and the shared `STATUS_FOR_CODE` can.
    """
    caller = RefusingToolCaller(error_class(code.value))

    response, _spy = _call(
        monkeypatch=monkeypatch, method=method, caller=caller, headers=_auth(_token())
    )

    assert response.status_code == status
    assert response.json() == {"code": code.value}
    assert caller.entered == 1


@pytest.mark.parametrize(("_id", "method"), VERBS, ids=VERB_IDS)
def test_an_unreachable_mcp_server_is_one_typed_internal_naming_neither_host_nor_path(
    killswitch_env: None, monkeypatch: pytest.MonkeyPatch, _id: str, method: str
) -> None:
    """The tenth requirement, last sentence. Catches two failures at once:
    a handshake failure left to propagate (an untyped 500 with a traceback
    body), and a body that carries the client's message -
    `TRANSPORT_FAILURE_MESSAGE` is opaque on purpose, and a route rendering the
    exception's `str` would leak the MCP host and path to a browser.
    `INTERNAL` maps to 500 in the one shared map, so the
    500 here pins that an unreachable server renders the typed `INTERNAL` and
    leaks neither the host nor the path.
    """
    caller = UnreachableToolCaller()

    response, _spy = _call(
        monkeypatch=monkeypatch, method=method, caller=caller, headers=_auth(_token())
    )

    assert response.status_code == 500
    assert response.json() == {"code": ErrorCode.INTERNAL.value}
    assert MCP_HOST not in response.text
    assert MCP_PATH not in response.text
    assert TRANSPORT_FAILURE_MESSAGE not in response.text


# --- the shared map, as a whole -----------------------------------------------


def test_the_shared_status_map_answers_all_eight_codes_with_an_internal_row() -> None:
    """The shared map asserted as a WHOLE, from this router's decision-D angle.

    The two once-disagreeing maps are now one with a row for every
    one of the eight codes, so a code no longer falls through to
    500 by accident. This pins that the proxied-route conflicts this router can
    produce each have their 409 row, and that `INTERNAL` is now an explicit 500
    rather than a fall-through. The canonical whole-map pin is
    `test_error_map.py::test_the_one_map_has_a_row_for_every_code`; this one
    stays because this router once needed a fifth row of its own.

    Asserted as the exact mapping rather than key by key, so a ninth row added
    by a later plan goes red here, where the behaviour lives, rather than on a
    page that renders one sentence per code.
    """
    assert dict(deps.STATUS_FOR_CODE) == {
        ErrorCode.VALIDATION: 400,
        ErrorCode.FORBIDDEN: 403,
        ErrorCode.NOT_FOUND: 404,
        ErrorCode.SOD_CONFLICT: 409,
        ErrorCode.POLICY_BLOCKED: 409,
        ErrorCode.IDEMPOTENCY_CONFLICT: 409,
        ErrorCode.REQUEST_NOT_PENDING: 409,
        ErrorCode.TOOL_DISABLED: 503,
        ErrorCode.INTERNAL: 500,
    }
    assert len(deps.STATUS_FOR_CODE) == 9
    assert deps.STATUS_FOR_CODE[ErrorCode.INTERNAL] == 500
