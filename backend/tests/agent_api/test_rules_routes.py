"""The SoD rules board, over HTTP.

Surface under test (written before it exists; every case below is red on one of
these names until step 1 lands):

``agent_api.rules_routes``
    ``router`` with ``GET /admin/rules`` - no query parameter, no body, no
    argument at all - calling the ``list_sod_rules`` tool through
    ``agent_api.deps``' existing seam: ``authenticated``, ``unauthenticated``,
    ``typed_refusal``, ``call_tool_route`` and ``STATUS_FOR_CODE``. Registered in ``main.py`` as
    ``rules_router``. That seam is why every
    case here patches ``deps.build_tool_caller``, enters no lifespan and opens
    no socket.

Shapes this file fixes:
- the tool name is ``list_sod_rules``, its own registry name, and the path is
  ``/admin/rules``, held here as ``RULES_PATH``.
- the refusal envelope is ``{"code": "<ErrorCode value>"}``.
- the bearer refusal is a literal ``401`` carrying ``{"code": "FORBIDDEN"}``,
  which is what ``deps.unauthenticated()`` answers, the
  shape unchanged. The expected body is written as
  ``{"code": "UNAUTHENTICATED"}``; ``helpdesk.errors.ErrorCode`` has no such
  member, so the code's own value is asserted here.
- the arguments are an EXACTLY empty dict, the zero-argument form
  ``me_routes.ListMyAccessArgs`` and ``killswitch_routes``' board already use;
  ``ListSodRulesInput`` declares no field and forbids extras, so any key at
  all would cross back as a ``VALIDATION`` from the far side.
- the board payloads below are literal-accurate: each is validated against the
  tool's own ``ListSodRulesOutput`` in a premise assertion, so a row invented
  here rather than read off ``mcp_server/tools/rules.py`` fails this file
  instead of passing against a route that answers the wrong shape.
- the staged order is ``policy_ref`` ascending then ``rule_id`` ascending, and the three staged
``rule_id``s are deliberately NOT in
  ascending order of their own, so a route that re-sorted on any single field,
  or reversed, fails the order case.

Cost and isolation: NO backing service at all, exactly as
``test_killswitch_routes.py`` and ``test_audit_routes.py``. The lifespan is
never entered - this route reads nothing off ``app.state`` - so nothing here
opens a socket, a pool or a Redis connection. Fixtures live in this module
rather than a conftest, for the same reason they do there.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from helpdesk.agent_api.api.router import app
from helpdesk.agent_api.api.routes.admin import rules as rules_routes
from helpdesk.agent_api.services import tool_proxy as deps
from helpdesk.agent_api.services.mcp_client import (
    TRANSPORT_FAILURE_MESSAGE,
    RemoteForbiddenError,
    RemoteInternalError,
    RemoteToolDisabledError,
    RemoteToolError,
    ToolCaller,
)
from helpdesk.mcp_server.tools.rules import ListSodRulesOutput
from helpdesk.shared.auth.jwt import issue_token
from helpdesk.shared.core.config import Settings
from helpdesk.shared.core.errors import ErrorCode

LIST_SOD_RULES = "list_sod_rules"

RULES_PATH = "/admin/rules"

# Obviously fake, tests only: none of these is a credential, and
# nothing in this file connects to any of these hosts.
TEST_JWT_SIGNING_PHRASE = "rules-routes-test-jwt-secret-0123456789abc"
OTHER_JWT_SECRET = "another-rules-routes-fake-secret-0123456789"
MCP_URL = "http://mcp.invalid:8081/mcp"
MCP_HOST = "mcp.invalid"
MCP_PATH = "/mcp"
DATABASE_URL = "postgresql+asyncpg://fake:fake@db.invalid:5432/fake_rules"
REDIS_URL = "redis://redis.invalid:6379/0"
QDRANT_URL = "http://qdrant.invalid:6333"

TENANT_ID = uuid.UUID("11111111-1111-5111-8111-111111111111")
ADMIN_ID = uuid.UUID("22222222-2222-5222-8222-222222222222")
DEPARTMENT_ID = uuid.UUID("33333333-3333-5333-8333-333333333333")
SESSION_ID = uuid.UUID("44444444-4444-5444-8444-444444444444")

# A token issued nine hours ago: `issue_token` gives an 8-hour `exp`, so this
# one verifies its signature and fails on expiry alone.
EXPIRED_ISSUED_AT = datetime.now(UTC) - timedelta(hours=9)

# The two principals the contract gives the row a cell for, and the five
# it does not. The five are asserted through the tool's own refusal: the route
# is not the authority on the matrix, so what these rows pin is that a refusal
# reaches the caller as a 403 rather than as a 500 or an empty board.
PERMITTED_PRINCIPALS = ("it_admin", "platform_admin")
DENIED_PRINCIPALS = ("employee", "manager", "system_owner", "hr", "auditor")

# The seven keys of `SodRuleRow` and the five of `SodRuleRoleBlock`, written out
# rather than derived from the payload, so a field added or renamed in the
# staged rows alone cannot satisfy these sets.
ROW_FIELDS = frozenset(
    {"rule_id", "role_a_id", "role_a", "role_b_id", "role_b", "rationale", "policy_ref"}
)
BLOCK_FIELDS = frozenset({"role_id", "slug", "name", "system_slug", "system_name"})

ROLE_A_ID = uuid.UUID("55555555-5555-5555-8555-555555555555")
ROLE_B_ID = uuid.UUID("66666666-6666-5666-8666-666666666666")
ROLE_C_ID = uuid.UUID("77777777-7777-5777-8777-777777777777")
ROLE_D_ID = uuid.UUID("88888888-8888-5888-8888-888888888888")

# Three rule ids whose own ascending order is NOT the staged order: the board
# below crosses as c, a, b, so a route that sorted on `rule_id`, sorted on
# anything else, or reversed the list, answers a different sequence.
RULE_C = "cccccccc-cccc-5ccc-8ccc-cccccccccccc"
RULE_A = "aaaaaaaa-aaaa-5aaa-8aaa-aaaaaaaaaaaa"
RULE_B = "bbbbbbbb-bbbb-5bbb-8bbb-bbbbbbbbbbbb"


def _block(role_id: uuid.UUID, slug: str, name: str) -> dict[str, Any]:
    """One resolved side of a rule: `SodRuleRoleBlock`'s five fields, no more."""
    return {
        "role_id": str(role_id),
        "slug": slug,
        "name": name,
        "system_slug": "fake-finance-app",
        "system_name": "Obviously Fake Finance App",
    }


RESOLVED_ROW: dict[str, Any] = {
    "rule_id": RULE_C,
    "role_a_id": str(ROLE_A_ID),
    "role_a": _block(ROLE_A_ID, "fake-payments-approver", "Fake Payments Approver"),
    "role_b_id": str(ROLE_B_ID),
    "role_b": _block(ROLE_B_ID, "fake-payments-creator", "Fake Payments Creator"),
    "rationale": "Obviously fake rationale, tests only.",
    "policy_ref": "FAKE-SOD-001",
}
"""A rule with both sides resolved: the ordinary row renders in full."""

HALF_RESOLVED_ROW: dict[str, Any] = {
    "rule_id": RULE_A,
    "role_a_id": str(ROLE_C_ID),
    "role_a": None,
    "role_b_id": str(ROLE_D_ID),
    "role_b": _block(ROLE_D_ID, "fake-ledger-closer", "Fake Ledger Closer"),
    "rationale": "Obviously fake rationale for one unreadable side.",
    "policy_ref": "FAKE-SOD-002",
}
"""A rule whose `role_a` row cannot be read: the block is null and the ID stays."""

UNRESOLVED_ROW: dict[str, Any] = {
    "rule_id": RULE_B,
    "role_a_id": str(ROLE_A_ID),
    "role_a": None,
    "role_b_id": str(ROLE_B_ID),
    "role_b": None,
    "rationale": "Obviously fake rationale for two unreadable sides.",
    "policy_ref": "FAKE-SOD-003",
}
"""Neither side resolvable. The point: the rule is still REPORTED."""

BOARD_PAYLOAD: Mapping[str, Any] = {"rules": [RESOLVED_ROW, HALF_RESOLVED_ROW, UNRESOLVED_ROW]}
"""One board exactly as `list_sod_rules` answers it: the `rules` envelope and
three `SodRuleRow`s carrying their seven fields, in `policy_ref` ascending
order. The route must hand this back UNCHANGED, so the
equality case is what catches a router that drops the unresolvable rows,
flattens a block into the row, renames a field or re-sorts the list."""

EMPTY_BOARD: Mapping[str, Any] = {"rules": []}
"""A tenant with no SoD rules. A 200 and an empty list, not a 404."""

STAGED_RULE_IDS = [RULE_C, RULE_A, RULE_B]
"""The staged sequence, as the exact list the order case asserts."""


# --- the scripted seam --------------------------------------------------------


class ScriptedToolCaller:
    """A `ToolCaller` answering one scripted payload per call, in order.

    The same stub shape `test_me_routes.py`, `test_approval_routes.py`,
    `test_review_routes.py`, `test_audit_routes.py` and
    `test_killswitch_routes.py` use: a call past the last
    scripted answer fails the test outright, which is how "the route opened no
    session at all" is asserted as a hard stop rather than as a count read
    afterwards.
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


class CallerSpy:
    """Records the bearer `build_tool_caller` was handed, and hands back a stub."""

    def __init__(self, caller: ToolCaller) -> None:
        self._caller = caller
        self.bearer_tokens: list[str] = []

    def build(self, settings: Settings, bearer_token: str) -> ToolCaller:
        self.bearer_tokens.append(bearer_token)
        return self._caller


@pytest.fixture
def rules_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Obviously fake settings. Nothing here is dialled: no lifespan is entered."""
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    monkeypatch.setenv("QDRANT_URL", QDRANT_URL)
    monkeypatch.setenv("JWT_SECRET", TEST_JWT_SIGNING_PHRASE)
    monkeypatch.setenv("MCP_URL", MCP_URL)


def _token(
    *,
    principals: Sequence[str] = ("it_admin",),
    secret: str = TEST_JWT_SIGNING_PHRASE,
    issued_at: datetime | None = None,
) -> str:
    """One verified-looking token for one fake admin and one session."""
    return issue_token(
        {
            "sub": str(ADMIN_ID),
            "tenant_id": str(TENANT_ID),
            "department_id": str(DEPARTMENT_ID),
            "principals": list(principals),
            "sid": str(SESSION_ID),
        },
        secret,
        datetime.now(UTC) if issued_at is None else issued_at,
    )


def _drive(scenario: Callable[[httpx.AsyncClient], Awaitable[httpx.Response]]) -> httpx.Response:
    async def runner() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await scenario(client)

    return asyncio.run(runner())


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


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
    monkeypatch.setattr(deps, "build_tool_caller", spy.build)

    async def scenario(client: httpx.AsyncClient) -> httpx.Response:
        return await client.get(RULES_PATH, headers=dict(headers or {}))

    return _drive(scenario), spy


def _rules(response: httpx.Response) -> list[dict[str, Any]]:
    body = response.json()
    assert isinstance(body, dict), f"the board must be an envelope, not {type(body)!r}"
    rules = body["rules"]
    assert isinstance(rules, list)
    return list(rules)


# --- the module's own surface --------------------------------------------------


def test_the_router_names_its_path_its_tool_and_a_fieldless_argument_model(
    rules_env: None,
) -> None:
    """The constants this file drives the route with, pinned at their values
    rather than read out of the module under test.

       Catches three drifts no behavioural case above can see, because each of them
       would make those cases agree with whatever the implementation chose: a path
       renamed to `/admin/sod-rules` (the path name is fixed by the contract, and the
       proxy on the other side of the wire is written against the same literal), a
       tool name that is not the registry's `list_sod_rules` (the far side would
       answer `NOT_FOUND` for a tool nobody registered), and an arguments model
       that declares a field - which is the identity-from-token rule's whole concern, and would
       cross back as a `VALIDATION` because `ListSodRulesInput` forbids extras.

       The registration half catches a router written and never included in
       `main.py`: every case above would then fail as a 404 with no hint which of
       the two is missing. It reads `app.openapi()["paths"]` rather than walking
       `app.routes`: this app includes its routers through a wrapper, so the
       concrete `APIRoute` is not on `app.routes` at all and the walk answered an
       empty set for every route in the service.
    """
    assert rules_routes.RULES_PATH == "/admin/rules"
    assert rules_routes.LIST_SOD_RULES_TOOL == "list_sod_rules"
    assert RULES_PATH == rules_routes.RULES_PATH
    assert LIST_SOD_RULES == rules_routes.LIST_SOD_RULES_TOOL
    assert rules_routes.ListSodRulesArgs().model_dump() == {}
    assert rules_routes.ListSodRulesArgs.model_fields == {}
    openapi_paths = app.openapi()["paths"]
    assert RULES_PATH in openapi_paths
    assert "get" in openapi_paths[RULES_PATH]


# --- the staged payloads are the tool's own shape -----------------------------


def test_the_staged_board_is_exactly_what_the_tool_model_produces() -> None:
    """The premise every case below rests on, and the one thing this file can get
    wrong on its own: catches a staged payload invented here instead of read off
    `mcp_server.tools.rules`, which would let the route's body case pass while
    the route answered a shape 24f's parser refuses.

    `ListSodRulesOutput` is the tool's own model, so validating the literal and
    dumping it back in JSON mode is the strongest available statement that the
    fixture is the wire's shape: an extra key is refused, a missing one is
    refused, and a uuid rendered in the wrong form does not round-trip.
    """
    assert ListSodRulesOutput.model_validate(BOARD_PAYLOAD).model_dump(mode="json") == dict(
        BOARD_PAYLOAD
    )
    assert ListSodRulesOutput.model_validate(EMPTY_BOARD).model_dump(mode="json") == dict(
        EMPTY_BOARD
    )
    assert [row["rule_id"] for row in BOARD_PAYLOAD["rules"]] == STAGED_RULE_IDS
    assert sorted(STAGED_RULE_IDS) != STAGED_RULE_IDS
    assert sorted(STAGED_RULE_IDS, reverse=True) != STAGED_RULE_IDS


# --- the board (GET) ----------------------------------------------------------


@pytest.mark.parametrize("principal", PERMITTED_PRINCIPALS)
def test_the_board_route_answers_the_tools_output_unchanged(
    rules_env: None, monkeypatch: pytest.MonkeyPatch, principal: str
) -> None:
    """Catches a route that reshapes, filters or renames `list_sod_rules`'
    structured output on the way out (the envelope
    is `rules` and each row carries its exact seven fields), and one that calls
    a different tool.

    The reshape that matters most here is dropping the unresolvable rows or
    flattening a `role_a` block into the row: the tool carries both ids
    top-level precisely so an admin can act on a rule whose side will not
    resolve, and a route that filtered those rows would hide the only rows this
    page exists to surface.

    The bearer half is the identity-from-token rule: the token the caller
    presented is forwarded VERBATIM, so RBAC, the kill switch and the tool's
    audit event apply to this read. A route that minted its own service token,
    or reused a configured one, fails on the identity assertion alone.

    Parametrised over both permitted principals so the second one is pinned by
    a SUCCESS here and not only by the mcp-server matrix sweep. No kill claim
    for the matrix itself: this route runs no RBAC check, so these rows cannot
    fail for a widened cell.
    """
    caller = ScriptedToolCaller(BOARD_PAYLOAD)
    token = _token(principals=(principal,))

    response, spy = _get(monkeypatch=monkeypatch, caller=caller, headers=_auth(token))

    assert response.status_code == 200
    assert response.json() == dict(BOARD_PAYLOAD)
    assert caller.names == [LIST_SOD_RULES]
    assert spy.bearer_tokens == [token]

    rows = _rules(response)
    assert len(rows) == 3
    for row in rows:
        assert set(row) == ROW_FIELDS
    assert set(rows[0]["role_a"]) == BLOCK_FIELDS
    assert set(rows[0]["role_b"]) == BLOCK_FIELDS


def test_the_board_route_sends_an_exactly_empty_argument_set(
    rules_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second requirement, the identity-from-token rule, as an EXACT key set.

    Catches a route that passes the caller's tenant, user id or a rule id along
    as an argument. `ListSodRulesInput` declares no field and forbids extras, so
    a key riding along would cross back as a `VALIDATION` and the board would
    answer an error on every load - and a `tenant_id` in particular is the
    browser asking to read another tenant's rules, which the token is the only
    authority on (nothing here leans on a cross-tenant guard,
    because the argument must not exist at all).
    """
    caller = ScriptedToolCaller(BOARD_PAYLOAD)

    response, _spy = _get(monkeypatch=monkeypatch, caller=caller, headers=_auth(_token()))

    assert response.status_code == 200
    _name, arguments = caller.calls[0]
    assert arguments == {}


def test_the_rows_cross_in_the_tools_own_order(
    rules_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The third requirement. Catches a route that
    sorts the rows again on the way out, or reverses them: the three staged
    `rule_id`s are neither ascending nor descending among themselves while their
    `policy_ref`s ascend, so sorting on either field, or reversing, answers a
    different sequence than the one asserted.

    The order is the whole value of total order - every caller sees ONE
    sequence - and a second sort here would give 24f a second contract to
    track.
    """
    caller = ScriptedToolCaller(BOARD_PAYLOAD)

    response, _spy = _get(monkeypatch=monkeypatch, caller=caller, headers=_auth(_token()))

    assert response.status_code == 200
    assert [row["rule_id"] for row in _rules(response)] == STAGED_RULE_IDS
    assert [row["policy_ref"] for row in _rules(response)] == [
        "FAKE-SOD-001",
        "FAKE-SOD-002",
        "FAKE-SOD-003",
    ]


def test_an_unresolvable_side_crosses_as_null_with_its_id_intact(
    rules_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The third requirement, second half.

    Catches three routes: one that DROPS a rule whose side will not resolve (the
    row an admin opened this page to fix disappears), one that turns a `null`
    block into an empty object or into invented blank names (the page renders an
    unresolvable rule as a resolved one), and one that omits `role_a_id` once
    `role_a` is null - which would leave the admin with a conflict they cannot
    look up.

    Both the one-null row and the both-null row are asserted, because a route
    that special-cased "fully unresolved" would pass on the first alone.
    """
    caller = ScriptedToolCaller(BOARD_PAYLOAD)

    response, _spy = _get(monkeypatch=monkeypatch, caller=caller, headers=_auth(_token()))

    assert response.status_code == 200
    _resolved, half, unresolved = _rules(response)

    assert half["role_a"] is None
    assert half["role_a_id"] == str(ROLE_C_ID)
    assert set(half["role_b"]) == BLOCK_FIELDS
    assert set(half) == ROW_FIELDS

    assert unresolved["role_a"] is None
    assert unresolved["role_b"] is None
    assert unresolved["role_a_id"] == str(ROLE_A_ID)
    assert unresolved["role_b_id"] == str(ROLE_B_ID)
    assert set(unresolved) == ROW_FIELDS


def test_a_tenant_with_no_rules_is_a_200_and_an_empty_list(
    rules_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The last unhappy path. Catches a route that treats an empty board
    as a missing resource - a 404, or a typed `NOT_FOUND` - which would leave a
    tenant that has simply not written any SoD rules yet looking at an error
    page, and catches a route that answers `{}` or a bare `[]` instead of the
    envelope 24f parses.

    Only the empty payload can decide this row: the bearer verifies, the tool
    answers successfully, and no refusal branch is entered.
    """
    caller = ScriptedToolCaller(EMPTY_BOARD)

    response, _spy = _get(monkeypatch=monkeypatch, caller=caller, headers=_auth(_token()))

    assert response.status_code == 200
    assert response.json() == {"rules": []}


# --- the bearer ---------------------------------------------------------------


BAD_BEARERS: tuple[tuple[str, Mapping[str, str]], ...] = (
    ("no-header", {}),
    ("wrong-scheme", {"Authorization": "Basic ZmFrZTpmYWtl"}),
    ("empty-bearer", {"Authorization": "Bearer "}),
)


@pytest.mark.parametrize(
    "headers", [case[1] for case in BAD_BEARERS], ids=[case[0] for case in BAD_BEARERS]
)
def test_an_absent_or_malformed_bearer_is_a_typed_401_and_opens_no_session(
    rules_env: None, monkeypatch: pytest.MonkeyPatch, headers: Mapping[str, str]
) -> None:
    """The fourth requirement. Catches a route with no token check at all,
    and one that checks the token AFTER opening the MCP session: the caller is
    scripted with NOTHING, so a route that entered it or called a tool fails
    outright rather than showing up as a count read afterwards.

    KILL: `deps.call_tool_route`
    calls `authenticated` exactly once and this route derives no identity of its
    own, so deleting that single call is the only thing that flips this row -
    there is no second copy of the check to hide behind.
    """
    caller = ScriptedToolCaller()

    response, spy = _get(monkeypatch=monkeypatch, caller=caller, headers=headers)

    assert response.status_code == 401
    assert response.json() == {"code": ErrorCode.FORBIDDEN.value}
    assert caller.entered == 0
    assert caller.calls == []
    assert spy.bearer_tokens == []


def test_a_forged_bearer_is_a_typed_401_and_opens_no_session(
    rules_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a route that reads the claims without VERIFYING the signature:
    this token is well formed and carries a real tenant, a real user, a real
    `sid` and the `it_admin` principal - it is signed with the wrong secret and
    nothing else, so only the verification can refuse it. A route that parsed
    the claims and trusted them would hand a tenant's whole SoD policy to
    anybody who can write a JSON object.

    Same kill as the row above: one `authenticated` call, no second copy.
    """
    caller = ScriptedToolCaller()

    response, spy = _get(
        monkeypatch=monkeypatch, caller=caller, headers=_auth(_token(secret=OTHER_JWT_SECRET))
    )

    assert response.status_code == 401
    assert response.json() == {"code": ErrorCode.FORBIDDEN.value}
    assert caller.entered == 0
    assert spy.bearer_tokens == []


def test_an_expired_bearer_is_a_typed_401_and_opens_no_session(
    rules_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a verification that checks the SIGNATURE and not the expiry: this
    token is signed with the right secret and differs from the happy path's only
    in being nine hours old against an eight-hour `exp`, so the expiry
    comparison is the only thing that can refuse it. A stale token left able to
    read the rules board is a session that never ends.

    Same kill as the two rows above: one `authenticated` call, no second copy.
    """
    caller = ScriptedToolCaller()

    response, spy = _get(
        monkeypatch=monkeypatch, caller=caller, headers=_auth(_token(issued_at=EXPIRED_ISSUED_AT))
    )

    assert response.status_code == 401
    assert response.json() == {"code": ErrorCode.FORBIDDEN.value}
    assert caller.entered == 0
    assert spy.bearer_tokens == []


# --- the tool's own refusals ---------------------------------------------------


@pytest.mark.parametrize("principal", DENIED_PRINCIPALS)
def test_a_principal_with_no_cell_reads_a_typed_403_and_no_rules(
    rules_env: None, monkeypatch: pytest.MonkeyPatch, principal: str
) -> None:
    """The fifth requirement, first half, one row per principal with no
    cell on the `list_sod_rules` row.

    Catches a route that lets the tool's `ToolError` escape - FastAPI answers an
    untyped 500, so "you may not read this tenant's SoD policy" reads as an
    outage - and a route that swallows a refusal into an empty board, which
    would tell an auditor the tenant has no separation-of-duties rules at all.

    No kill claim for the MATRIX: the matrix lives behind the
    MCP boundary and is pinned per principal by
    `services/mcp-server/tests/test_mcp_list_sod_rules.py`. These rows stage the
    refusal and pin what the caller RECEIVES, which only the `except ToolError`
    branch and the shared `STATUS_FOR_CODE` can decide - the bearer verifies
    here, so the token gate cannot.
    """
    caller = RefusingToolCaller(RemoteForbiddenError(ErrorCode.FORBIDDEN.value))

    response, _spy = _get(
        monkeypatch=monkeypatch, caller=caller, headers=_auth(_token(principals=(principal,)))
    )

    assert response.status_code == 403
    assert response.json() == {"code": ErrorCode.FORBIDDEN.value}
    assert caller.entered == 1


@pytest.mark.parametrize(
    ("error_class", "code", "status"),
    [
        pytest.param(RemoteForbiddenError, ErrorCode.FORBIDDEN, 403, id="forbidden-403"),
        pytest.param(RemoteToolDisabledError, ErrorCode.TOOL_DISABLED, 503, id="disabled-503"),
    ],
)
def test_a_tool_refusal_answers_its_own_typed_code(
    rules_env: None,
    monkeypatch: pytest.MonkeyPatch,
    error_class: type[RemoteToolError],
    code: ErrorCode,
    status: int,
) -> None:
    """The fifth requirement, both codes, read through the SHARED
    `STATUS_FOR_CODE` (which this change does not touch).

    Catches a route that maps every refusal onto one status: a matrix denial the
    caller cannot act on and a kill switch tripped on `list_sod_rules` ("try
    again later") are different answers, and a board that reported a tripped
    switch as a permission problem would send an admin looking for the wrong
    fix. Catches a router that built its own status map too, which the shared
    map exists to stop.

    The bearer verifies and the route takes no input at all, so no validation
    and no token gate can decide these rows - only the `except ToolError`
    branch can.
    """
    caller = RefusingToolCaller(error_class(code.value))

    response, _spy = _get(monkeypatch=monkeypatch, caller=caller, headers=_auth(_token()))

    assert response.status_code == status
    assert response.json() == {"code": code.value}
    assert caller.entered == 1


def test_an_unreachable_mcp_server_is_one_typed_internal_naming_neither_host_nor_path(
    rules_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sixth requirement. Catches two failures at once: a handshake
    failure left to propagate (an untyped 500 carrying a traceback body), and a
    body that carries the client's own message - `TRANSPORT_FAILURE_MESSAGE` is
    opaque on purpose, and a route rendering the exception's `str` would leak
    the MCP host and path to a browser. `INTERNAL` maps to 500 in the one
    shared map, so the 500 here pins that an unreachable
    server renders the typed `INTERNAL` and leaks neither the host nor the path.
    """
    caller = UnreachableToolCaller()

    response, _spy = _get(monkeypatch=monkeypatch, caller=caller, headers=_auth(_token()))

    assert response.status_code == 500
    assert response.json() == {"code": ErrorCode.INTERNAL.value}
    assert MCP_HOST not in response.text
    assert MCP_PATH not in response.text
    assert TRANSPORT_FAILURE_MESSAGE not in response.text
