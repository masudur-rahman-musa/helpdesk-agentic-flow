"""The notification page and the read stamp, over HTTP.

Surface under test (written before it exists; every case below is red on one
of these names until steps 1 and 2 land):

``agent_api.graph.keys``
    ``derive_notification_read_key(*, reader_user_id: uuid.UUID,
    notification_id: uuid.UUID) -> str`` -- ``uuid5(KEY_NAMESPACE, reader ||
    notification)`` over the module's existing ``\\x1f`` separator, length
    checked by ``check_idempotency_key`` before it is returned. A two-value
    sibling of ``derive_decision_key`` and 23h's
    ``derive_review_decision_key`` with no verb in the material.

``agent_api.notification_routes``
    ``router`` with ``GET /me/notifications`` (optional ``unread_only``,
    optional ``limit``) answered by ``my_notifications``, and ``POST
    /notifications/{notification_id}/read`` answered by ``mark_read_route``,
    registered in ``main.py``. The argument models are
    ``ListNotificationsArgs`` (empty base),
    ``ListNotificationsArgsWithUnreadOnly``, ``ListNotificationsArgsWithLimit``,
    ``ListNotificationsArgsWithUnreadOnlyAndLimit`` and
    ``MarkNotificationReadArgs``. Both routes go through ``agent_api.deps``'
    existing seam -- ``authenticated``, ``unauthenticated``,
    ``call_tool_route`` and ``STATUS_FOR_CODE`` -- which is why every case here
    patches ``deps.build_tool_caller`` and none of them opens a socket.

Shapes this file asserts against, all of them fixed by
the tool contract except where noted:
- the two tool names are exactly ``list_notifications`` and
  ``mark_notification_read``, and the read stamp's key material is the reader
  and the notification and NOTHING else (one verb is a constant
  pretending to be a variable).
- the refusal envelope is ``{"code": "<ErrorCode value>"}`` and the bearer
  refusal a literal ``401``, the usual shape, unchanged.
- the page's arguments are ``{}``, ``{"unread_only": ...}``, ``{"limit": ...}``
  or both -- a subclass per shape rather than a nullable field, because
  ``model_dump`` would send ``unread_only: null`` into a ``bool`` and
  ``limit: null`` into a ``1``-``200`` bound. The cases below assert the
  arguments DICT, so any construction answering those four shapes passes.
- the stamp's arguments are exactly ``{"notification_id": ...,
  "idempotency_key": ...}``, and the route takes NO body at all,
  so the cases send none.
- ``KEY_LENGTH`` is read from ``helpdesk.models.idempotency`` rather than
  written as a literal, because the contract names the constant and not
  its value; the two
  bounds it DOES state, ``LIMIT_MIN = 1`` and ``LIMIT_MAX = 200``, are
  written out below as the literals they are.
- a non-uuid ``notification_id`` and an unparseable ``unread_only`` are both
  FastAPI's own ``422``: the first is a ``uuid.UUID`` path
  parameter and the second a typed ``bool``, so no second
  validator is written and what the cases pin is that no tool, no key and no
  read is reached.
- the page size bound stays the TOOL's (a known risk), so the
  boundary rows here drive a caller that applies
  ``ListNotificationsInput``'s own ``1``-``200`` and answers the typed
  ``VALIDATION`` the real MCP server answers. The route's job -- the only thing
  those rows can fail on -- is forwarding the number untouched and dressing
  that code as **400**, rather than declaring a second bound that answers
  FastAPI's untyped 422 for a value the tool decides.

Cost and isolation: NO backing service at all, exactly as
``test_review_routes.py``. The lifespan is never entered -- neither route reads
anything off ``app.state`` -- so nothing here opens a socket, a pool or a Redis
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
from pydantic import BaseModel

from helpdesk.agent_api.agent.keys import (
    KEY_NAMESPACE,
    InvalidIdempotencyKeyError,
    check_idempotency_key,
    derive_notification_read_key,
)
from helpdesk.agent_api.api.router import app
from helpdesk.agent_api.services import tool_proxy as deps
from helpdesk.agent_api.services.mcp_client import (
    TRANSPORT_FAILURE_MESSAGE,
    RemoteForbiddenError,
    RemoteInternalError,
    RemoteNotFoundError,
    RemoteToolDisabledError,
    RemoteToolError,
    RemoteValidationError,
    ToolCaller,
)
from helpdesk.shared.auth.jwt import issue_token
from helpdesk.shared.core.config import Settings
from helpdesk.shared.core.errors import ErrorCode
from helpdesk.shared.models.idempotency import KEY_LENGTH

LIST_NOTIFICATIONS = "list_notifications"
MARK_NOTIFICATION_READ = "mark_notification_read"

# `graph/keys.py`'s own separator, repeated here as the literal it is: the
# module keeps it private, and the key material is the thing these cases pin.
SEPARATOR = "\x1f"

# The tool's bounds (`mcp_server.tools.notifications.LIMIT_MIN` / `LIMIT_MAX` /
# `LIMIT_DEFAULT`), written out because agent-api cannot import the mcp-server
# package. The values are written out; the second risk says these
# are the only validator, so the route must neither repeat them more narrowly
# nor refuse what they accept.
LIMIT_MIN = 1
LIMIT_MAX = 200
LIMIT_DEFAULT = 50

# Obviously fake, tests only: none of these is a credential, and
# nothing in this file connects to any of these hosts.
TEST_JWT_SIGNING_PHRASE = "notification-routes-test-jwt-secret-01234"
OTHER_JWT_SECRET = "another-notification-routes-fake-secret-1"
MCP_URL = "http://mcp.invalid:8081/mcp"
MCP_HOST = "mcp.invalid"
MCP_PATH = "/mcp"
DATABASE_URL = "postgresql+asyncpg://fake:fake@db.invalid:5432/fake_notifications"
REDIS_URL = "redis://redis.invalid:6379/0"
QDRANT_URL = "http://qdrant.invalid:6333"

TENANT_ID = uuid.UUID("11111111-1111-5111-8111-111111111111")
READER_ID = uuid.UUID("22222222-2222-5222-8222-222222222222")
OTHER_READER_ID = uuid.UUID("2b2b2b2b-2b2b-5b2b-8b2b-2b2b2b2b2b2b")
DEPARTMENT_ID = uuid.UUID("33333333-3333-5333-8333-333333333333")
SESSION_ID = uuid.UUID("44444444-4444-5444-8444-444444444444")
NOTIFICATION_ID = uuid.UUID("99999999-9999-5999-8999-999999999999")
OTHER_NOTIFICATION_ID = uuid.UUID("9b9b9b9b-9b9b-5b9b-8b9b-9b9b9b9b9b9b")
REQUEST_ID = uuid.UUID("77777777-7777-5777-8777-777777777777")

PAGE_PATH = "/me/notifications"
STAMP_PATH = f"/notifications/{NOTIFICATION_ID}/read"

# The key `uuid5(KEY_NAMESPACE, READER_ID \x1f NOTIFICATION_ID)` takes, computed
# by hand outside this file and written as the literal it is. The first
# risk is a key colliding across two notifications, and a derivation over the
# right two values under the WRONG namespace or joined with a different
# separator would be stable, unique and completely different from this: the
# bare concatenation of the same two uuids is
# `8f4294b8-4169-56c9-b448-95073f9abdff`, which is what this literal separates
# from the documented one.
EXPECTED_READ_FINGERPRINT = "157149bd-4d3a-5f66-87b1-aa756d2177fe"

# A token issued nine hours ago: `issue_token` gives an 8-hour `exp`, so this
# one verifies its signature and fails on expiry alone.
EXPIRED_ISSUED_AT = datetime.now(UTC) - timedelta(hours=9)

PAGE_PAYLOAD: Mapping[str, Any] = {
    "notifications": [
        {
            "notification_id": str(NOTIFICATION_ID),
            "kind": "request_approved",
            "title": "Your access request was approved",
            "body": "Fake Payroll Read is yours until the end of the quarter.",
            "subject_type": "access_request",
            "subject_id": str(REQUEST_ID),
            "created_at": "2026-06-02T10:15:00Z",
            "read_at": None,
        }
    ],
    "unread_count": 3,
}
"""One page, exactly as `list_notifications` answers it: the two-key envelope
over the eight `NotificationRow` fields. The route must hand this
back UNCHANGED, so the case asserting equality is what catches a router that
reshapes, filters, re-counts or renames on the way out."""

STAMP_PAYLOAD: Mapping[str, Any] = {
    "notification_id": str(NOTIFICATION_ID),
    "kind": "request_approved",
    "title": "Your access request was approved",
    "read_at": "2026-06-02T11:00:00Z",
}
"""`mark_notification_read`'s four `MarkNotificationReadOutput` fields."""


# --- the scripted seam --------------------------------------------------------


class ScriptedToolCaller:
    """A `ToolCaller` answering one scripted payload per call, in order.

    The same stub shape `test_me_routes.py`, `test_approval_routes.py` and
    `test_review_routes.py` use: a call past the last
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


class BoundCheckingToolCaller:
    """The MCP server's own input validation, standing in for the wire.

    The second risk: `ListNotificationsInput`'s `1`-`200` is the only
    bound on the page size, so an out-of-bounds limit crosses back as a typed
    `VALIDATION` rather than being refused in agent-api. This caller answers
    exactly that, and records in `answered` only the calls it actually
    ANSWERED, which is what "no read" means on a seam that never reaches a
    database.
    """

    def __init__(self, answer: Mapping[str, Any]) -> None:
        self._answer = answer
        self.answered: list[dict[str, Any]] = []
        self.entered = 0

    async def __aenter__(self) -> ToolCaller:
        self.entered += 1
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def call_tool(self, name: str, arguments: BaseModel) -> Mapping[str, Any]:
        sent = arguments.model_dump(mode="json")
        limit = sent.get("limit")
        if limit is not None and (
            not isinstance(limit, int) or not LIMIT_MIN <= limit <= LIMIT_MAX
        ):
            raise RemoteValidationError("limit is out of bounds")
        unread_only = sent.get("unread_only")
        if unread_only is not None and not isinstance(unread_only, bool):
            raise RemoteValidationError("unread_only is not a bool")
        self.answered.append(sent)
        return self._answer


@pytest.fixture
def notifications_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Obviously fake settings. Nothing here is dialled: no lifespan is entered."""
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    monkeypatch.setenv("QDRANT_URL", QDRANT_URL)
    monkeypatch.setenv("JWT_SECRET", TEST_JWT_SIGNING_PHRASE)
    monkeypatch.setenv("MCP_URL", MCP_URL)


def _token(
    *,
    user_id: uuid.UUID = READER_ID,
    secret: str = TEST_JWT_SIGNING_PHRASE,
    issued_at: datetime | None = None,
) -> str:
    """One verified-looking token for one fake reader and one conversation."""
    return issue_token(
        {
            "sub": str(user_id),
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
    params: Mapping[str, str] | None = None,
) -> tuple[httpx.Response, CallerSpy]:
    """One call against the app, with the tool caller replaced at the seam.

        The seam is `deps.build_tool_caller` (`call_tool_route` is built there), and the lifespan is
        NOT entered: neither route reads anything off
        `app.state`, so entering it would buy a psycopg pool, a Redis client and a
        gateway client that no case here uses.

        No `body` parameter exists on purpose: the stamp route takes no body at all
    , so every call here sends none and a route that required one
        would fail every stamp case with a 422.

        Query values go through `params` rather than through the path, so a value
        with a trailing space is percent-encoded by httpx and reaches the route as
        the string the browser actually sent.
    """
    spy = CallerSpy(caller)
    monkeypatch.setattr(deps, "build_tool_caller", spy.build)

    async def scenario(client: httpx.AsyncClient) -> httpx.Response:
        return await client.request(
            method,
            path,
            headers=dict(headers or {}),
            params=None if params is None else dict(params),
        )

    return _drive(scenario), spy


# The two routes, as one table: every refusal below is asserted on both of
# them, because a guard present on the read and missing on the mutation is
# exactly the hole a per-route case cannot see.
ROUTES: tuple[tuple[str, str, str], ...] = (
    ("page", "GET", PAGE_PATH),
    ("stamp", "POST", STAMP_PATH),
)
ROUTE_IDS = [case[0] for case in ROUTES]


# --- the happy paths ----------------------------------------------------------


def test_the_page_route_answers_the_tools_output_unchanged(
    notifications_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a route that reshapes, filters, re-counts or renames
    `list_notifications`' two-key envelope on the way out (the tool "returns
    the tool's structured output unchanged"), and one that calls a different
    tool.

    The bearer half is the identity-from-token rule: the token the caller
    presented is forwarded VERBATIM, so RBAC, the kill switch and the self-only
    scope check all apply to this read. A route that minted its own service
    token, or reused a configured one, fails on the identity assertion alone; a
    route that passed the reader's id as an ARGUMENT fails on the exact key
    set, which is what the identity-from-token rule forbids on a page whose whole filter is the
    caller's own identity.
    """
    caller = ScriptedToolCaller(PAGE_PAYLOAD)
    token = _token()

    response, spy = _request(
        monkeypatch=monkeypatch,
        method="GET",
        path=PAGE_PATH,
        caller=caller,
        headers=_auth(token),
        params={"unread_only": "true", "limit": "25"},
    )

    assert response.status_code == 200
    assert response.json() == PAGE_PAYLOAD
    assert set(response.json()) == {"notifications", "unread_count"}
    assert caller.names == [LIST_NOTIFICATIONS]
    _name, arguments = caller.calls[0]
    assert arguments == {"unread_only": True, "limit": 25}
    assert spy.bearer_tokens == [token]


def test_the_page_route_sends_neither_unread_only_nor_limit_when_neither_is_asked_for(
    notifications_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first requirement, both halves at once. Catches a route that
    repeats the tool's `LIMIT_DEFAULT` (50) as a number of its own -- a second
    place for it to drift from -- one that sends `limit: null`
    into a bound of 1..200 or `unread_only: null` into a `bool` and gets a
    `VALIDATION` back, so the page shows nothing to a person with messages
    waiting, and one that defaults the filter to `True`, which would hide every
    already-read message the page exists to list.
    """
    caller = ScriptedToolCaller(PAGE_PAYLOAD)

    response, _spy = _request(
        monkeypatch=monkeypatch,
        method="GET",
        path=PAGE_PATH,
        caller=caller,
        headers=_auth(_token()),
    )

    assert response.status_code == 200
    _name, arguments = caller.calls[0]
    assert arguments == {}


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        pytest.param({"unread_only": "true"}, {"unread_only": True}, id="unread-only-only"),
        pytest.param({"limit": "25"}, {"limit": 25}, id="limit-only"),
    ],
)
def test_the_page_route_sends_each_query_parameter_only_when_it_is_asked_for(
    notifications_env: None,
    monkeypatch: pytest.MonkeyPatch,
    params: Mapping[str, str],
    expected: Mapping[str, Any],
) -> None:
    """The two shapes between "neither" and "both", which no other case covers.

    Catches a route carrying ONE argument model with both fields nullable:
    `model_dump` sends every declared field, so asking for the unread set alone
    would put `limit: null` on the wire (refused by a 1..200 bound) and asking
    for a limit alone would put `unread_only: null` into a `bool` (refused
    too). Only the per-shape construction the contract names -- four models, one
    per shape -- can make both rows pass.
    """
    caller = ScriptedToolCaller(PAGE_PAYLOAD)

    response, _spy = _request(
        monkeypatch=monkeypatch,
        method="GET",
        path=PAGE_PATH,
        caller=caller,
        headers=_auth(_token()),
        params=params,
    )

    assert response.status_code == 200
    _name, arguments = caller.calls[0]
    assert arguments == expected


def test_the_stamp_route_answers_the_tools_output_under_the_callers_own_bearer(
    notifications_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mutation half of hthe no-model-mutation and identity-from-token rules at once.

    Catches a route that reshapes the stamp's four-key answer, one that calls
    the wrong tool, one that forwards anything but the caller's own bearer,
    and -- the assertion no other case here makes -- one that passes an
    IDENTITY argument: the arguments are an EXACT key set, so a `user_id` or a
    `reader_user_id` riding along fails, and so does a missing
    `idempotency_key`, which would leave a double-clicked bell filing a second
    call instead of replaying the stored response.

    No body is sent at all, so a route declaring one fails this
    case with a 422 rather than passing quietly.
    """
    caller = ScriptedToolCaller(STAMP_PAYLOAD)
    token = _token()

    response, spy = _request(
        monkeypatch=monkeypatch,
        method="POST",
        path=STAMP_PATH,
        caller=caller,
        headers=_auth(token),
    )

    assert response.status_code == 200
    assert response.json() == STAMP_PAYLOAD
    assert set(response.json()) == {"notification_id", "kind", "title", "read_at"}
    assert caller.names == [MARK_NOTIFICATION_READ]
    _name, arguments = caller.calls[0]
    assert set(arguments) == {"notification_id", "idempotency_key"}
    assert arguments["notification_id"] == str(NOTIFICATION_ID)
    assert arguments["idempotency_key"] == derive_notification_read_key(
        reader_user_id=READER_ID, notification_id=NOTIFICATION_ID
    )
    assert spy.bearer_tokens == [token]


def test_the_key_is_derived_from_the_tokens_reader_and_never_from_a_query_parameter(
    notifications_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The identity requirement, the identity-from-token rule on the one value this route
    derives.

    The query names ANOTHER user of the SAME tenant as the caller (a cross-tenant name would be
    refused by the tenant guard on the MCP
    side whether this rule holds or not, so it would pin nothing). Catches a
    route that derives the key from `reader_user_id` or `user_id` instead of
    from the verified `sub` -- two readers would then share a key and the second
    stamp would replay the first's stored answer -- and a route that forwards
    either field as a tool argument, which the exact key set refuses.

    Only the source of the reader can flip this case: the token verifies, the
    path parameter is a real uuid, and there is no body to validate.
    """
    caller = ScriptedToolCaller(STAMP_PAYLOAD)

    response, _spy = _request(
        monkeypatch=monkeypatch,
        method="POST",
        path=STAMP_PATH,
        caller=caller,
        headers=_auth(_token()),
        params={"user_id": str(OTHER_READER_ID), "reader_user_id": str(OTHER_READER_ID)},
    )

    assert response.status_code == 200
    _name, arguments = caller.calls[0]
    assert set(arguments) == {"notification_id", "idempotency_key"}
    assert arguments["idempotency_key"] == derive_notification_read_key(
        reader_user_id=READER_ID, notification_id=NOTIFICATION_ID
    )
    assert arguments["idempotency_key"] != derive_notification_read_key(
        reader_user_id=OTHER_READER_ID, notification_id=NOTIFICATION_ID
    )


def test_the_page_route_sends_no_identity_argument_even_when_the_query_names_one(
    notifications_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The read half of the same criterion. Catches a route that forwards every
    query parameter it received: `list_notifications` IGNORES extras (23ga's
    read contract), so a `user_id` riding along would never be refused on the
    far side and nothing else in this file would notice it.

    The named user is another user of the SAME tenant, so a
    route honouring it would answer that colleague's messages rather than being
    caught by tenant scoping.
    """
    caller = ScriptedToolCaller(PAGE_PAYLOAD)

    response, _spy = _request(
        monkeypatch=monkeypatch,
        method="GET",
        path=PAGE_PATH,
        caller=caller,
        headers=_auth(_token()),
        params={"user_id": str(OTHER_READER_ID), "reader_user_id": str(OTHER_READER_ID)},
    )

    assert response.status_code == 200
    _name, arguments = caller.calls[0]
    assert arguments == {}


def test_one_reader_stamping_one_notification_twice_sends_one_key(
    notifications_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The idempotency requirement, through the ROUTE.

    Catches a key minted fresh per call (`uuid4`) or reaching a clock: a
    double-clicked bell would then arrive under two keys and file a second call
    instead of replaying the stored response. Everything is identical across
    the two calls -- same bearer, same path, no query -- so only the material
    can flip it.
    """
    caller = ScriptedToolCaller(STAMP_PAYLOAD, STAMP_PAYLOAD)
    token = _token()

    for _click in range(2):
        response, _spy = _request(
            monkeypatch=monkeypatch,
            method="POST",
            path=STAMP_PATH,
            caller=caller,
            headers=_auth(token),
        )
        assert response.status_code == 200

    first, second = (arguments for _name, arguments in caller.calls)
    assert first["idempotency_key"] == second["idempotency_key"]


def test_another_notification_stamped_by_the_same_reader_sends_a_different_key(
    notifications_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the same criterion, at the route rather than in the
    pure function. Catches a route that derives the key from the READER alone
    and ignores its own path parameter: the second notification a person opens
    would arrive under the first one's key and be handed the first one's stored
    answer, so the bell would never clear -- a known risk, seen from
    the wire.
    """
    caller = ScriptedToolCaller(STAMP_PAYLOAD, STAMP_PAYLOAD)
    token = _token()

    for path in (STAMP_PATH, f"/notifications/{OTHER_NOTIFICATION_ID}/read"):
        response, _spy = _request(
            monkeypatch=monkeypatch,
            method="POST",
            path=path,
            caller=caller,
            headers=_auth(token),
        )
        assert response.status_code == 200

    first, second = (arguments for _name, arguments in caller.calls)
    assert first["idempotency_key"] != second["idempotency_key"]
    assert second["notification_id"] == str(OTHER_NOTIFICATION_ID)


# --- the refusals -------------------------------------------------------------


BAD_BEARERS: tuple[tuple[str, Mapping[str, str]], ...] = (
    ("no-header", {}),
    ("wrong-scheme", {"Authorization": "Basic ZmFrZTpmYWtl"}),
    ("empty-bearer", {"Authorization": "Bearer "}),
)


@pytest.mark.parametrize(("_id", "method", "path"), ROUTES, ids=ROUTE_IDS)
@pytest.mark.parametrize(
    "headers", [case[1] for case in BAD_BEARERS], ids=[c[0] for c in BAD_BEARERS]
)
def test_an_absent_or_malformed_bearer_is_a_typed_401_and_opens_no_session(
    notifications_env: None,
    monkeypatch: pytest.MonkeyPatch,
    _id: str,
    method: str,
    path: str,
    headers: Mapping[str, str],
) -> None:
    """Catches a route with no token check at all, and one that checks the
    token AFTER opening the MCP session: the caller is scripted with NOTHING,
    so a route that entered it or called a tool fails outright rather than
    showing up as a count afterwards.

    On the PAGE route the token check is the only guard on the path -- no body,
    no path parameter, no query, no rate limit and no DB read -- so deleting it
    is what flips that row. On the STAMP route the token is
    verified twice, once for the reader the key is derived from and once inside
    `call_tool_route`, so that row pins the ANSWER and claims no kill on either
    copy (two copies of one guard cannot be killed
    separately, and the tool says so in those words).
    """
    caller = ScriptedToolCaller()

    response, spy = _request(
        monkeypatch=monkeypatch,
        method=method,
        path=path,
        caller=caller,
        headers=headers,
    )

    assert response.status_code == 401
    assert response.json() == {"code": ErrorCode.FORBIDDEN.value}
    assert caller.entered == 0
    assert caller.calls == []
    assert spy.bearer_tokens == []


@pytest.mark.parametrize(("_id", "method", "path"), ROUTES, ids=ROUTE_IDS)
def test_a_forged_bearer_is_a_typed_401_and_opens_no_session(
    notifications_env: None,
    monkeypatch: pytest.MonkeyPatch,
    _id: str,
    method: str,
    path: str,
) -> None:
    """Catches a route that reads the claims without VERIFYING the signature:
    this token is well formed and carries a real tenant, a real user and a real
    `sid` -- it is signed with the wrong secret and nothing else, so only the
    verification can refuse it. A route that parsed the claims and trusted them
    would hand somebody else's messages to a token anybody can write.

    Same split as the row above: a kill claim on the page
    route, the ANSWER alone on the stamp route.
    """
    caller = ScriptedToolCaller()

    response, spy = _request(
        monkeypatch=monkeypatch,
        method=method,
        path=path,
        caller=caller,
        headers=_auth(_token(secret=OTHER_JWT_SECRET)),
    )

    assert response.status_code == 401
    assert response.json() == {"code": ErrorCode.FORBIDDEN.value}
    assert caller.entered == 0
    assert spy.bearer_tokens == []


@pytest.mark.parametrize(("_id", "method", "path"), ROUTES, ids=ROUTE_IDS)
def test_an_expired_bearer_is_a_typed_401_and_opens_no_session(
    notifications_env: None,
    monkeypatch: pytest.MonkeyPatch,
    _id: str,
    method: str,
    path: str,
) -> None:
    """Catches a verification that checks the SIGNATURE and not the expiry:
    this token is signed with the right secret and differs from the happy
    path's only in being nine hours old against an eight-hour `exp`, so the
    expiry comparison is the only thing that can refuse it. A stale token left
    reading somebody's bell is a session that never ends.

    Same split as the two rows above: a kill claim on the page
    route, the ANSWER alone on the stamp route, where the same check runs
    twice.
    """
    caller = ScriptedToolCaller()

    response, spy = _request(
        monkeypatch=monkeypatch,
        method=method,
        path=path,
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
        pytest.param(RemoteToolDisabledError, ErrorCode.TOOL_DISABLED, 503, id="tool-disabled-503"),
        pytest.param(RemoteNotFoundError, ErrorCode.NOT_FOUND, 404, id="not-found-404"),
    ],
)
@pytest.mark.parametrize(("_id", "method", "path"), ROUTES, ids=ROUTE_IDS)
def test_a_tool_refusal_answers_its_own_typed_code(
    notifications_env: None,
    monkeypatch: pytest.MonkeyPatch,
    _id: str,
    method: str,
    path: str,
    error_class: type[RemoteToolError],
    code: ErrorCode,
    status: int,
) -> None:
    """The second unhappy path, one code per row. Catches a route that
    lets the tool's `ToolError` escape: FastAPI answers an untyped 500, so
    another person's notification id (`NOT_FOUND`), an admin's
    tripped kill switch and a matrix refusal would all read as one outage.

    The bearer here VERIFIES and the arguments are valid, so neither the token
    gate nor any validation can decide these rows -- only the `except
    ToolError` branch and the shared `STATUS_FOR_CODE` can.
    The map itself is `deps`' and is pinned as a whole by
    `test_approval_routes.py::test_the_shared_status_map_answers_a_matrix_refusal_403_and_a_kill_switch_503`;
    what these rows add is that both notification routes go THROUGH it.
    """
    caller = RefusingToolCaller(error_class(code.value))

    response, _spy = _request(
        monkeypatch=monkeypatch,
        method=method,
        path=path,
        caller=caller,
        headers=_auth(_token()),
    )

    assert response.status_code == status
    assert response.json() == {"code": code.value}
    assert caller.entered == 1


@pytest.mark.parametrize(("_id", "method", "path"), ROUTES, ids=ROUTE_IDS)
def test_an_unreachable_mcp_server_is_one_typed_internal_naming_neither_host_nor_path(
    notifications_env: None,
    monkeypatch: pytest.MonkeyPatch,
    _id: str,
    method: str,
    path: str,
) -> None:
    """Catches two failures at once: a handshake failure left to propagate (an
    untyped 500 with a traceback body), and a body that carries the client's
    message -- `TRANSPORT_FAILURE_MESSAGE` is opaque on purpose, and a route
    rendering the exception's `str` would leak the MCP host and path to a
    browser. `INTERNAL` maps to 500 in the one shared map,
    so the 500 here pins that an unreachable server renders the typed `INTERNAL`
    and leaks neither the host nor the path.
    """
    caller = UnreachableToolCaller()

    response, _spy = _request(
        monkeypatch=monkeypatch,
        method=method,
        path=path,
        caller=caller,
        headers=_auth(_token()),
    )

    assert response.status_code == 500
    assert response.json() == {"code": ErrorCode.INTERNAL.value}
    assert MCP_HOST not in response.text
    assert MCP_PATH not in response.text
    assert TRANSPORT_FAILURE_MESSAGE not in response.text


def test_a_non_uuid_notification_id_reaches_no_tool_and_no_key(
    notifications_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fourth requirement. Catches an id declared as a `str` and
    passed straight through: it would FIRST reach
    `derive_notification_read_key`, which would happily digest `"../../etc"`
    into a perfectly valid key and file it against nothing, and then reach a
    tool whose input model refuses it after the key was already stored.

    The bearer verifies here and there is no body and no query, so nothing but
    the path parameter's type can produce this refusal.
    """
    caller = ScriptedToolCaller()

    response, spy = _request(
        monkeypatch=monkeypatch,
        method="POST",
        path="/notifications/not-a-uuid/read",
        caller=caller,
        headers=_auth(_token()),
    )

    assert response.status_code == 422
    assert caller.entered == 0
    assert caller.calls == []
    assert spy.bearer_tokens == []


# --- the tool's own bound, pinned THROUGH the route (a known risk) --


@pytest.mark.parametrize("limit", [LIMIT_MIN, LIMIT_MAX], ids=["limit-1", "limit-200"])
def test_the_inclusive_ends_of_the_tools_page_size_are_accepted(
    notifications_env: None, monkeypatch: pytest.MonkeyPatch, limit: int
) -> None:
    """The INCLUSIVE ends of the `1`-`200` range.

    Catches a route that re-declares the bound on a query parameter of its own:
    a `Query(gt=1)`, a `Query(lt=200)` or any second opinion refuses one of
    these two with FastAPI's untyped 422 while the tool accepts it, and a
    person asking for a full page of messages gets an error instead. Nothing
    else in this file sends a limit at either end, and the refusal rows below
    cannot tell an inclusive bound from an exclusive one on their own.
    """
    caller = BoundCheckingToolCaller(PAGE_PAYLOAD)

    response, _spy = _request(
        monkeypatch=monkeypatch,
        method="GET",
        path=PAGE_PATH,
        caller=caller,
        headers=_auth(_token()),
        params={"limit": str(limit)},
    )

    assert response.status_code == 200
    assert len(caller.answered) == 1
    assert caller.answered[0]["limit"] == limit


@pytest.mark.parametrize("limit", [LIMIT_MIN - 1, LIMIT_MAX + 1], ids=["limit-0", "limit-201"])
def test_a_page_size_outside_the_tools_bounds_is_a_typed_400_with_no_read(
    notifications_env: None, monkeypatch: pytest.MonkeyPatch, limit: int
) -> None:
    """The other end of the same bound, one step outside it on each side.

    Catches a route that answers the tool's typed `VALIDATION` as an untyped
    500 or as FastAPI's own 422 -- the error map has one sentence per documented
    code and nothing to say for either -- and a route that silently CLAMPS the
    value into range, which would show somebody a page they did not ask for and
    hide the mistake from them. No row was read, which is what `answered` being
    empty asserts.
    """
    caller = BoundCheckingToolCaller(PAGE_PAYLOAD)

    response, _spy = _request(
        monkeypatch=monkeypatch,
        method="GET",
        path=PAGE_PATH,
        caller=caller,
        headers=_auth(_token()),
        params={"limit": str(limit)},
    )

    assert response.status_code == 400
    assert response.json() == {"code": ErrorCode.VALIDATION.value}
    assert caller.answered == []


@pytest.mark.parametrize(
    ("sent", "expected"),
    [
        pytest.param("true", True, id="true"),
        pytest.param("false", False, id="false"),
    ],
)
def test_each_value_of_unread_only_is_forwarded_as_a_bool(
    notifications_env: None, monkeypatch: pytest.MonkeyPatch, sent: str, expected: bool
) -> None:
    """The accepted half: `unread_only` is a typed `bool` query
    parameter, so both values cross the wire as bools.

    Catches a route that forwards the raw STRING -- `"false"` is truthy to
    nothing on the far side, but `ListNotificationsInput` declares a `bool` and
    a bare `"false"` string would either be coerced to True by a looser model
    or refused outright, either way showing the unread set to somebody who
    asked for everything. Also catches a route that only ever sends True, which
    the `false` row alone can see.
    """
    caller = BoundCheckingToolCaller(PAGE_PAYLOAD)

    response, _spy = _request(
        monkeypatch=monkeypatch,
        method="GET",
        path=PAGE_PATH,
        caller=caller,
        headers=_auth(_token()),
        params={"unread_only": sent},
    )

    assert response.status_code == 200
    assert len(caller.answered) == 1
    assert caller.answered[0]["unread_only"] is expected


def test_an_unparseable_unread_only_is_fastapis_own_422_with_no_read(
    notifications_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The COST of a typed bool, pinned rather than hidden (the third risk):
    `unread_only=maybe` answers FastAPI's untyped **422** and not the
    typed 400, because a bool is a TYPE the route declares and not a
    vocabulary the tool owns.

    This row makes no claim about a typed refusal. What it catches is a route
    that declares `unread_only` as a `str` and forwards whatever arrived: the
    word would then reach the tool, or be coerced to True, and somebody asking
    for `maybe` would silently be shown only their unread messages. The
    bearer verifies and no limit is sent, so only the parameter's declared type
    can decide this row.
    """
    caller = BoundCheckingToolCaller(PAGE_PAYLOAD)

    response, spy = _request(
        monkeypatch=monkeypatch,
        method="GET",
        path=PAGE_PATH,
        caller=caller,
        headers=_auth(_token()),
        params={"unread_only": "maybe"},
    )

    assert response.status_code == 422
    assert caller.answered == []
    assert caller.entered == 0
    assert spy.bearer_tokens == []


# --- the derived key, pure ----------------------------------------------------


def test_the_same_reader_and_notification_derive_the_same_key_every_time() -> None:
    """The property the idempotency check rests on. Catches a
    derivation that reaches a clock or a random source (`uuid4`,
    `datetime.now`): the second click of a double-clicked bell would then file
    a second call instead of replaying the first one's stored response.
    """
    first = derive_notification_read_key(reader_user_id=READER_ID, notification_id=NOTIFICATION_ID)
    second = derive_notification_read_key(reader_user_id=READER_ID, notification_id=NOTIFICATION_ID)

    assert first == second


@pytest.mark.parametrize(
    ("reader", "notification_id"),
    [
        pytest.param(OTHER_READER_ID, NOTIFICATION_ID, id="another-reader"),
        pytest.param(READER_ID, OTHER_NOTIFICATION_ID, id="another-notification"),
    ],
)
def test_changing_either_of_the_two_values_changes_the_key(
    reader: uuid.UUID, notification_id: uuid.UUID
) -> None:
    """The first risk, one value at a time. Each row is the failure the
    value prevents: without the reader two people stamping their own copy of
    one message would share a key and the second would replay the first's
    answer; without the notification id one reader's second stamp would replay
    their first on a DIFFERENT notification -- the collision named in the risk list,
    which leaves the bell counting a message the person has opened.
    """
    baseline = derive_notification_read_key(
        reader_user_id=READER_ID, notification_id=NOTIFICATION_ID
    )

    assert (
        derive_notification_read_key(reader_user_id=reader, notification_id=notification_id)
        != baseline
    )


def test_the_key_is_the_uuid5_of_the_two_values_under_the_modules_namespace() -> None:
    """Catches a derivation over the right two values under the WRONG namespace
    or joined with a different separator: it would be stable, unique and
    completely different from the key `graph/keys.py` documents, so every other
    case in this file passes while the module has two derivations under two
    namespaces (the whole reason for putting this function in that
    module rather than beside the routes).

    `EXPECTED_READ_FINGERPRINT` is hand-computed and written as a literal, and the
    constructive form is asserted beside it so the two cannot drift. The
    separator is pinned by construction: a bare concatenation of the same two
    uuids gives a different value, and a derivation joining them that way fails
    here.
    """
    expected = str(
        uuid.uuid5(KEY_NAMESPACE, SEPARATOR.join([str(READER_ID), str(NOTIFICATION_ID)]))
    )

    assert expected == EXPECTED_READ_FINGERPRINT
    assert (
        derive_notification_read_key(reader_user_id=READER_ID, notification_id=NOTIFICATION_ID)
        == EXPECTED_READ_FINGERPRINT
    )


def test_the_derived_key_fits_the_column_it_is_stored_in() -> None:
    """The length half: the key is length-checked by
    `check_idempotency_key` before it reaches the wire. Catches a derivation
    that returns the MATERIAL rather than a uuid over it -- a string carrying
    both uuids and the separator, which would abort the store AFTER the stamp
    had run, as a database error instead of the documented error code.
    """
    key = derive_notification_read_key(
        reader_user_id=READER_ID, notification_id=OTHER_NOTIFICATION_ID
    )

    assert len(key) == len(str(uuid.uuid5(KEY_NAMESPACE, "fake")))
    assert 0 < len(key) <= KEY_LENGTH


def test_the_key_check_is_inclusive_at_the_column_length_and_refuses_one_over() -> None:
    """The stored-value boundary, pinned at its exact value: a key of exactly `KEY_LENGTH`
    is accepted and one character over is refused as `VALIDATION` before it can reach the wire.

    NO KILL CLAIM of its own on this code. `check_idempotency_key` is
    guard, already pinned by `test_agent_planning.py`, and whether
    `derive_notification_read_key` routes THROUGH it is unobservable -- every
    derived key is a 36-character uuid, so the check can never fire from that
    caller. This row is here because the contract states the bound and wants the value written
    out;
    the case above is what pins the derivation returning a uuid at all.
    """
    longest = "k" * KEY_LENGTH

    assert check_idempotency_key(longest) == longest
    with pytest.raises(InvalidIdempotencyKeyError) as refused:
        check_idempotency_key("k" * (KEY_LENGTH + 1))
    assert refused.value.code == ErrorCode.VALIDATION
