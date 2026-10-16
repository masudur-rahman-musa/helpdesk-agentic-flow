"""An earlier change section 6: the audit search, over HTTP.

Surface under test (written before it exists; every case below is red on one
of these names until step 1 lands):

``agent_api.audit_routes`` (step 1)
    ``router`` with ``GET /admin/audit/search`` (optional ``event_type``,
    ``subject_type``, ``subject_id``, ``actor_ref``, ``occurred_from``,
    ``occurred_to``, ``before_seq``, ``limit``), registered in ``main.py``.
    The route goes through ``agent_api.deps``' existing seam -
    ``authenticated``, ``unauthenticated``, ``call_tool_route`` and
    ``STATUS_FOR_CODE`` (decision B) - which is why every case here patches
    ``deps.build_tool_caller`` and none of them opens a socket.

Shapes this file fixes that the plan left open (a project rule and 55;
these belong in 24a's Deviations):
- the tool name is ``search_audit``, an earlier change's own registry name.
- the refusal envelope is ``{"code": "<ErrorCode value>"}`` and the bearer
  refusal a literal ``401``, 23a/23c/23h's shape, unchanged.
- the arguments are ``{}`` or a SUBSET of the tool's eight keys, carried by a
  subclass per shape rather than by one model with eight nullable fields,
  because ``model_dump`` would otherwise send ``limit: null`` into a ``1``-``200``
  bound and ``event_type: null`` where the tool's absent-filter meaning is
  "every event type" (23c's reason, section 6's own note).
- ``occurred_from`` and ``occurred_to`` cross the route as the STRINGS the
  browser sent, never as a ``datetime`` the route parses: section 1 asks a
  non-ISO value to answer the typed ``400 VALIDATION``, and a route declaring
  its own ``datetime`` query parameter answers FastAPI's untyped 422 instead
  (section 5's first risk). ``before_seq`` and ``limit`` cross as ints, whose
  bounds - not whose type - are the tool's to judge.
- the caps and the page-size bounds are IMPORTED from
  ``mcp_server.tools.read`` rather than retyped. That import
  needs no ``sys.path`` insert: ``mcp-server`` is a workspace package, and
  ``test_decision_card.py`` already imports ``mcp_server.tools.access`` the
  same bare way (a project rule is about ``mcp_fixtures``, a test module).
- the bounds themselves are pinned THROUGH the route by a caller that applies
  ``SearchAuditInput`` itself and answers the typed ``VALIDATION`` the real MCP
  server answers (section 5's first risk, 23h's shape): the route's job - the
  only thing those rows can fail on - is forwarding the value untouched and
  dressing that code as **400**.

Cost and isolation: NO backing service at all, exactly as
``test_review_routes.py``. The lifespan is never entered - this route reads
nothing off ``app.state`` - so nothing here opens a socket, a pool or a Redis
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
from helpdesk.agent_api.services import tool_proxy as deps
from helpdesk.agent_api.services.mcp_client import (
    TRANSPORT_FAILURE_MESSAGE,
    RemoteForbiddenError,
    RemoteInternalError,
    RemoteToolDisabledError,
    RemoteToolError,
    RemoteValidationError,
    ToolCaller,
)
from helpdesk.mcp_server.tools.read import (
    ACTOR_REF_MAX_LENGTH,
    EVENT_TYPE_MAX_LENGTH,
    SEARCH_LIMIT_MAX,
    SEARCH_LIMIT_MIN,
    SUBJECT_ID_MAX_LENGTH,
    SUBJECT_TYPE_MAX_LENGTH,
    SearchAuditInput,
)
from helpdesk.shared.auth.jwt import issue_token
from helpdesk.shared.core.config import Settings
from helpdesk.shared.core.errors import ErrorCode

SEARCH_AUDIT = "search_audit"

SEARCH_PATH = "/admin/audit/search"

# The lowest `audit_events.seq` a cursor can name (`read.py::_MIN_SEQ`, which
# the module keeps private). Written as the literal it is, a project rule:
# section 1 fixes `before_seq=1` accepted and `0` refused.
MIN_BEFORE_SEQ = 1

# Obviously fake, tests only: none of these is a credential, and
# nothing in this file connects to any of these hosts.
TEST_JWT_SIGNING_PHRASE = "audit-routes-test-jwt-secret-0123456789"
OTHER_JWT_SECRET = "another-audit-routes-fake-secret-01234"
MCP_URL = "http://mcp.invalid:8081/mcp"
MCP_HOST = "mcp.invalid"
MCP_PATH = "/mcp"
DATABASE_URL = "postgresql+asyncpg://fake:fake@db.invalid:5432/fake_audit"
REDIS_URL = "redis://redis.invalid:6379/0"
QDRANT_URL = "http://qdrant.invalid:6333"

TENANT_ID = uuid.UUID("11111111-1111-5111-8111-111111111111")
AUDITOR_ID = uuid.UUID("22222222-2222-5222-8222-222222222222")
DEPARTMENT_ID = uuid.UUID("33333333-3333-5333-8333-333333333333")
SESSION_ID = uuid.UUID("44444444-4444-5444-8444-444444444444")
REQUEST_ID = uuid.UUID("55555555-5555-5555-8555-555555555555")

# A token issued nine hours ago: `issue_token` gives an 8-hour `exp`, so this
# one verifies its signature and fails on expiry alone.
EXPIRED_ISSUED_AT = datetime.now(UTC) - timedelta(hours=9)

# The eight filters, as one browser would send them. Every value is inside the
# tool's own bounds, so the happy path can fail on the forwarding alone.
ALL_PARAMS: Mapping[str, str] = {
    "event_type": "access_request.created",
    "subject_type": "access_request",
    "subject_id": str(REQUEST_ID),
    "actor_ref": "fake.person@example.invalid",
    "occurred_from": "2026-01-01T00:00:00Z",
    "occurred_to": "2026-02-01T00:00:00Z",
    "before_seq": "900",
    "limit": "25",
}

ALL_ARGUMENTS: Mapping[str, Any] = {
    "event_type": "access_request.created",
    "subject_type": "access_request",
    "subject_id": str(REQUEST_ID),
    "actor_ref": "fake.person@example.invalid",
    "occurred_from": "2026-01-01T00:00:00Z",
    "occurred_to": "2026-02-01T00:00:00Z",
    "before_seq": 900,
    "limit": 25,
}
"""The same eight on the wire: the two timestamps as the browser's own strings
(the tool's model parses them), the cursor and the page size as ints."""

SEARCH_PAYLOAD: Mapping[str, Any] = {
    "events": [
        {
            "seq": 901,
            "occurred_at": "2026-01-15T09:30:00Z",
            "actor_kind": "user",
            "actor_ref": "fake.person@example.invalid",
            "event_type": "access_request.created",
            "subject_type": "access_request",
            "subject_id": str(REQUEST_ID),
            "payload": {"risk_class": "medium", "role_name": "Fake Payroll Read"},
            "hash": "0" * 64,
        }
    ],
    "next_before_seq": 901,
}
"""One page exactly as `search_audit` answers it: the nine `AuditEventRow`
fields and the cursor. The route must hand this back UNCHANGED, so the case
asserting equality is what catches a router that reshapes, filters, redacts or
renames on the way out."""


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

    Section 5's first risk: `SearchAuditInput` is the ONLY validator of the
    four column-derived caps, the `1`-`200` page size, the `before_seq` floor
    and the two timestamps, so an over-long filter, an out-of-bounds limit and
    a non-ISO date each cross back as a typed `VALIDATION` rather than being
    refused inside agent-api. This caller answers exactly that - it runs the
    tool's own model over what the route sent - and records in `answered` only
    the calls it actually ANSWERED, which is what "no read" means on a seam
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
        try:
            SearchAuditInput.model_validate(sent)
        except ValidationError as exc:
            raise RemoteValidationError("the search filters are out of bounds") from exc
        self.answered.append(sent)
        return self._answer


@pytest.fixture
def audit_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Obviously fake settings. Nothing here is dialled: no lifespan is entered."""
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    monkeypatch.setenv("QDRANT_URL", QDRANT_URL)
    monkeypatch.setenv("JWT_SECRET", TEST_JWT_SIGNING_PHRASE)
    monkeypatch.setenv("MCP_URL", MCP_URL)


def _token(
    *,
    user_id: uuid.UUID = AUDITOR_ID,
    secret: str = TEST_JWT_SIGNING_PHRASE,
    issued_at: datetime | None = None,
) -> str:
    """One verified-looking token for one fake auditor and one conversation."""
    return issue_token(
        {
            "sub": str(user_id),
            "tenant_id": str(TENANT_ID),
            "department_id": str(DEPARTMENT_ID),
            "principals": ["auditor"],
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


def _get(
    *,
    monkeypatch: pytest.MonkeyPatch,
    caller: ToolCaller,
    headers: Mapping[str, str] | None = None,
    params: Mapping[str, str] | None = None,
) -> tuple[httpx.Response, CallerSpy]:
    """One search against the app, with the tool caller replaced at the seam.

    The seam is `deps.build_tool_caller` (23c decision C put `call_tool_route`
    there), and the lifespan is NOT entered: this route reads nothing off
    `app.state`, so entering it would buy a psycopg pool, a Redis client and a
    gateway client that no case here uses.

    Query values go through `params` rather than through the path, so a filter
    holding a space or a non-ISO date is percent-encoded by httpx and reaches
    the route as the string the browser actually sent.
    """
    spy = CallerSpy(caller)
    monkeypatch.setattr(deps, "build_tool_caller", spy.build)

    async def scenario(client: httpx.AsyncClient) -> httpx.Response:
        return await client.get(
            SEARCH_PATH,
            headers=dict(headers or {}),
            params=None if params is None else dict(params),
        )

    return _drive(scenario), spy


# --- the happy paths ----------------------------------------------------------


def test_the_search_route_answers_the_tools_output_unchanged(
    audit_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a route that reshapes, filters, redacts or renames
    `search_audit`'s structured output on the way out (section 1: "returns the
    tool's structured output unchanged"), and one that calls a different tool.

    The bearer half is a project rule and decision A: the token the caller
    presented is forwarded VERBATIM, so RBAC, the kill switch, the per-row
    scope filter and the audit event all apply to this read. A route that
    minted its own service token, or reused a configured one, fails on the
    identity assertion alone.

    The `next_before_seq` half is section 5's fourth risk: the scope filter
    runs after the read, so a page can be short while matching events exist
    further back, and the cursor is the only way to reach them. A route that
    dropped it passes every other case in this file.
    """
    caller = ScriptedToolCaller(SEARCH_PAYLOAD)
    token = _token()

    response, spy = _get(
        monkeypatch=monkeypatch,
        caller=caller,
        headers=_auth(token),
        params=ALL_PARAMS,
    )

    assert response.status_code == 200
    assert response.json() == SEARCH_PAYLOAD
    assert caller.names == [SEARCH_AUDIT]
    assert spy.bearer_tokens == [token]


def test_the_search_route_sends_the_eight_filters_and_no_identity_argument(
    audit_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Section 1's fourth criterion, a project rule, as an EXACT key set.

    Catches a route that passes the caller's tenant or user id along as an
    argument - `search_audit`'s scope is decided from the token on the MCP
    side, so an argument riding along is a filter the browser could set on a
    read that spans other people's events - and a route that renames, drops or
    invents a filter on the way through, which agent-api's far side would
    ignore while answering a page nobody asked for.

    Every one of the eight values here is inside the tool's own bounds and the
    bearer verifies, so only the forwarding can flip this case.
    """
    caller = ScriptedToolCaller(SEARCH_PAYLOAD)

    response, _spy = _get(
        monkeypatch=monkeypatch,
        caller=caller,
        headers=_auth(_token()),
        params=ALL_PARAMS,
    )

    assert response.status_code == 200
    _name, arguments = caller.calls[0]
    assert arguments == dict(ALL_ARGUMENTS)
    assert set(arguments) == set(ALL_ARGUMENTS)


def test_the_search_route_sends_no_argument_at_all_when_no_filter_is_asked_for(
    audit_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Section 1's first criterion, both halves at once. Catches a route that
    repeats the tool's `SEARCH_LIMIT_DEFAULT` (50) as a number of its own - a
    second place for it to drift from, a project rule - one that sends
    `limit: null` into a bound of `1`-`200` and gets a `VALIDATION` back, so
    the auditor's first page is an error instead of the log, and one that sends
    `event_type: null` where the tool's absent-filter meaning is "every event
    type".
    """
    caller = ScriptedToolCaller(SEARCH_PAYLOAD)

    response, _spy = _get(monkeypatch=monkeypatch, caller=caller, headers=_auth(_token()))

    assert response.status_code == 200
    _name, arguments = caller.calls[0]
    assert arguments == {}


@pytest.mark.parametrize(
    ("name", "sent", "expected"),
    [
        pytest.param("event_type", "access_request.created", "access_request.created", id="event"),
        pytest.param("subject_type", "access_request", "access_request", id="subject-type"),
        pytest.param("subject_id", str(REQUEST_ID), str(REQUEST_ID), id="subject-id"),
        pytest.param(
            "actor_ref", "fake.person@example.invalid", "fake.person@example.invalid", id="actor"
        ),
        pytest.param("occurred_from", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", id="from"),
        pytest.param("occurred_to", "2026-02-01T00:00:00Z", "2026-02-01T00:00:00Z", id="to"),
        pytest.param("before_seq", "900", 900, id="cursor"),
        pytest.param("limit", "25", 25, id="limit"),
    ],
)
def test_each_filter_present_sends_exactly_its_own_key_and_no_other(
    audit_env: None,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    sent: str,
    expected: Any,
) -> None:
    """The eight shapes between "none" and "all eight", which no other case in
    this file covers.

    Catches a route carrying ONE argument model with eight nullable fields:
    `model_dump` sends every declared field, so asking for an event type alone
    would put `limit: null` on the wire (refused by a `1`-`200` bound) and
    `occurred_from: null` beside it, and every single-filter search the page
    can make would answer `VALIDATION`. Only the per-shape construction section
    6 names can make all eight rows pass.

    The two timestamp rows are also what catches a route that PARSES the date
    itself: a `datetime` on the wire dumps as pydantic's own rendering
    (`2026-01-01T00:00:00+00:00`), not as the string the browser sent, and the
    tool's model is then reading a value this route reformatted.
    """
    caller = ScriptedToolCaller(SEARCH_PAYLOAD)

    response, _spy = _get(
        monkeypatch=monkeypatch,
        caller=caller,
        headers=_auth(_token()),
        params={name: sent},
    )

    assert response.status_code == 200
    _name, arguments = caller.calls[0]
    assert arguments == {name: expected}


# --- the tool's own caps and bounds, pinned THROUGH the route (section 5) -----


CAPS: tuple[tuple[str, str, int], ...] = (
    ("event_type", "event_type", EVENT_TYPE_MAX_LENGTH),
    ("subject_type", "subject_type", SUBJECT_TYPE_MAX_LENGTH),
    ("subject_id", "subject_id", SUBJECT_ID_MAX_LENGTH),
    ("actor_ref", "actor_ref", ACTOR_REF_MAX_LENGTH),
)
"""The four column-derived caps, IMPORTED rather than retyped (standing rule
1): `read.py::_column_length` reads each one off the `audit_events` column, so
a widened column moves both ends of every boundary row below with it."""

CAP_IDS = [case[0] for case in CAPS]


@pytest.mark.parametrize(("_id", "name", "cap"), CAPS, ids=CAP_IDS)
def test_a_string_filter_of_exactly_the_column_cap_is_accepted(
    audit_env: None, monkeypatch: pytest.MonkeyPatch, _id: str, name: str, cap: int
) -> None:
    """The INCLUSIVE end of section 1's second criterion, one row per filter.

    Catches a route that re-declares the cap on a query parameter of its own: a
    `Query(max_length=cap - 1)`, or any second opinion narrower than the
    column, refuses a value the tool and the store both accept, with FastAPI's
    untyped 422 - and the auditor searching for a subject id at full length
    gets an error the page has no sentence for. Nothing else in this file sends
    a value at a cap, and the refusal rows below cannot tell an inclusive bound
    from an exclusive one on their own.
    """
    value = "x" * cap
    caller = BoundCheckingToolCaller(SEARCH_PAYLOAD)

    response, _spy = _get(
        monkeypatch=monkeypatch,
        caller=caller,
        headers=_auth(_token()),
        params={name: value},
    )

    assert response.status_code == 200
    assert len(caller.answered) == 1
    assert caller.answered[0][name] == value


@pytest.mark.parametrize(("_id", "name", "cap"), CAPS, ids=CAP_IDS)
def test_a_string_filter_one_character_over_the_cap_is_a_typed_400_with_no_read(
    audit_env: None, monkeypatch: pytest.MonkeyPatch, _id: str, name: str, cap: int
) -> None:
    """The other end of the same boundary, one character outside it.

    Catches a route that answers the tool's typed `VALIDATION` as an untyped
    500 or as FastAPI's own 422 - the page has one sentence per SPEC section
    8.1 code and nothing to say for either (section 5's first risk) - and a
    route that silently TRUNCATES the value into the cap, which would answer a
    search the auditor did not run and hide the mistake from them. `answered`
    being empty is what "no read reached a tool" means on a seam that never
    touches a database.

    The bearer verifies and no other filter is sent, so only the forwarded
    string can decide this row.
    """
    caller = BoundCheckingToolCaller(SEARCH_PAYLOAD)

    response, _spy = _get(
        monkeypatch=monkeypatch,
        caller=caller,
        headers=_auth(_token()),
        params={name: "x" * (cap + 1)},
    )

    assert response.status_code == 400
    assert response.json() == {"code": ErrorCode.VALIDATION.value}
    assert caller.answered == []


@pytest.mark.parametrize(
    "limit", [SEARCH_LIMIT_MIN, SEARCH_LIMIT_MAX], ids=["limit-1", "limit-200"]
)
def test_the_inclusive_ends_of_the_tools_page_size_are_accepted(
    audit_env: None, monkeypatch: pytest.MonkeyPatch, limit: int
) -> None:
    """The INCLUSIVE ends of section 1's `1`-`200`.

    Catches a route that re-declares the bound on a query parameter of its own:
    a `Query(gt=1)`, a `Query(lt=200)` or any second opinion refuses one of
    these two with FastAPI's untyped 422 while the tool accepts it, and an
    auditor asking for a full page gets an error instead.
    """
    caller = BoundCheckingToolCaller(SEARCH_PAYLOAD)

    response, _spy = _get(
        monkeypatch=monkeypatch,
        caller=caller,
        headers=_auth(_token()),
        params={"limit": str(limit)},
    )

    assert response.status_code == 200
    assert len(caller.answered) == 1
    assert caller.answered[0]["limit"] == limit


@pytest.mark.parametrize(
    "limit", [SEARCH_LIMIT_MIN - 1, SEARCH_LIMIT_MAX + 1], ids=["limit-0", "limit-201"]
)
def test_a_page_size_outside_the_tools_bounds_is_a_typed_400_with_no_read(
    audit_env: None, monkeypatch: pytest.MonkeyPatch, limit: int
) -> None:
    """The other end of the same bound, one step outside it on each side.

    Catches a route that answers the tool's typed `VALIDATION` as an untyped
    500 or as FastAPI's own 422, and a route that silently CLAMPS the value
    into range - which would show an auditor a page they did not ask for and
    hide the mistake from them, on a surface whose whole purpose is showing
    exactly what happened.
    """
    caller = BoundCheckingToolCaller(SEARCH_PAYLOAD)

    response, _spy = _get(
        monkeypatch=monkeypatch,
        caller=caller,
        headers=_auth(_token()),
        params={"limit": str(limit)},
    )

    assert response.status_code == 400
    assert response.json() == {"code": ErrorCode.VALIDATION.value}
    assert caller.answered == []


def test_a_cursor_of_one_is_accepted(audit_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """`audit_events.seq` is an identity starting at 1, so `before_seq=1` is
    the lowest cursor that can name a row and section 1 fixes it as accepted.

    Catches a route re-declaring the floor as `Query(gt=1)`: the last page of
    the log - the oldest events, the ones an investigation reaches for - would
    answer an untyped 422 while the tool accepts the same value.
    """
    caller = BoundCheckingToolCaller(SEARCH_PAYLOAD)

    response, _spy = _get(
        monkeypatch=monkeypatch,
        caller=caller,
        headers=_auth(_token()),
        params={"before_seq": str(MIN_BEFORE_SEQ)},
    )

    assert response.status_code == 200
    assert len(caller.answered) == 1
    assert caller.answered[0]["before_seq"] == MIN_BEFORE_SEQ


def test_a_cursor_of_zero_is_a_typed_400_with_no_read(
    audit_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One step below the same floor. Catches a route that forwards a cursor
    the tool refuses as an untyped 422 or 500, and one that COERCES it to the
    lowest legal value, which would answer an empty page for a query the
    auditor could otherwise see refused.
    """
    caller = BoundCheckingToolCaller(SEARCH_PAYLOAD)

    response, _spy = _get(
        monkeypatch=monkeypatch,
        caller=caller,
        headers=_auth(_token()),
        params={"before_seq": str(MIN_BEFORE_SEQ - 1)},
    )

    assert response.status_code == 400
    assert response.json() == {"code": ErrorCode.VALIDATION.value}
    assert caller.answered == []


@pytest.mark.parametrize(
    "occurred_from",
    ["yesterday", "2026-13-01T00:00:00Z", "01/02/2026"],
    ids=["a-word", "impossible-month", "a-local-format"],
)
def test_a_non_iso_occurred_from_is_a_typed_400_and_never_fastapis_422(
    audit_env: None, monkeypatch: pytest.MonkeyPatch, occurred_from: str
) -> None:
    """Section 1's third criterion, last sentence, and section 5's first risk.

    Catches the route this plan most easily writes by accident: one declaring
    `occurred_from: datetime | None = Query(None)`, which answers FastAPI's
    UNTYPED 422 for each of these three - a body with `detail` and a `loc`
    path, not `{"code": "VALIDATION"}`, so the page has nothing to say and the
    admin surface leaks the route's own parameter names. The typed 400 can only
    come from forwarding the string and dressing the tool's `VALIDATION`.

    The bearer verifies, no other filter is sent and the value is well within
    every cap, so only the date's handling can decide these rows.
    """
    caller = BoundCheckingToolCaller(SEARCH_PAYLOAD)

    response, _spy = _get(
        monkeypatch=monkeypatch,
        caller=caller,
        headers=_auth(_token()),
        params={"occurred_from": occurred_from},
    )

    assert response.status_code == 400
    assert response.json() == {"code": ErrorCode.VALIDATION.value}
    assert caller.answered == []


# --- the refusals -------------------------------------------------------------


BAD_BEARERS: tuple[tuple[str, Mapping[str, str]], ...] = (
    ("no-header", {}),
    ("wrong-scheme", {"Authorization": "Basic ZmFrZTpmYWtl"}),
    ("empty-bearer", {"Authorization": "Bearer "}),
)


@pytest.mark.parametrize(
    "headers", [case[1] for case in BAD_BEARERS], ids=[case[0] for case in BAD_BEARERS]
)
def test_an_absent_or_malformed_bearer_is_a_typed_401_and_opens_no_session(
    audit_env: None, monkeypatch: pytest.MonkeyPatch, headers: Mapping[str, str]
) -> None:
    """Section 1's fifth criterion. Catches a route with no token check at all,
    and one that checks the token AFTER opening the MCP session: the caller is
    scripted with NOTHING, so a route that entered it or called a tool fails
    outright rather than showing up as a count afterwards.

    The token check inside `call_tool_route` is the ONLY guard on this path -
    the route derives no identity of its own, so `deps.authenticated` runs once
    (decision B, and `deps.py`'s own body) - and there is no rate limit, no
    body and no read behind it. Deleting that check is the only thing that
    flips these rows.
    """
    caller = ScriptedToolCaller()

    response, spy = _get(monkeypatch=monkeypatch, caller=caller, headers=headers)

    assert response.status_code == 401
    assert response.json() == {"code": ErrorCode.FORBIDDEN.value}
    assert caller.entered == 0
    assert caller.calls == []
    assert spy.bearer_tokens == []


def test_a_forged_bearer_is_a_typed_401_and_opens_no_session(
    audit_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a route that reads the claims without VERIFYING the signature:
    this token is well formed and carries a real tenant, a real user, a real
    `sid` and the `auditor` principal - it is signed with the wrong secret and
    nothing else, so only the verification can refuse it. A route that parsed
    the claims and trusted them would hand the whole audit log to anybody who
    can write a JSON object.
    """
    caller = ScriptedToolCaller()

    response, spy = _get(
        monkeypatch=monkeypatch,
        caller=caller,
        headers=_auth(_token(secret=OTHER_JWT_SECRET)),
    )

    assert response.status_code == 401
    assert response.json() == {"code": ErrorCode.FORBIDDEN.value}
    assert caller.entered == 0
    assert spy.bearer_tokens == []


def test_an_expired_bearer_is_a_typed_401_and_opens_no_session(
    audit_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a verification that checks the SIGNATURE and not the expiry:
    this token is signed with the right secret and differs from the happy
    path's only in being nine hours old against an eight-hour `exp`, so the
    expiry comparison is the only thing that can refuse it. A stale token left
    reading the audit log is a session that never ends.
    """
    caller = ScriptedToolCaller()

    response, spy = _get(
        monkeypatch=monkeypatch,
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
    ],
)
def test_a_tool_refusal_answers_its_own_typed_code(
    audit_env: None,
    monkeypatch: pytest.MonkeyPatch,
    error_class: type[RemoteToolError],
    code: ErrorCode,
    status: int,
) -> None:
    """Section 1's sixth criterion, one code per row. Catches a route that lets
    the tool's `ToolError` escape: FastAPI answers an untyped 500, so a matrix
    refusal the caller could act on ("you may not read this log") and an
    admin's tripped kill switch ("try again later") read as one outage.

    The bearer VERIFIES here and no filter is sent, so neither the token gate
    nor any validation can decide these rows - only the `except ToolError`
    branch and the shared `STATUS_FOR_CODE` can. The map
    itself is `deps`' and is pinned as a whole by
    `test_approval_routes.py::test_the_shared_status_map_answers_a_matrix_refusal_403_and_a_kill_switch_503`;
    what these rows add is that the search route goes THROUGH it rather than
    carrying `admin_routes`' own literal statuses (decision B).
    """
    caller = RefusingToolCaller(error_class(code.value))

    response, _spy = _get(monkeypatch=monkeypatch, caller=caller, headers=_auth(_token()))

    assert response.status_code == status
    assert response.json() == {"code": code.value}
    assert caller.entered == 1


def test_an_unreachable_mcp_server_is_one_typed_internal_naming_neither_host_nor_path(
    audit_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Section 1's sixth criterion, second half. Catches two failures at once:
    a handshake failure left to propagate (an untyped 500 with a traceback
    body), and a body that carries the client's message -
    `TRANSPORT_FAILURE_MESSAGE` is opaque on purpose, and a route rendering the
    exception's `str` would leak the MCP host and path to a browser.
    `INTERNAL` maps to 500 in the one shared map, so the
    500 here pins that an unreachable server renders the typed `INTERNAL` and
    leaks neither the host nor the path.
    """
    caller = UnreachableToolCaller()

    response, _spy = _get(monkeypatch=monkeypatch, caller=caller, headers=_auth(_token()))

    assert response.status_code == 500
    assert response.json() == {"code": ErrorCode.INTERNAL.value}
    assert MCP_HOST not in response.text
    assert MCP_PATH not in response.text
    assert TRANSPORT_FAILURE_MESSAGE not in response.text
