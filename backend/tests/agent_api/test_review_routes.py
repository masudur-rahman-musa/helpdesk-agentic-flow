"""The review queue, the item read and the decision, over HTTP.

Surface under test (written before it exists; every case below is red on one
of these names until steps 1, 2 and 4 land):

``agent_api.graph.keys``
    ``derive_review_decision_key(*, reviewer_user_id: uuid.UUID, item_id:
    uuid.UUID, decision: str) -> str`` — ``uuid5(KEY_NAMESPACE, reviewer ∥
    item ∥ decision)`` over the module's existing ``\\x1f`` separator, length
    checked by ``check_idempotency_key`` before it is returned. A sibling of
    ``derive_decision_key``, never a reuse of it.

``agent_api.review_routes``
    ``router`` with ``GET /me/reviews`` (optional ``status``, optional
    ``limit``), ``GET /reviews/{review_id}/items`` (optional ``limit``) and
    ``POST /review-items/{item_id}/decide`` (body ``{"decision": "keep" |
    "revoke"}``), registered in ``main.py``. Every route goes through
    ``agent_api.deps``' existing seam — ``authenticated``, ``unauthenticated``,
    ``call_tool_route`` and ``STATUS_FOR_CODE`` — which is why every case here
    patches ``deps.build_tool_caller`` and none of them opens a socket.

Shapes this file fixes:
- the three tool names are ``list_reviews``, ``list_review_items`` and
  ``decide_review_item``, and the decision's key material is taken over the
  decision WORD itself (``"keep"`` / ``"revoke"``, the tool's own
  ``ReviewDecision`` members), never over a verb of the route's invention.
- the refusal envelope is ``{"code": "<ErrorCode value>"}`` and the bearer
  refusal a literal ``401``, the usual shape, unchanged.
- the queue's arguments are ``{}``, ``{"status": ...}``, ``{"limit": ...}`` or
  both — a subclass per shape rather than a nullable field, because
  ``model_dump`` would send ``limit: null`` into a ``1..200`` bound. The cases
  below assert the arguments DICT, so any construction answering those four
  shapes passes.
- the item read's arguments are ``{"review_id": ...}`` plus an optional
  ``limit``; the decision's are exactly ``{"item_id": ..., "decision": ...,
  "idempotency_key": ...}``.
- a non-uuid ``review_id`` or ``item_id`` is FastAPI's own ``422``: the contract makes
  both path parameters ``uuid.UUID``, so the refusal is the framework's and no
  second validator is written. What the case pins is that no tool, no key and
  no query is reached.
- the ``status``, ``decision`` and ``limit`` bounds stay the TOOL's (the contract's
  second risk), so the boundary rows here drive a caller that applies
  ``ListReviewsInput``/``DecideReviewItemInput``'s own membership and bounds
  and answers the typed ``VALIDATION`` the real MCP server answers. The route's
  job — the only thing those rows can fail on — is forwarding the word
  untouched and dressing that code as **400**, rather than declaring a second
  enum that answers FastAPI's untyped 422 for a word the tool decides.

Cost and isolation: NO backing service at all, exactly as
``test_approval_routes.py``. The lifespan is never entered — these three routes
read nothing off ``app.state`` — so nothing here opens a socket, a pool or a
Redis connection, and the key cases are pure. Fixtures live in this module
rather than a conftest, for the same reason they do there.
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
    derive_review_decision_key,
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
from helpdesk.shared.models import ReviewDecision, ReviewStatus
from helpdesk.shared.models.idempotency import KEY_LENGTH

LIST_REVIEWS = "list_reviews"
LIST_REVIEW_ITEMS = "list_review_items"
DECIDE_REVIEW_ITEM = "decide_review_item"

# The decision words, and the material the derived key is taken over. The
# TOOL's `ReviewDecision` members, written as the literals they are: the key a route sends and the
# key `derive_review_decision_key`
# returns are compared against ONE value here, whatever the route spells them.
KEEP = "keep"
REVOKE = "revoke"

OPEN_STATUS = "open"

# `graph/keys.py`'s own separator, repeated here as the literal it is: the
# module keeps it private, and the key material is the thing these cases pin.
SEPARATOR = "\x1f"

# The tool's bounds (`mcp_server.tools.reviews.LIMIT_MIN` / `LIMIT_MAX` /
# `LIMIT_DEFAULT`), written out because agent-api cannot import the mcp-server
# package. The values are written out; the second risk says these are
# the only validator, so the route must neither repeat them more narrowly nor
# refuse what they accept.
LIMIT_MIN = 1
LIMIT_MAX = 200
LIMIT_DEFAULT = 50

# Obviously fake, tests only: none of these is a credential, and
# nothing in this file connects to any of these hosts.
TEST_JWT_SIGNING_PHRASE = "review-routes-test-jwt-secret-0123456789"
OTHER_JWT_SECRET = "another-review-routes-fake-secret-01234"
MCP_URL = "http://mcp.invalid:8081/mcp"
MCP_HOST = "mcp.invalid"
MCP_PATH = "/mcp"
DATABASE_URL = "postgresql+asyncpg://fake:fake@db.invalid:5432/fake_reviews"
REDIS_URL = "redis://redis.invalid:6379/0"
QDRANT_URL = "http://qdrant.invalid:6333"

TENANT_ID = uuid.UUID("11111111-1111-5111-8111-111111111111")
REVIEWER_ID = uuid.UUID("22222222-2222-5222-8222-222222222222")
OTHER_REVIEWER_ID = uuid.UUID("2b2b2b2b-2b2b-5b2b-8b2b-2b2b2b2b2b2b")
DEPARTMENT_ID = uuid.UUID("33333333-3333-5333-8333-333333333333")
SESSION_ID = uuid.UUID("44444444-4444-5444-8444-444444444444")
REVIEW_ID = uuid.UUID("55555555-5555-5555-8555-555555555555")
ITEM_ID = uuid.UUID("66666666-6666-5666-8666-666666666666")
OTHER_ITEM_ID = uuid.UUID("6b6b6b6b-6b6b-5b6b-8b6b-6b6b6b6b6b6b")
GRANT_ID = uuid.UUID("77777777-7777-5777-8777-777777777777")

REVIEWS_PATH = "/me/reviews"
ITEMS_PATH = f"/reviews/{REVIEW_ID}/items"
DECIDE_PATH = f"/review-items/{ITEM_ID}/decide"

# A token issued nine hours ago: `issue_token` gives an 8-hour `exp`, so this
# one verifies its signature and fails on expiry alone.
EXPIRED_ISSUED_AT = datetime.now(UTC) - timedelta(hours=9)

QUEUE_PAYLOAD: Mapping[str, Any] = {
    "reviews": [
        {
            "review_id": str(REVIEW_ID),
            "period": "2026-Q2",
            "status": OPEN_STATUS,
            "due_at": "2026-06-30T23:59:00Z",
            "completed_at": None,
            "department_id": str(DEPARTMENT_ID),
            "department_name": "Fake Department",
            "system_id": None,
            "system_name": None,
            "reviewer_user_id": str(REVIEWER_ID),
            "reviewer_display_name": "Fake Person One",
            "item_count": 4,
            "decided_count": 1,
        }
    ]
}
"""One queue row, exactly as `list_reviews` answers it (its thirteen
`ReviewQueueRow` fields). The route must hand this back UNCHANGED, so the case
asserting equality is what catches a router that reshapes, filters or renames
on the way out."""

ITEMS_PAYLOAD: Mapping[str, Any] = {
    "review_id": str(REVIEW_ID),
    "items": [
        {
            "item_id": str(ITEM_ID),
            "grant_id": str(GRANT_ID),
            "flag": "non_standard",
            "decision": None,
            "decided_at": None,
            "target_display_name": "Fake Person Two",
            "role_name": "Fake Payroll Read",
            "system_name": "Fake Payroll",
            "privilege": "read",
            "granted_at": "2026-01-04T09:00:00Z",
            "expires_at": "2026-12-31T09:00:00Z",
        }
    ],
}
"""One item as 23e widened it: the eleven `ReviewItemOutput` fields."""

DECIDE_PAYLOAD: Mapping[str, Any] = {
    "item_id": str(ITEM_ID),
    "review_id": str(REVIEW_ID),
    "decision": KEEP,
    "decided_at": "2026-06-02T10:15:00Z",
    "revoked_grant_count": 0,
    "review_complete": False,
}
"""`decide_review_item`'s six `DecideReviewItemOutput` fields."""


# --- the scripted seam --------------------------------------------------------


class ScriptedToolCaller:
    """A `ToolCaller` answering one scripted payload per call, in order.

    The same stub shape `test_me_routes.py`, `test_chat_route.py` and
    `test_approval_routes.py` use: a call past the last
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

    the second risk: the tool's `ListReviewsInput`, `ListReviewItemsInput` and
    `DecideReviewItemInput` are the only validators of the status word, the
    decision word and the page size, so an unknown word or an out-of-bounds
    limit crosses back as a typed `VALIDATION` rather than being refused in
    agent-api. This caller answers exactly that — membership read off the
    `ReviewStatus` and `ReviewDecision` enums the tools declare, the limit
    against `LIMIT_MIN`/`LIMIT_MAX` — and records in `answered` only the calls
    it actually ANSWERED, which is what "no read" and "no write" mean on a seam
    that never reaches a database.
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
        status = sent.get("status")
        if status is not None and status not in {member.value for member in ReviewStatus}:
            raise RemoteValidationError("status is not a review status")
        decision = sent.get("decision")
        if decision is not None and decision not in {member.value for member in ReviewDecision}:
            raise RemoteValidationError("decision is not a review decision")
        self.answered.append(sent)
        return self._answer


@pytest.fixture
def reviews_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Obviously fake settings. Nothing here is dialled: no lifespan is entered."""
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    monkeypatch.setenv("QDRANT_URL", QDRANT_URL)
    monkeypatch.setenv("JWT_SECRET", TEST_JWT_SIGNING_PHRASE)
    monkeypatch.setenv("MCP_URL", MCP_URL)


def _token(
    *,
    user_id: uuid.UUID = REVIEWER_ID,
    secret: str = TEST_JWT_SIGNING_PHRASE,
    issued_at: datetime | None = None,
) -> str:
    """One verified-looking token for one fake reviewer and one conversation."""
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
    params: Mapping[str, str] | None = None,
    body: Mapping[str, Any] | None = None,
) -> tuple[httpx.Response, CallerSpy]:
    """One call against the app, with the tool caller replaced at the seam.

    The seam is `deps.build_tool_caller` (`call_tool_route` is built there), and the lifespan is NOT
    entered: these three routes read nothing
    off `app.state`, so entering it would buy a psycopg pool, a Redis client
    and a gateway client that no case here uses.

    Query values go through `params` rather than through the path, so a status
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
            json=None if body is None else dict(body),
        )

    return _drive(scenario), spy


# The three routes, as one table: every refusal below is asserted on all of
# them, because a guard present on two routes and missing on the third is
# exactly the hole a per-route case cannot see.
ROUTES: tuple[tuple[str, str, str, Mapping[str, Any] | None], ...] = (
    ("queue", "GET", REVIEWS_PATH, None),
    ("items", "GET", ITEMS_PATH, None),
    ("decide", "POST", DECIDE_PATH, {"decision": KEEP}),
)
ROUTE_IDS = [case[0] for case in ROUTES]


# --- the happy paths ----------------------------------------------------------


def test_the_queue_route_answers_the_tools_output_unchanged(
    reviews_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a route that reshapes, filters or renames `list_reviews`'
    structured output on the way out ("returns the tool's structured output
    unchanged"), and one that calls a different tool.

    The bearer half is the identity-from-token rule: the token the caller
    presented is forwarded VERBATIM, so RBAC, the kill switch and the audit
    event all apply to this read. A route that minted its own service token, or
    reused a configured one, fails on the identity assertion alone; a route
    that passed the reviewer's id as an ARGUMENT fails on the exact key set,
    which is what the identity-from-token rule forbids on a queue whose whole filter is the
    caller's own the contract scope.
    """
    caller = ScriptedToolCaller(QUEUE_PAYLOAD)
    token = _token()

    response, spy = _request(
        monkeypatch=monkeypatch,
        method="GET",
        path=REVIEWS_PATH,
        caller=caller,
        headers=_auth(token),
        params={"status": OPEN_STATUS, "limit": "25"},
    )

    assert response.status_code == 200
    assert response.json() == QUEUE_PAYLOAD
    assert caller.names == [LIST_REVIEWS]
    _name, arguments = caller.calls[0]
    assert arguments == {"status": OPEN_STATUS, "limit": 25}
    assert spy.bearer_tokens == [token]


def test_the_queue_route_sends_neither_status_nor_limit_when_neither_is_asked_for(
    reviews_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first requirement, both halves at once. Catches a route that repeats
    the tool's `LIMIT_DEFAULT` (50) as a number of its own — a second place for
    it to drift from — one that sends `limit: null` into a
    bound of 1..200 and gets a `VALIDATION` back, so the page shows an empty
    queue to a reviewer with rows waiting, and one that defaults the filter to
    `"open"`, which would hide every completed review a reviewer looks for
    (that is the reason for the filter).
    """
    caller = ScriptedToolCaller(QUEUE_PAYLOAD)

    response, _spy = _request(
        monkeypatch=monkeypatch,
        method="GET",
        path=REVIEWS_PATH,
        caller=caller,
        headers=_auth(_token()),
    )

    assert response.status_code == 200
    _name, arguments = caller.calls[0]
    assert arguments == {}


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        pytest.param({"status": OPEN_STATUS}, {"status": OPEN_STATUS}, id="status-only"),
        pytest.param({"limit": "25"}, {"limit": 25}, id="limit-only"),
    ],
)
def test_the_queue_route_sends_each_query_parameter_only_when_it_is_asked_for(
    reviews_env: None,
    monkeypatch: pytest.MonkeyPatch,
    params: Mapping[str, str],
    expected: Mapping[str, Any],
) -> None:
    """The two shapes between "neither" and "both", which no other case covers.

    Catches a route carrying ONE argument model with both fields nullable:
    `model_dump` sends every declared field, so asking for a status alone would
    put `limit: null` on the wire (refused by a 1..200 bound) and asking for a
    limit alone would put `status: null` where the tool's absent-status meaning
    is "every status". Only the per-shape construction the requirements name can make
    both rows pass.
    """
    caller = ScriptedToolCaller(QUEUE_PAYLOAD)

    response, _spy = _request(
        monkeypatch=monkeypatch,
        method="GET",
        path=REVIEWS_PATH,
        caller=caller,
        headers=_auth(_token()),
        params=params,
    )

    assert response.status_code == 200
    _name, arguments = caller.calls[0]
    assert arguments == expected


def test_the_item_route_answers_the_tools_output_unchanged_under_the_callers_own_bearer(
    reviews_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second requirement. Catches a route that reshapes 23e's eleven-field
    item on the way out, one that calls a different tool, one that forwards
    anything but the caller's own bearer, and one that adds an identity
    argument beside the review id — the arguments are an EXACT key set, and
    `list_review_items` declares `review_id` and `limit` and nothing else.

    The review id is asserted as the one from the PATH, which is what catches a
    route reading it from the body or from a query parameter instead.
    """
    caller = ScriptedToolCaller(ITEMS_PAYLOAD)
    token = _token()

    response, spy = _request(
        monkeypatch=monkeypatch,
        method="GET",
        path=ITEMS_PATH,
        caller=caller,
        headers=_auth(token),
    )

    assert response.status_code == 200
    assert response.json() == ITEMS_PAYLOAD
    assert caller.names == [LIST_REVIEW_ITEMS]
    _name, arguments = caller.calls[0]
    assert arguments == {"review_id": str(REVIEW_ID)}
    assert spy.bearer_tokens == [token]


def test_the_item_route_sends_the_limit_it_was_asked_for(
    reviews_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the item read's optional page size. Catches a route
    that declares `limit` and never forwards it — the reviewer asking for two
    hundred rows would silently get the tool's fifty, and 23f's paging would be
    a control that does nothing.
    """
    caller = ScriptedToolCaller(ITEMS_PAYLOAD)

    response, _spy = _request(
        monkeypatch=monkeypatch,
        method="GET",
        path=ITEMS_PATH,
        caller=caller,
        headers=_auth(_token()),
        params={"limit": "25"},
    )

    assert response.status_code == 200
    _name, arguments = caller.calls[0]
    assert arguments == {"review_id": str(REVIEW_ID), "limit": 25}


def test_the_decide_route_answers_the_tools_output_under_the_callers_own_bearer(
    reviews_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mutation half of hthe no-model-mutation and identity-from-token rules at once.

    Catches a route that reshapes the decision's output, one that calls the
    wrong tool, one that forwards anything but the caller's own bearer, and —
    the assertion no other case here makes — one that passes an IDENTITY
    argument: the arguments are an EXACT key set, so a `reviewer_user_id` the
    browser could set fails, and so does a missing `idempotency_key`, which
    would leave a double-clicked Keep filing two decisions.
    """
    caller = ScriptedToolCaller(DECIDE_PAYLOAD)
    token = _token()

    response, spy = _request(
        monkeypatch=monkeypatch,
        method="POST",
        path=DECIDE_PATH,
        caller=caller,
        headers=_auth(token),
        body={"decision": KEEP},
    )

    assert response.status_code == 200
    assert response.json() == DECIDE_PAYLOAD
    assert caller.names == [DECIDE_REVIEW_ITEM]
    _name, arguments = caller.calls[0]
    assert set(arguments) == {"item_id", "decision", "idempotency_key"}
    assert arguments["item_id"] == str(ITEM_ID)
    assert arguments["decision"] == KEEP
    assert arguments["idempotency_key"] == derive_review_decision_key(
        reviewer_user_id=REVIEWER_ID, item_id=ITEM_ID, decision=KEEP
    )
    assert spy.bearer_tokens == [token]


def test_the_key_is_derived_from_the_tokens_reviewer_and_never_from_the_body(
    reviews_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The identity requirement, the identity-from-token rule on the one value this route derives.

    The body carries a `reviewer_user_id` naming ANOTHER user of the SAME
    tenant as the caller (a cross-tenant name would be refused
    by the tenant guard on the MCP side whether this rule holds or not, so it
    would pin nothing). Catches a route that derives the key from that field
    instead of from the verified `sub` — two reviewers would then share a key
    and the second decision would replay the first's stored answer — and a
    route that forwards the field as a tool argument, which the exact key set
    refuses.

    Only the source of the reviewer can flip this case: the token verifies, the
    path parameter is a real uuid and the decision word is a real member.
    """
    caller = ScriptedToolCaller(DECIDE_PAYLOAD)

    response, _spy = _request(
        monkeypatch=monkeypatch,
        method="POST",
        path=DECIDE_PATH,
        caller=caller,
        headers=_auth(_token()),
        body={"decision": KEEP, "reviewer_user_id": str(OTHER_REVIEWER_ID)},
    )

    assert response.status_code == 200
    _name, arguments = caller.calls[0]
    assert set(arguments) == {"item_id", "decision", "idempotency_key"}
    assert arguments["idempotency_key"] == derive_review_decision_key(
        reviewer_user_id=REVIEWER_ID, item_id=ITEM_ID, decision=KEEP
    )
    assert arguments["idempotency_key"] != derive_review_decision_key(
        reviewer_user_id=OTHER_REVIEWER_ID, item_id=ITEM_ID, decision=KEEP
    )


def test_one_reviewer_deciding_one_item_twice_sends_one_key_and_changing_the_word_changes_it(
    reviews_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The idempotency requirement, both sentences of it, through the ROUTE.

    Catches a key minted fresh per call (`uuid4`) or reaching a clock: a
    double-clicked Keep would then arrive under two keys and file two
    decisions. And it catches a key that leaves the decision WORD out of its
    material: a later Revoke would arrive under the Keep's key and be handed
    the stored Keep back as if the change of mind had worked, instead of
    meeting the tool's own re-decision refusal.

    Everything but the decision word is identical across the three calls — same
    bearer, same path, same item — so only the material can flip either half.
    """
    caller = ScriptedToolCaller(DECIDE_PAYLOAD, DECIDE_PAYLOAD, DECIDE_PAYLOAD)
    token = _token()

    for decision in (KEEP, KEEP, REVOKE):
        response, _spy = _request(
            monkeypatch=monkeypatch,
            method="POST",
            path=DECIDE_PATH,
            caller=caller,
            headers=_auth(token),
            body={"decision": decision},
        )
        assert response.status_code == 200

    first, second, third = (arguments for _name, arguments in caller.calls)
    assert first["idempotency_key"] == second["idempotency_key"]
    assert third["idempotency_key"] != first["idempotency_key"]


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
    reviews_env: None,
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
        the path parameter is a real uuid, there is no query to validate, no rate
        limit and no DB read — so deleting it is what flips every row here
    .
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
    reviews_env: None,
    monkeypatch: pytest.MonkeyPatch,
    _id: str,
    method: str,
    path: str,
    body: Mapping[str, Any] | None,
) -> None:
    """Catches a route that reads the claims without VERIFYING the signature:
    this token is well formed and carries a real tenant, a real user and a real
    `sid` — it is signed with the wrong secret and nothing else, so only the
    verification can refuse it. A route that parsed the claims and trusted them
    would revoke somebody's access on the say-so of a token anybody can write.
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
    reviews_env: None,
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
    deciding review items is a session that never ends.

    On the decide route the token is verified twice — once for the reviewer the
    key is derived from, once inside `call_tool_route` — so this row pins the
    ANSWER and claims no kill on either copy: two
    copies of one guard cannot be killed separately.
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


@pytest.mark.parametrize(
    ("error_class", "code", "status"),
    [
        pytest.param(RemoteForbiddenError, ErrorCode.FORBIDDEN, 403, id="forbidden-403"),
        pytest.param(RemoteToolDisabledError, ErrorCode.TOOL_DISABLED, 503, id="tool-disabled-503"),
        pytest.param(RemoteNotFoundError, ErrorCode.NOT_FOUND, 404, id="not-found-404"),
    ],
)
@pytest.mark.parametrize(("_id", "method", "path", "body"), ROUTES, ids=ROUTE_IDS)
def test_a_tool_refusal_answers_its_own_typed_code(
    reviews_env: None,
    monkeypatch: pytest.MonkeyPatch,
    _id: str,
    method: str,
    path: str,
    body: Mapping[str, Any] | None,
    error_class: type[RemoteToolError],
    code: ErrorCode,
    status: int,
) -> None:
    """the second unhappy path, one code per row. Catches a route that lets
    the tool's `ToolError` escape: FastAPI answers an untyped 500, so a scope
    refusal the caller could act on, an admin's kill switch the page should
    name, and an unknown review id all read as one outage.

    The bearer here VERIFIES and the arguments are valid, so neither the token
    gate nor any validation can decide these rows — only the `except ToolError`
    branch and the shared `STATUS_FOR_CODE` can. The map
    itself is `deps`' and is pinned as a whole by
    `test_approval_routes.py::test_the_shared_status_map_answers_a_matrix_refusal_403_and_a_kill_switch_503`;
    what these rows add is that all three review routes go THROUGH it.
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
    reviews_env: None,
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
        body=body,
    )

    assert response.status_code == 500
    assert response.json() == {"code": ErrorCode.INTERNAL.value}
    assert MCP_HOST not in response.text
    assert MCP_PATH not in response.text
    assert TRANSPORT_FAILURE_MESSAGE not in response.text


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        pytest.param("GET", "/reviews/not-a-uuid/items", None, id="review-id"),
        pytest.param("POST", "/review-items/not-a-uuid/decide", {"decision": KEEP}, id="item-id"),
    ],
)
def test_a_non_uuid_path_parameter_reaches_no_tool_and_no_key(
    reviews_env: None,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    body: Mapping[str, Any] | None,
) -> None:
    """The fifth requirement. Catches an id declared as a `str` and passed
    straight through: on the item read it would reach the tool as a string its
    input model refuses, and on the decision it would FIRST reach
    `derive_review_decision_key`, which would happily digest `"../../etc"` into
    a perfectly valid key and file it against nothing.

    The bearer verifies here and the body is well formed, so neither the token
    gate nor the decision word can produce this refusal — only the path
    parameter's type can.
    """
    caller = ScriptedToolCaller()

    response, spy = _request(
        monkeypatch=monkeypatch,
        method=method,
        path=path,
        caller=caller,
        headers=_auth(_token()),
        body=body,
    )

    assert response.status_code == 422
    assert caller.entered == 0
    assert caller.calls == []
    assert spy.bearer_tokens == []


# --- the tool's own bounds, pinned THROUGH the route (the second risk) --------


@pytest.mark.parametrize("limit", [LIMIT_MIN, LIMIT_MAX], ids=["limit-1", "limit-200"])
@pytest.mark.parametrize(
    ("_id", "path", "answer"),
    [
        pytest.param("queue", REVIEWS_PATH, QUEUE_PAYLOAD, id="queue"),
        pytest.param("items", ITEMS_PATH, ITEMS_PAYLOAD, id="items"),
    ],
)
def test_the_inclusive_ends_of_the_tools_page_size_are_accepted(
    reviews_env: None,
    monkeypatch: pytest.MonkeyPatch,
    _id: str,
    path: str,
    answer: Mapping[str, Any],
    limit: int,
) -> None:
    """The INCLUSIVE ends of the contract's `1`-`200`, on both reads.

    Catches a route that re-declares the bound on a query parameter of its own:
    a `Query(gt=1)`, a `Query(lt=200)` or any second opinion refuses one of
    these two with FastAPI's untyped 422 while the tool accepts it, and a
    reviewer asking for a full page gets an error instead. Nothing else in this
    file sends a limit at either end, and the refusal rows below cannot tell an
    inclusive bound from an exclusive one on their own.
    """
    caller = BoundCheckingToolCaller(answer)

    response, _spy = _request(
        monkeypatch=monkeypatch,
        method="GET",
        path=path,
        caller=caller,
        headers=_auth(_token()),
        params={"limit": str(limit)},
    )

    assert response.status_code == 200
    assert len(caller.answered) == 1
    assert caller.answered[0]["limit"] == limit


@pytest.mark.parametrize("limit", [LIMIT_MIN - 1, LIMIT_MAX + 1], ids=["limit-0", "limit-201"])
@pytest.mark.parametrize(
    ("_id", "path", "answer"),
    [
        pytest.param("queue", REVIEWS_PATH, QUEUE_PAYLOAD, id="queue"),
        pytest.param("items", ITEMS_PATH, ITEMS_PAYLOAD, id="items"),
    ],
)
def test_a_page_size_outside_the_tools_bounds_is_a_typed_400_with_no_read(
    reviews_env: None,
    monkeypatch: pytest.MonkeyPatch,
    _id: str,
    path: str,
    answer: Mapping[str, Any],
    limit: int,
) -> None:
    """The other end of the same bound, one step outside it on each side.

    Catches a route that answers the tool's typed `VALIDATION` as an untyped
    500 or as FastAPI's own 422 — the page has one sentence per the contract code
    and nothing to say for either — and a route that silently CLAMPS the value
    into range, which would show a reviewer a page they did not ask for and
    hide the mistake from them. No row was read, which is what `answered`
    being empty asserts.
    """
    caller = BoundCheckingToolCaller(answer)

    response, _spy = _request(
        monkeypatch=monkeypatch,
        method="GET",
        path=path,
        caller=caller,
        headers=_auth(_token()),
        params={"limit": str(limit)},
    )

    assert response.status_code == 400
    assert response.json() == {"code": ErrorCode.VALIDATION.value}
    assert caller.answered == []


@pytest.mark.parametrize(
    "status",
    ["OPEN", "open ", "archived"],
    ids=["wrong-case", "trailing-space", "unknown-word"],
)
def test_a_status_that_is_not_a_tool_enum_member_is_a_typed_400_with_no_read(
    reviews_env: None, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    """The sixth requirement, the three rows it names. `ReviewStatus` is a
    StrEnum and membership is EXACT, so the wrong case, a trailing space and an
    unknown word are each `VALIDATION` before any read.

    Catches a route that lower-cases, strips or otherwise repairs the word on
    the way through — `"open "` would then answer somebody's open reviews for a
    query the tool refuses, and the page would never learn its filter was
    wrong — and a route that declares a second enum of its own, which answers
    FastAPI's untyped 422 where the contract's `VALIDATION` is what 23f has a
    sentence for.

    The bearer verifies and no limit is sent, so only the forwarded word can
    decide these rows.
    """
    caller = BoundCheckingToolCaller(QUEUE_PAYLOAD)

    response, _spy = _request(
        monkeypatch=monkeypatch,
        method="GET",
        path=REVIEWS_PATH,
        caller=caller,
        headers=_auth(_token()),
        params={"status": status},
    )

    assert response.status_code == 400
    assert response.json() == {"code": ErrorCode.VALIDATION.value}
    assert caller.answered == []


@pytest.mark.parametrize("decision", [KEEP, REVOKE], ids=[KEEP, REVOKE])
def test_each_tool_decision_word_is_forwarded_untouched_and_keyed_over_itself(
    reviews_env: None, monkeypatch: pytest.MonkeyPatch, decision: str
) -> None:
    """Both members of `ReviewDecision`, accepted through the route.

    Catches a route that declares a narrower vocabulary of its own (one that
    only knows `keep`, say, refuses `revoke` with an untyped 422 and the page's
    Revoke button never works) and one that maps the word to something else on
    the way out. The key is asserted over the SAME word that went on the wire,
    which is what keeps "changing the decision changes the key"
    true at the route rather than only in the pure function.
    """
    caller = BoundCheckingToolCaller(DECIDE_PAYLOAD)

    response, _spy = _request(
        monkeypatch=monkeypatch,
        method="POST",
        path=DECIDE_PATH,
        caller=caller,
        headers=_auth(_token()),
        body={"decision": decision},
    )

    assert response.status_code == 200
    assert len(caller.answered) == 1
    assert caller.answered[0]["decision"] == decision
    assert caller.answered[0]["idempotency_key"] == derive_review_decision_key(
        reviewer_user_id=REVIEWER_ID, item_id=ITEM_ID, decision=decision
    )


@pytest.mark.parametrize(
    "decision",
    ["KEEP", "keep ", "delete", ""],
    ids=["wrong-case", "trailing-space", "unknown", "empty"],
)
def test_a_decision_that_is_not_a_tool_enum_member_is_a_typed_400_with_no_write(
    reviews_env: None, monkeypatch: pytest.MonkeyPatch, decision: str
) -> None:
    """The sixth requirement on the mutation. `ReviewDecision` is a StrEnum and
    membership is EXACT, so each of these four is `VALIDATION` before any
    effect runs.

    Catches a route that repairs the word (a stripped `"keep "` would revoke or
    keep somebody's access on a request the tool would have refused), one that
    defaults an unknown word to a member of its own choosing — the worst of the
    four, because `revoke` ends access — and one that answers the typed code as
    an untyped 422 or 500. `answered` being empty is what "no write" means on a
    seam that never reaches a database.

    The bearer verifies and the item id is a real uuid, so only the forwarded
    word can decide these rows.
    """
    caller = BoundCheckingToolCaller(DECIDE_PAYLOAD)

    response, _spy = _request(
        monkeypatch=monkeypatch,
        method="POST",
        path=DECIDE_PATH,
        caller=caller,
        headers=_auth(_token()),
        body={"decision": decision},
    )

    assert response.status_code == 400
    assert response.json() == {"code": ErrorCode.VALIDATION.value}
    assert caller.answered == []


# --- the derived key, pure ----------------------------------------------------


def test_the_same_reviewer_item_and_decision_derive_the_same_key_every_time() -> None:
    """The property the idempotency requirement rests on. Catches a derivation
    that reaches a clock or a random source (`uuid4`, `datetime.now`): the
    second Keep of a double click would then file a second decision instead of
    replaying the first one's stored response.
    """
    first = derive_review_decision_key(reviewer_user_id=REVIEWER_ID, item_id=ITEM_ID, decision=KEEP)
    second = derive_review_decision_key(
        reviewer_user_id=REVIEWER_ID, item_id=ITEM_ID, decision=KEEP
    )

    assert first == second


@pytest.mark.parametrize(
    ("reviewer", "item_id", "decision"),
    [
        pytest.param(OTHER_REVIEWER_ID, ITEM_ID, KEEP, id="another-reviewer"),
        pytest.param(REVIEWER_ID, OTHER_ITEM_ID, KEEP, id="another-item"),
        pytest.param(REVIEWER_ID, ITEM_ID, REVOKE, id="another-decision"),
    ],
)
def test_changing_any_one_of_the_three_values_changes_the_key(
    reviewer: uuid.UUID, item_id: uuid.UUID, decision: str
) -> None:
    """The first risk, one value at a time. Each row is the failure the value
    prevents: without the reviewer two reviewers deciding one item would share
    a key and the second would replay the first's answer; without the item id
    one reviewer's second decision would replay their first on a DIFFERENT item
    — the collision the contract names, which would end somebody else's access or spare
    it; without the decision word a later Revoke would be handed the stored
    Keep back instead of meeting the tool's own re-decision refusal.
    """
    baseline = derive_review_decision_key(
        reviewer_user_id=REVIEWER_ID, item_id=ITEM_ID, decision=KEEP
    )

    assert (
        derive_review_decision_key(reviewer_user_id=reviewer, item_id=item_id, decision=decision)
        != baseline
    )


def test_the_key_is_the_uuid5_of_the_three_values_under_the_modules_namespace() -> None:
    """Catches a derivation over the right three values under the WRONG
    namespace or joined with a different separator: it would be stable, unique
    and completely different from the key `graph/keys.py` documents, so every
    other case in this file passes while the module has two derivations under
    two namespaces (the whole reason for putting this function in that
    module rather than beside the routes).

    The separator is asserted by construction: a join on `\\x1f` is the only one
    under which the pair below cannot collide with any other pair of uuids, and
    a derivation concatenating the three values bare fails here.
    """
    expected = str(
        uuid.uuid5(KEY_NAMESPACE, SEPARATOR.join([str(REVIEWER_ID), str(ITEM_ID), KEEP]))
    )

    assert (
        derive_review_decision_key(reviewer_user_id=REVIEWER_ID, item_id=ITEM_ID, decision=KEEP)
        == expected
    )


def test_the_derived_key_fits_the_column_it_is_stored_in() -> None:
    """The contract's length half: the key is length-checked by `check_idempotency_key`
    before it reaches the wire. Catches a derivation that returns the MATERIAL
    rather than a uuid over it — a string that grows with the decision word and
    would abort the store AFTER the decision had run, as a database error
    instead of the code the contract names.
    """
    key = derive_review_decision_key(reviewer_user_id=REVIEWER_ID, item_id=ITEM_ID, decision=REVOKE)

    assert len(key) == len(str(uuid.uuid5(KEY_NAMESPACE, "fake")))
    assert 0 < len(key) <= KEY_LENGTH


def test_the_key_check_is_inclusive_at_the_column_length_and_refuses_one_over() -> None:
    """The contract's boundary, pinned at its exact value: a key of
    exactly `KEY_LENGTH` is accepted and one character over is refused as
    `VALIDATION` before it can reach the wire.

    NO KILL CLAIM of its own on this code. `check_idempotency_key` is
    guard, already pinned by `test_agent_planning.py`, and whether
    `derive_review_decision_key` routes THROUGH it is unobservable — every
    derived key is a 36-character uuid, so the check can never fire from that
    caller. This row is here because the requirements state the bound and want the value written
    out;
    the case above is what pins the derivation returning a uuid at all.
    """
    longest = "k" * KEY_LENGTH

    assert check_idempotency_key(longest) == longest
    with pytest.raises(InvalidIdempotencyKeyError) as refused:
        check_idempotency_key("k" * (KEY_LENGTH + 1))
    assert refused.value.code == ErrorCode.VALIDATION
