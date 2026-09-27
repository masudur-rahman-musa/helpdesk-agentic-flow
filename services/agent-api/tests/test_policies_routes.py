"""An earlier change section 6: the policy documents board, over HTTP.

Surface under test (written before it exists; every case below is red on one of
these names until step 4 lands):

``agent_api.policies_routes`` (step 4)
    ``router`` with ``GET /admin/policies`` - no query parameter, no body, no
    argument at all - calling the ``list_policy_documents`` tool (step 3)
    through ``agent_api.deps``' existing seam: ``authenticated``,
    ``unauthenticated``, ``typed_refusal``, ``call_tool_route`` and
    ``STATUS_FOR_CODE``. Registered in ``main.py`` beside the rules router.
    That seam is why every case here patches ``deps.build_tool_caller``,
    enters no lifespan and opens no socket.
    ``POLICIES_PATH = "/admin/policies"``,
    ``LIST_POLICY_DOCUMENTS_TOOL = "list_policy_documents"`` and
    ``ListPolicyDocumentsArgs`` (no fields).

Shapes this file fixes that the plan left open (a project rule and 55;
these belong in 24g's Deviations):
- the refusal envelope is ``{"code": "<ErrorCode value>"}``, and the bearer
  refusal is a literal ``401`` carrying ``{"code": "FORBIDDEN"}`` - what
  ``deps.unauthenticated()`` answers, 24eb's shape unchanged.
- the staged board is literal-accurate: it is validated against the tool's own
  ``ListPolicyDocumentsOutput`` in a premise assertion, so a row invented here
  rather than read off ``mcp_server/tools/admin.py`` fails this file instead of
  passing against a route that answers the wrong shape.
- the staged version order is ``ingested_at`` descending (24g decision C) over
  ``v9``, ``v10``, ``v2``, whose own string order is neither that sequence nor
  its reverse: a route that re-sorted the versions on any single field, or
  reversed them, answers a different list.
- the staged documents are slug ascending while their TITLES sort the other
  way round, so a route that re-sorted the documents by title is red too.

Not asserted here, and why (a project rule - already pinned, not duplicated):
- ``deps.STATUS_FOR_CODE`` gaining no row is pinned as an EXACT mapping by
  ``test_killswitch_routes.py::
  test_the_shared_status_map_answers_exactly_five_codes_with_no_internal_row``,
  which goes red for any sixth row whoever adds it.
- a forged and an expired bearer are pinned against the same single
  ``deps.call_tool_route`` seam by ``test_rules_routes.py::
  test_a_forged_bearer_is_a_typed_401_and_opens_no_session`` and
  ``::test_an_expired_bearer_is_a_typed_401_and_opens_no_session``. The
  absent-or-malformed row below is kept because it is the one that also pins
  that THIS router reaches the seam at all.

An earlier change extends this file with the re-ingest POST (``POST
/admin/policies/reingest``); its own expected surface is written out above its
three cases, at the end of the file. Its bearer and refusal cases carry their
own names rather than 31a section 6's, because the board's cases already hold
those names in this module and a duplicate definition would silently replace
one of them.

Cost and isolation: NO backing service at all, exactly as
``test_rules_routes.py`` and ``test_killswitch_routes.py``. The lifespan is
never entered - this route reads nothing off ``app.state`` - so nothing here
opens a socket, a pool or a Redis connection. Fixtures live in this module
rather than a conftest, for the same reason they do there.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from agent_api import deps, policies_routes
from agent_api.main import app
from agent_api.mcp_client import (
    RemoteForbiddenError,
    RemoteNotFoundError,
    RemoteToolDisabledError,
    RemoteToolError,
    RemoteValidationError,
    ToolCaller,
)
from helpdesk.auth.jwt import issue_token
from helpdesk.core.settings import Settings
from helpdesk.errors import ErrorCode
from mcp_server.tools.admin import (
    ListPolicyDocumentsOutput,
    ReingestPolicyDocumentOutput,
)
from pydantic import BaseModel

LIST_POLICY_DOCUMENTS = "list_policy_documents"

POLICIES_PATH = "/admin/policies"

# Obviously fake, tests only: none of these is a credential, and
# nothing in this file connects to any of these hosts.
TEST_JWT_SIGNING_PHRASE = "policies-routes-test-jwt-secret-0123456789"
MCP_URL = "http://mcp.invalid:8081/mcp"
DATABASE_URL = "postgresql+asyncpg://fake:fake@db.invalid:5432/fake_policies"
REDIS_URL = "redis://redis.invalid:6379/0"
QDRANT_URL = "http://qdrant.invalid:6333"

TENANT_ID = uuid.UUID("11111111-1111-5111-8111-111111111111")
ADMIN_ID = uuid.UUID("22222222-2222-5222-8222-222222222222")
DEPARTMENT_ID = uuid.UUID("33333333-3333-5333-8333-333333333333")
SESSION_ID = uuid.UUID("44444444-4444-5444-8444-444444444444")

# The two principals SPEC section 7 gives the row a cell for, and the five it
# does not. The five are asserted through the tool's own refusal: the route is
# not the authority on the matrix, so what these rows pin is that a refusal
# reaches the caller as a 403 rather than as a 500 or an empty board.
PERMITTED_PRINCIPALS = ("it_admin", "platform_admin")
DENIED_PRINCIPALS = ("employee", "manager", "system_owner", "hr", "auditor")

# The three keys of `PolicyDocumentRow` and the six of `PolicyVersionRow`,
# written out rather than derived from the payload, so a field added or renamed
# in the staged rows alone cannot satisfy these sets.
DOCUMENT_FIELDS = frozenset({"slug", "title", "versions"})
VERSION_FIELDS = frozenset(
    {"version", "is_current", "ingested_at", "chunk_count", "source_path", "sha256"}
)

# Obviously fake digests, 64 hex characters each.
DIGEST_V9 = "a9" * 32
DIGEST_V10 = "b0" * 32
DIGEST_V2 = "c2" * 32
DIGEST_LEAVE = "d1" * 32


def _version(
    version: str,
    *,
    is_current: bool,
    ingested_at: str,
    chunk_count: int,
    sha256: str,
    slug: str = "fake-aa-access-policy",
) -> dict[str, Any]:
    """One version row: `PolicyVersionRow`'s six fields, no more."""
    return {
        "version": version,
        "is_current": is_current,
        "ingested_at": ingested_at,
        "chunk_count": chunk_count,
        "source_path": f"policies/fake-tenant/{version}/{slug}.md",
        "sha256": sha256,
    }


ACCESS_DOCUMENT: dict[str, Any] = {
    "slug": "fake-aa-access-policy",
    "title": "Zzz Fake Access Standard",
    "versions": [
        _version(
            "v9",
            is_current=True,
            ingested_at="2026-04-01T09:00:00Z",
            chunk_count=3,
            sha256=DIGEST_V9,
        ),
        _version(
            "v10",
            is_current=False,
            ingested_at="2026-03-01T09:00:00Z",
            chunk_count=0,
            sha256=DIGEST_V10,
        ),
        _version(
            "v2",
            is_current=False,
            ingested_at="2026-01-01T09:00:00Z",
            chunk_count=5,
            sha256=DIGEST_V2,
        ),
    ],
}
"""A document with three versions, one of them current and one of them with no
chunk at all - the half-landed ingest `/admin/policies` exists to show."""

LEAVE_DOCUMENT: dict[str, Any] = {
    "slug": "fake-zz-leave-policy",
    "title": "Aaa Fake Leave Standard",
    "versions": [
        _version(
            "v1",
            is_current=False,
            ingested_at="2026-02-01T09:00:00Z",
            chunk_count=2,
            sha256=DIGEST_LEAVE,
            slug="fake-zz-leave-policy",
        )
    ],
}
"""A document with NO current version: every flag false, still on the board
(criterion 6). Its title sorts before the other document's, while its slug
sorts after - which is what catches a route that re-sorts by title."""

BOARD_PAYLOAD: Mapping[str, Any] = {"documents": [ACCESS_DOCUMENT, LEAVE_DOCUMENT]}
"""One board exactly as `list_policy_documents` answers it: the `documents`
envelope, slug ascending, versions `ingested_at` descending. The route must
hand this back UNCHANGED."""

EMPTY_BOARD: Mapping[str, Any] = {"documents": []}
"""A tenant with no corpus. Section 1: a 200 and an empty list, not a 404."""

STAGED_SLUGS = ["fake-aa-access-policy", "fake-zz-leave-policy"]
"""The staged document sequence, as the exact list the order case asserts."""

STAGED_VERSIONS = ["v9", "v10", "v2"]
"""The staged version sequence, whose own string order is neither this nor its
reverse: `sorted` gives `v10, v2, v9` and `sorted(reverse=True)` gives
`v9, v2, v10`."""


# --- the scripted seam --------------------------------------------------------


class ScriptedToolCaller:
    """A `ToolCaller` answering one scripted payload per call, in order.

    The same stub shape `test_rules_routes.py` and `test_killswitch_routes.py`
    use: a call past the last scripted answer fails the test
    outright, which is how "the route opened no session at all" is asserted as
    a hard stop rather than as a count read afterwards.
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


class CallerSpy:
    """Records the bearer `build_tool_caller` was handed, and hands back a stub."""

    def __init__(self, caller: ToolCaller) -> None:
        self._caller = caller
        self.bearer_tokens: list[str] = []

    def build(self, settings: Settings, bearer_token: str) -> ToolCaller:
        self.bearer_tokens.append(bearer_token)
        return self._caller


@pytest.fixture
def policies_env(monkeypatch: pytest.MonkeyPatch) -> None:
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

    The seam is `deps.build_tool_caller` (23c decision C put `call_tool_route`
    there), and the lifespan is NOT entered: this route reads nothing off
    `app.state`, so entering it would buy a psycopg pool, a Redis client and a
    gateway client that no case here uses.
    """
    spy = CallerSpy(caller)
    monkeypatch.setattr(deps, "build_tool_caller", spy.build)

    async def scenario(client: httpx.AsyncClient) -> httpx.Response:
        return await client.get(POLICIES_PATH, headers=dict(headers or {}))

    return _drive(scenario), spy


def _documents(response: httpx.Response) -> list[dict[str, Any]]:
    body = response.json()
    assert isinstance(body, dict), f"the board must be an envelope, not {type(body)!r}"
    documents = body["documents"]
    assert isinstance(documents, list)
    return list(documents)


# --- the module's own surface --------------------------------------------------


def test_the_router_names_its_path_its_tool_and_a_fieldless_argument_model(
    policies_env: None,
) -> None:
    """The constants this file drives the route with, pinned at their values rather than read out of the module under test.

    Catches three drifts no behavioural case below can see, because each of
    them would make those cases agree with whatever the implementation chose: a
    path renamed to `/admin/policy-documents` (SPEC section 14's own name is
    `/admin/policies`, and the proxy on the other side of the wire is written
    against the same literal), a tool name that is not the registry's
    `list_policy_documents` (the far side would answer `NOT_FOUND` for a tool
    nobody registered), and an arguments model that declares a field - which is
    a project rule's whole concern, and would cross back as a `VALIDATION` because
    `ListPolicyDocumentsInput` forbids extras.

    The registration half catches a router written and never included in
    `main.py`: every case below would then fail as a 404 with no hint which of
    the two is missing. It reads `app.openapi()["paths"]` rather than walking
    `app.routes`: this app includes its routers through a wrapper, so the
    concrete `APIRoute` is not on `app.routes` at all and the walk answered an
    empty set for every route in the service.
    """
    assert policies_routes.POLICIES_PATH == "/admin/policies"
    assert policies_routes.LIST_POLICY_DOCUMENTS_TOOL == "list_policy_documents"
    assert POLICIES_PATH == policies_routes.POLICIES_PATH
    assert LIST_POLICY_DOCUMENTS == policies_routes.LIST_POLICY_DOCUMENTS_TOOL
    assert policies_routes.ListPolicyDocumentsArgs().model_dump() == {}
    assert policies_routes.ListPolicyDocumentsArgs.model_fields == {}
    openapi_paths = app.openapi()["paths"]
    assert POLICIES_PATH in openapi_paths
    assert "get" in openapi_paths[POLICIES_PATH]


# --- the staged payloads are the tool's own shape -----------------------------


def test_the_staged_board_is_exactly_what_the_tool_model_produces() -> None:
    """The premise every case below rests on, and the one thing this file can
    get wrong on its own: catches a staged payload invented here instead of
    read off `mcp_server.tools.admin`, which would let the route's body case
    pass while the route answered a shape the web parser refuses.

    `ListPolicyDocumentsOutput` is the tool's own model, so validating the
    literal and dumping it back in JSON mode is the strongest available
    statement that the fixture is the wire's shape: an extra key is refused, a
    missing one is refused, and an instant rendered in the wrong form does not
    round-trip.

    The last three assertions are the order case's premise: the staged version
    sequence is neither the ascending nor the descending string order of those
    same three versions, so a route that re-sorted on the `version` string
    cannot accidentally agree with it.
    """
    assert ListPolicyDocumentsOutput.model_validate(BOARD_PAYLOAD).model_dump(mode="json") == dict(
        BOARD_PAYLOAD
    )
    assert ListPolicyDocumentsOutput.model_validate(EMPTY_BOARD).model_dump(mode="json") == dict(
        EMPTY_BOARD
    )
    assert [document["slug"] for document in BOARD_PAYLOAD["documents"]] == STAGED_SLUGS
    assert [version["version"] for version in ACCESS_DOCUMENT["versions"]] == STAGED_VERSIONS
    assert sorted(STAGED_VERSIONS) != STAGED_VERSIONS
    assert sorted(STAGED_VERSIONS, reverse=True) != STAGED_VERSIONS


# --- the board (GET) ----------------------------------------------------------


@pytest.mark.parametrize("principal", PERMITTED_PRINCIPALS)
def test_the_board_route_answers_the_tools_output_unchanged(
    policies_env: None, monkeypatch: pytest.MonkeyPatch, principal: str
) -> None:
    """Section 1's "reshapes nothing", and the bearer half of the same
    criterion.

    Catches a route that reshapes, filters or renames `list_policy_documents`'
    structured output on the way out (the envelope is `documents`, each
    document carries its exact three fields and each version its exact six),
    and one that calls a different tool. The reshape that matters most here is
    dropping the version whose `chunk_count` is `0` or the document with no
    current version: those are the two rows this page exists to surface, and a
    route that filtered either would report a broken corpus as clean.

    The bearer half is a project rule: the token the caller presented is forwarded
    VERBATIM, so RBAC, the kill switch and the tool's audit event apply to this
    read. A route that minted its own service token, or reused a configured
    one, fails on that assertion alone.

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
    assert caller.names == [LIST_POLICY_DOCUMENTS]
    assert spy.bearer_tokens == [token]

    documents = _documents(response)
    assert len(documents) == 2
    for document in documents:
        assert set(document) == DOCUMENT_FIELDS
        for version in document["versions"]:
            assert set(version) == VERSION_FIELDS
    assert [version["chunk_count"] for version in documents[0]["versions"]] == [3, 0, 5]
    assert [version["is_current"] for version in documents[1]["versions"]] == [False]


def test_the_board_route_sends_an_exactly_empty_argument_set(
    policies_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Section 1's "hands `call_tool_route` an exactly empty argument set",
    a project rule, as an EXACT key set.

    Catches a route that passes the caller's tenant, user id or a slug along as
    an argument. `ListPolicyDocumentsInput` declares no field and forbids
    extras, so a key riding along would cross back as a `VALIDATION` and the
    board would answer an error on every load - and a `tenant_id` in particular
    is the browser asking to read another tenant's corpus, which the token is
    the only authority on (a project rule: nothing here leans on a
    cross-tenant guard, because the argument must not exist at all).
    """
    caller = ScriptedToolCaller(BOARD_PAYLOAD)

    response, _spy = _get(monkeypatch=monkeypatch, caller=caller, headers=_auth(_token()))

    assert response.status_code == 200
    _name, arguments = caller.calls[0]
    assert arguments == {}


def test_the_rows_cross_in_the_tools_own_order(
    policies_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Section 1's "crosses the rows in the tool's own order" and decision C.

    Catches a route that sorts again on the way out. The documents are staged
    slug ascending while their TITLES sort the other way round, so a title sort
    answers the reverse; and the three staged versions are in `ingested_at`
    descending order whose own string order is neither that sequence nor its
    reverse, so a sort on the `version` string - the mistake decision C exists
    to stop, because `v10` sorts before `v2` - answers a third sequence.

    The order is the whole value of the helper's total order: every caller sees
    ONE sequence, and a second sort here would give 24h a second contract to
    track.
    """
    caller = ScriptedToolCaller(BOARD_PAYLOAD)

    response, _spy = _get(monkeypatch=monkeypatch, caller=caller, headers=_auth(_token()))

    assert response.status_code == 200
    documents = _documents(response)
    assert [document["slug"] for document in documents] == STAGED_SLUGS
    assert [document["title"] for document in documents] == [
        "Zzz Fake Access Standard",
        "Aaa Fake Leave Standard",
    ]
    assert [version["version"] for version in documents[0]["versions"]] == STAGED_VERSIONS
    assert [version["ingested_at"] for version in documents[0]["versions"]] == [
        "2026-04-01T09:00:00Z",
        "2026-03-01T09:00:00Z",
        "2026-01-01T09:00:00Z",
    ]


def test_a_tenant_with_no_corpus_is_a_200_and_an_empty_list(
    policies_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Section 1's unhappy path, first half. Catches a route that treats an
    empty corpus as a missing resource - a 404, or a typed `NOT_FOUND` - which
    would leave a tenant that has simply never been ingested looking at an
    error page, and catches a route that answers `{}` or a bare `[]` instead of
    the envelope the web parser reads.

    Only the empty payload can decide this row: the bearer verifies, the tool
    answers successfully, and no refusal branch is entered.
    """
    caller = ScriptedToolCaller(EMPTY_BOARD)

    response, _spy = _get(monkeypatch=monkeypatch, caller=caller, headers=_auth(_token()))

    assert response.status_code == 200
    assert response.json() == {"documents": []}


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
    policies_env: None, monkeypatch: pytest.MonkeyPatch, headers: Mapping[str, str]
) -> None:
    """Catches a route with no token check at all, and one that checks the
    token AFTER opening the MCP session: the caller is scripted with NOTHING,
    so a route that entered it or called a tool fails outright rather than
    showing up as a count read afterwards.

    KILL: `deps.call_tool_route` calls `authenticated`
    exactly once and this route derives no identity of its own, so deleting
    that single call is the only thing that flips this row - there is no second
    copy of the check to hide behind. Equally, a router that reached past the
    seam and called `build_tool_caller` itself would answer the board here.
    """
    caller = ScriptedToolCaller()

    response, spy = _get(monkeypatch=monkeypatch, caller=caller, headers=headers)

    assert response.status_code == 401
    assert response.json() == {"code": ErrorCode.FORBIDDEN.value}
    assert caller.entered == 0
    assert caller.calls == []
    assert spy.bearer_tokens == []


# --- the tool's own refusals ---------------------------------------------------


@pytest.mark.parametrize("principal", DENIED_PRINCIPALS)
def test_a_principal_with_no_cell_reads_a_typed_403_and_no_documents(
    policies_env: None, monkeypatch: pytest.MonkeyPatch, principal: str
) -> None:
    """Section 1's unhappy path, second half, one row per principal with no
    cell on the `list_policy_documents` row.

    Catches a route that lets the tool's `ToolError` escape - FastAPI answers
    an untyped 500, so "you may not read this tenant's corpus" reads as an
    outage - and a route that swallows a refusal into an empty board, which
    would tell an admin the tenant has never been ingested at all. The body
    carries the code and nothing of the corpus, which is what "the tool is not
    an existence oracle" means on this side of the wire.

    No kill claim for the MATRIX: the matrix lives behind
    the MCP boundary and is pinned per principal by
    `services/mcp-server/tests/test_mcp_list_policy_documents.py::
    test_the_five_denied_principals_are_refused_whatever_the_corpus_holds`.
    These rows stage the refusal and pin what the caller RECEIVES, which only
    the `except ToolError` branch and the shared `STATUS_FOR_CODE` can decide -
    the bearer verifies here, so the token gate cannot.
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
    policies_env: None,
    monkeypatch: pytest.MonkeyPatch,
    error_class: type[RemoteToolError],
    code: ErrorCode,
    status: int,
) -> None:
    """Section 1's "answers `FORBIDDEN` 403 and `TOOL_DISABLED` 503 from the
    existing five", read through the SHARED `deps.STATUS_FOR_CODE` (which this
    plan does not touch).

    Catches a route that maps every refusal onto one status: a matrix denial
    the caller cannot act on and a kill switch tripped on
    `list_policy_documents` ("try again later") are different answers, and a
    board that reported a tripped switch as a permission problem would send an
    admin looking for the wrong fix. Catches a router that built its own status
    map too, which is what 23c decision C moved the map to stop.

    The bearer verifies and the route takes no input at all, so no validation
    and no token gate can decide these rows - only the `except ToolError`
    branch can.
    """
    caller = RefusingToolCaller(error_class(code.value))

    response, _spy = _get(monkeypatch=monkeypatch, caller=caller, headers=_auth(_token()))

    assert response.status_code == status
    assert response.json() == {"code": code.value}
    assert caller.entered == 1


# --- an earlier change: the re-ingest POST ---------------------------------------------
#
# Surface under test (written before it exists; the three cases below are red
# on one of these names until step 6 lands):
#
# ``agent_api.policies_routes``
#     ``REINGEST_PATH = "/admin/policies/reingest"`` - SPEC section 14's page
#     with the action appended, identical upstream and browser-side (24eb
#     decision C, 31a decision I).
#     ``REINGEST_POLICY_DOCUMENT_TOOL = "reingest_policy_document"``.
#     ``ReingestPolicyDocumentArgs(BaseModel)`` - exactly ``slug``, ``version``
#     and ``idempotency_key``, the three keys the tool's own input declares.
#     ``POST REINGEST_PATH`` over the same ``deps`` seam as the GET above:
#     ``call_tool_route`` verifies the bearer, opens ONE session under the
#     caller's own token and renders the tool's structured output unchanged.
#
# Nothing new in ``deps.STATUS_FOR_CODE`` (31a criterion 15): four of its
# existing five codes cross this route, and the map itself is pinned as an
# exact mapping by ``test_killswitch_routes.py::
# test_the_shared_status_map_answers_exactly_five_codes_with_no_internal_row``
# (a project rule - already held, not duplicated here).

REINGEST_PATH = "/admin/policies/reingest"
REINGEST_POLICY_DOCUMENT = "reingest_policy_document"

REINGEST_SLUG = "fake-aa-access-policy"
REINGEST_VERSION = "v9"
REINGEST_KEY = "fake-reingest-key-0123456789abcdef"

REINGEST_BODY: Mapping[str, Any] = {
    "slug": REINGEST_SLUG,
    "version": REINGEST_VERSION,
    "idempotency_key": REINGEST_KEY,
}
"""The browser's request body: the three keys and nothing else."""

REINGEST_PAYLOAD: Mapping[str, Any] = {
    "slug": REINGEST_SLUG,
    "version": REINGEST_VERSION,
    "created": True,
    "chunk_count": 4,
    "is_current": True,
    "sha256": "e5" * 32,
    "ingested_at": "2026-05-04T09:30:00Z",
    "summary": "Re-ingested fake-aa-access-policy v9: 4 chunks, now current.",
}
"""One re-ingest exactly as `reingest_policy_document` answers it (31a
criterion 10): seven values and the human-readable summary FIELD."""


def _post(
    *,
    monkeypatch: pytest.MonkeyPatch,
    caller: ToolCaller,
    body: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
) -> tuple[httpx.Response, CallerSpy]:
    """One re-ingest against the app, with the tool caller replaced at the seam.

    The same seam, the same reason and the same absent lifespan as `_get`: this
    route reads nothing off `app.state`, so nothing here opens a socket.
    """
    spy = CallerSpy(caller)
    monkeypatch.setattr(deps, "build_tool_caller", spy.build)

    async def scenario(client: httpx.AsyncClient) -> httpx.Response:
        return await client.post(
            REINGEST_PATH,
            json=dict(REINGEST_BODY if body is None else body),
            headers=dict(headers or {}),
        )

    return _drive(scenario), spy


def test_the_reingest_route_sends_its_exact_three_key_argument_set(
    policies_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Section 1's "hands `call_tool_route` exactly `{slug, version,
    idempotency_key}` and the caller's own `Authorization` header, reshapes
    nothing on the way out", as an EXACT dict.

    Catches a route that passes the caller's tenant or user id along as a
    fourth argument: `ReingestPolicyDocumentInput` forbids extras, so the call
    would cross back as a `VALIDATION` and the control would never work - and a
    `tenant_id` in particular is the browser asking to rewrite another tenant's
    corpus, which the token is the only authority on (a project rule: nothing
    here leans on a cross-tenant guard, because the argument must not exist at
    all). It equally catches a route that DROPS the idempotency key or mints
    one of its own, which would turn every retry of a re-ingest into a second
    ingest rather than a replay.

    The bearer half is a project rule: the token the caller presented is forwarded
    VERBATIM, so RBAC, the kill switch and the tool's `policy.reingested` event
    name this admin. A route that minted a service token fails on that
    assertion alone.

    The staged answer is validated against the tool's own output model first: a payload invented here rather than read off
    `mcp_server/tools/admin.py` would let the body assertion pass while the
    route answered a shape the web parser refuses.

    The registration half reads `app.openapi()["paths"]` rather than walking
    `app.routes`: this app includes its routers through a wrapper, so the
    concrete `APIRoute` is not on `app.routes` at all and the walk answered an
    empty set for every route in the service.
    """
    assert ReingestPolicyDocumentOutput.model_validate(REINGEST_PAYLOAD).model_dump(
        mode="json"
    ) == dict(REINGEST_PAYLOAD)
    caller = ScriptedToolCaller(REINGEST_PAYLOAD)
    token = _token()

    response, spy = _post(monkeypatch=monkeypatch, caller=caller, headers=_auth(token))

    assert response.status_code == 200
    assert response.json() == dict(REINGEST_PAYLOAD)
    assert caller.calls == [(REINGEST_POLICY_DOCUMENT, dict(REINGEST_BODY))]
    assert spy.bearer_tokens == [token]
    assert policies_routes.REINGEST_PATH == REINGEST_PATH
    assert policies_routes.REINGEST_POLICY_DOCUMENT_TOOL == REINGEST_POLICY_DOCUMENT
    assert set(policies_routes.ReingestPolicyDocumentArgs.model_fields) == set(REINGEST_BODY)
    openapi_paths = app.openapi()["paths"]
    assert REINGEST_PATH in openapi_paths
    assert "post" in openapi_paths[REINGEST_PATH]


@pytest.mark.parametrize(
    ("error_class", "code", "status"),
    [
        pytest.param(RemoteValidationError, ErrorCode.VALIDATION, 400, id="validation-400"),
        pytest.param(RemoteForbiddenError, ErrorCode.FORBIDDEN, 403, id="forbidden-403"),
        pytest.param(RemoteNotFoundError, ErrorCode.NOT_FOUND, 404, id="not-found-404"),
        pytest.param(RemoteToolDisabledError, ErrorCode.TOOL_DISABLED, 503, id="disabled-503"),
    ],
)
def test_a_reingest_tool_refusal_answers_its_own_typed_code(
    policies_env: None,
    monkeypatch: pytest.MonkeyPatch,
    error_class: type[RemoteToolError],
    code: ErrorCode,
    status: int,
) -> None:
    """Section 1's criterion 15: four of `deps.STATUS_FOR_CODE`'s existing five
    cross THIS route, each as its own typed code.

    Catches a route that maps every refusal onto one status. All four are
    reachable here and all four mean different things to the admin holding the
    button: a traversing slug or a malformed version is a `VALIDATION` they can
    fix by typing something else (400), a corpus file that is not on the host
    is a `NOT_FOUND` that needs the file deployed (404), a matrix denial is a
    permission they do not hold (403), and a tripped kill switch is "not right
    now" (503). Collapsing them - or letting the `ToolError` escape into
    FastAPI's untyped 500 - sends every one of those admins to look for an
    outage.

    Catches a router that built its own status map too, which is what 23c
    decision C moved the map to stop. The bearer verifies and the body is
    valid, so neither the token gate nor FastAPI's own validation can decide
    these rows: only the `except ToolError` branch can.
    """
    caller = RefusingToolCaller(error_class(code.value))

    response, _spy = _post(monkeypatch=monkeypatch, caller=caller, headers=_auth(_token()))

    assert response.status_code == status
    assert response.json() == {"code": code.value}
    assert caller.entered == 1


@pytest.mark.parametrize(
    "headers", [case[1] for case in BAD_BEARERS], ids=[case[0] for case in BAD_BEARERS]
)
def test_an_absent_or_malformed_bearer_on_the_reingest_route_is_a_typed_401(
    policies_env: None, monkeypatch: pytest.MonkeyPatch, headers: Mapping[str, str]
) -> None:
    """The MUTATING twin of the board's 401 case, and the sharper one: this
    route rewrites a tenant's corpus.

    Catches a route with no token check at all, and one that checks the token
    AFTER opening the MCP session: the caller is scripted with NOTHING, so a
    route that entered it or called a tool fails outright rather than showing
    up as a count read afterwards. An unauthenticated caller who reached the
    tool would be re-ingesting under whatever identity the far side derived.

    KILL: `deps.call_tool_route` calls `authenticated`
    exactly once and this route derives no identity of its own, so deleting
    that single call is the only thing that flips these rows. The body is
    perfectly valid, so FastAPI's own 422 cannot answer them either - a 422
    here would mean the route declared the body in a way that is checked before
    the bearer is.
    """
    caller = ScriptedToolCaller()

    response, spy = _post(
        monkeypatch=monkeypatch, caller=caller, body=REINGEST_BODY, headers=headers
    )

    assert response.status_code == 401
    assert response.json() == {"code": ErrorCode.FORBIDDEN.value}
    assert caller.entered == 0
    assert caller.calls == []
    assert spy.bearer_tokens == []
