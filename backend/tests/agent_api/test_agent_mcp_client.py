"""The per-turn MCP session, its token and its typed error mapping.

Surface under test (written before it exists):

``agent_api.mcp_client``
    ``MCP_CONNECT_TIMEOUT_SECONDS = 5.0`` · ``MCP_READ_TIMEOUT_SECONDS = 30.0``
    ``TRANSPORT_FAILURE_MESSAGE`` — the one opaque message every unreachable,
        non-2xx or unparseable answer ends on.
    ``class RemoteToolError(ToolError)`` and exactly eight subclasses, one per
        ``helpdesk.errors.ErrorCode`` member.
    ``REMOTE_ERRORS: Mapping[ErrorCode, type[RemoteToolError]]``
    ``class ToolCaller(Protocol)`` — ``__aenter__``, ``__aexit__``,
        ``async def call_tool(name, arguments: BaseModel) -> Mapping[str, Any]``
    ``class McpToolCaller`` — ``__init__(*, url, bearer_token, transport=None)``,
        public ``http_client: httpx2.AsyncClient`` built in ``__init__``.

Driven over a REAL MCP session against the in-process `mcp_server` app
(the official SDK's streamable-HTTP client), exactly the shape
`mcp_fixtures.py` uses — an httpx2 ASGI transport, so no socket is opened. The
cheap read tool `get_me` is the vehicle: it reaches Postgres and nothing else,
so this file's cost is that fixture module's session-scoped database and Redis
index and never Qdrant or a model.

The transport-failure rows use hand-written transports rather than a real
unreachable host: a dial to a black-holed address is a timeout the length of
this file's own connect timeout, and a DNS failure is a different exception
from a refused connection on some machines.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, cast

import httpx2
import mcp.types as mcp_types
import pytest
from pydantic import BaseModel

from helpdesk.agent_api.services.mcp_client import (
    MCP_CONNECT_TIMEOUT_SECONDS,
    MCP_READ_TIMEOUT_SECONDS,
    REMOTE_ERRORS,
    TRANSPORT_FAILURE_MESSAGE,
    McpToolCaller,
    RemoteForbiddenError,
    RemoteIdempotencyConflictError,
    RemoteInternalError,
    RemoteNotFoundError,
    RemotePolicyBlockedError,
    RemoteRequestNotPendingError,
    RemoteSodConflictError,
    RemoteToolDisabledError,
    RemoteToolError,
    RemoteValidationError,
    error_for,
)
from helpdesk.shared.core.errors import ErrorCode, ToolError
from tests.support.mcp import (
    SeededMcp,
    token_for,
    token_with_claims,
)

# The MCP transport's DNS-rebinding protection accepts the patterns in
# `MCP_ALLOWED_HOSTS`, whose default carries `127.0.0.1:*`, so the
# in-process app is addressed by that literal.
LIVE_URL = "http://127.0.0.1:8000/mcp"

# A host and a path that appear nowhere else, so "the message names neither"
# is a substring search that cannot pass by accident.
OPAQUE_HOST = "policy-gateway-9c1f.invalid"
OPAQUE_PATH = "/deep/private/mcp-endpoint"
OPAQUE_URL = f"http://{OPAQUE_HOST}:9443{OPAQUE_PATH}"

# Obviously fake, tests only: this is not a credential.
FAKE_TOKEN = "fake-bearer-token-for-header-assertions"

PLANNED_REMOTE_ERRORS: dict[str, str] = {
    "FORBIDDEN": "RemoteForbiddenError",
    "NOT_FOUND": "RemoteNotFoundError",
    "SOD_CONFLICT": "RemoteSodConflictError",
    "POLICY_BLOCKED": "RemotePolicyBlockedError",
    "IDEMPOTENCY_CONFLICT": "RemoteIdempotencyConflictError",
    "TOOL_DISABLED": "RemoteToolDisabledError",
    "VALIDATION": "RemoteValidationError",
    "INTERNAL": "RemoteInternalError",
    "REQUEST_NOT_PENDING": "RemoteRequestNotPendingError",
}
"""The eight codes and the class each maps to, as a literal.

Eight, counted from `helpdesk.errors.ErrorCode`. Written out
rather than generated from the enum, so a code deleted from BOTH sides at once
is still a red row."""


class GetMeArgs(BaseModel):
    """`get_me` takes no arguments: everything it needs is on the token.

    Local to this file rather than in the catalogue, because the catalogue
    holds the tools the AGENT may call and this file registers only
    `search_policy`. `get_me` is here as the cheapest thing that proves the
    transport."""


class _RefusingTransport(httpx2.AsyncBaseTransport):
    """Every request fails to connect, deterministically and instantly."""

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        raise httpx2.ConnectError("connection refused (test transport)", request=request)


def _server_error_transport() -> httpx2.MockTransport:
    """A server that answers 500 to everything, before any MCP handshake."""
    return httpx2.MockTransport(lambda request: httpx2.Response(500, text="upstream is unwell"))


def _drive[T](
    caller_factory: Callable[[Any], McpToolCaller],
    scenario: Callable[[McpToolCaller], Awaitable[T]],
) -> T:
    """Run one scenario against a fresh in-process `mcp_server` app.

    `create_app()` per call and `close_shared_connections()` in a `finally`,
    both for `mcp_fixtures`' own reasons: the SDK's session manager refuses a
    second `run()` on one instance, and this loop's pooled Postgres and Redis
    connections would otherwise die unclosed.
    """

    async def run() -> T:
        from helpdesk.mcp_server.main import create_app
        from helpdesk.mcp_server.tools import dispatch as pipeline

        app = create_app()
        try:
            async with app.router.lifespan_context(app):
                return await scenario(caller_factory(httpx2.ASGITransport(app=app)))
        finally:
            await pipeline.close_shared_connections()

    return asyncio.run(run())


# --- the constants -------------------------------


def test_the_client_timeouts_are_exactly_the_expected_values() -> None:
    """Catches a client built on httpx2's own five-second default for both
    halves: a `search_policy` call embeds a query and searches a vector store,
    so a five-second READ deadline would fail turns the server was about to
    answer, and it would look like an outage rather than a tuning mistake."""
    assert MCP_CONNECT_TIMEOUT_SECONDS == 5.0
    assert MCP_READ_TIMEOUT_SECONDS == 30.0


def test_the_constructed_client_carries_both_halves_of_the_timeout() -> None:
    """The constants existing is not the obligation, the
    CONSTRUCTED client carrying them is. Catches an implementation that
    declares both numbers and then passes `timeout=MCP_READ_TIMEOUT_SECONDS`
    as a scalar — the connect half would silently become 30 seconds, so an
    unreachable server would hold a turn open six times longer than planned.
    Both halves are read off the client, so dropping either one flips this."""
    caller = McpToolCaller(url=OPAQUE_URL, bearer_token=FAKE_TOKEN)

    assert caller.http_client.timeout.connect == MCP_CONNECT_TIMEOUT_SECONDS
    assert caller.http_client.timeout.read == MCP_READ_TIMEOUT_SECONDS


def test_the_bearer_token_is_forwarded_verbatim_on_the_constructed_client() -> None:
    """Catches a client that re-signs, trims or re-cases the token it was
    handed. The agent forwards the caller's own token so the SERVER decides
    what that person may do; a token rebuilt here would either
    fail to verify or, worse, carry claims the agent chose."""
    caller = McpToolCaller(url=OPAQUE_URL, bearer_token=FAKE_TOKEN)

    assert caller.http_client.headers["Authorization"] == f"Bearer {FAKE_TOKEN}"


def test_the_transport_failure_message_names_neither_host_nor_path() -> None:
    """On the constant itself: catches a message built
    as an f-string over the configured URL, which would put the internal
    hostname and the endpoint path in front of an end user."""
    assert TRANSPORT_FAILURE_MESSAGE == "the policy service could not be reached"
    assert OPAQUE_HOST not in TRANSPORT_FAILURE_MESSAGE
    assert OPAQUE_PATH not in TRANSPORT_FAILURE_MESSAGE


# --- the typed error map -------------------------------------------------------


def test_remote_errors_covers_every_error_code_exactly_once() -> None:
    """Catches a map keyed by the codes the author happened to think of. A
    missing key sends a real `SOD_CONFLICT` — a segregation-of-duties refusal a
    user can act on — down the same `INTERNAL` path as an unreachable socket,
    so the reason a request was blocked is lost at the client boundary."""
    assert set(REMOTE_ERRORS) == set(ErrorCode)
    assert len(REMOTE_ERRORS) == 9
    assert len(set(REMOTE_ERRORS.values())) == 9


@pytest.mark.parametrize(
    ("code_value", "class_name"),
    sorted((key, value) for key, value in PLANNED_REMOTE_ERRORS.items()),
)
def test_each_code_maps_to_the_subclass_that_declares_it(code_value: str, class_name: str) -> None:
    """Catches a map wired to the wrong class — `FORBIDDEN` pointing at
    `RemoteValidationError` reads as "you typed something wrong" where the
    truth is "you may not do this" — and a subclass whose own `.code` does not
    match the key it is filed under, which would make the error's code and its
    type disagree once it is caught and re-raised upstream."""
    code = ErrorCode(code_value)
    mapped = REMOTE_ERRORS[code]

    assert mapped.__name__ == class_name
    assert mapped.code is code
    assert issubclass(mapped, RemoteToolError)
    assert issubclass(mapped, ToolError)


def test_the_nine_named_subclasses_are_exactly_the_mapped_ones() -> None:
    """Catches a tenth remote error class added beside the nine — a second
    answer for a code the error list closes — and a subclass declared but never
    reachable through the map, which is a code the client can never raise."""
    named = {
        RemoteForbiddenError,
        RemoteNotFoundError,
        RemoteSodConflictError,
        RemotePolicyBlockedError,
        RemoteRequestNotPendingError,
        RemoteIdempotencyConflictError,
        RemoteToolDisabledError,
        RemoteValidationError,
        RemoteInternalError,
    }

    assert set(REMOTE_ERRORS.values()) == named
    assert set(RemoteToolError.__subclasses__()) == named


# --- a real session over the real transport ------------------------------------


def test_the_forwarded_token_reaches_the_server_and_the_call_succeeds(
    mcp_env: SeededMcp,
) -> None:
    """End to end. Catches a client that opens the session without
    the `Authorization` header — the server would answer `FORBIDDEN` for a call
    the caller is entitled to make — and one that returns the raw
    `CallToolResult` instead of the structured content the protocol promises.
    The identity asserted is the TOKEN's user, so an implementation sending a
    user id as a tool argument could not change it."""
    user = mcp_env.tenant_a

    async def scenario(caller: McpToolCaller) -> Mapping[str, Any]:
        async with caller:
            return await caller.call_tool("get_me", GetMeArgs())

    payload = _drive(
        lambda transport: McpToolCaller(
            url=LIVE_URL, bearer_token=token_for(user), transport=transport
        ),
        scenario,
    )

    assert payload["user_id"] == str(user.user_id)
    assert payload["tenant_id"] == str(user.tenant_id)


def test_the_session_is_closed_after_a_successful_call(
    mcp_env: SeededMcp,
) -> None:
    """One session PER TURN means it ends with the
    turn. Catches an `__aexit__` that closes the MCP session and leaves the
    httpx client open, which leaks a connection pool per turn and shows up as
    exhaustion under load rather than as a failure here."""
    user = mcp_env.tenant_a

    async def scenario(caller: McpToolCaller) -> bool:
        async with caller:
            await caller.call_tool("get_me", GetMeArgs())
        return caller.http_client.is_closed

    assert _drive(
        lambda transport: McpToolCaller(
            url=LIVE_URL, bearer_token=token_for(user), transport=transport
        ),
        scenario,
    )


def test_the_session_is_closed_when_the_call_raises(
    mcp_env: SeededMcp,
) -> None:
    """Catches an `__aexit__` that returns early on an
    exception, or a close written after the call rather than in the context
    manager: the failing turn would leak the pool, and a failing turn is
    exactly when a retrying client opens more of them."""
    forged = token_with_claims(mcp_env.tenant_b, {"sub": str(mcp_env.tenant_a.user_id)})

    async def scenario(caller: McpToolCaller) -> bool:
        with pytest.raises(RemoteToolError):
            async with caller:
                await caller.call_tool("get_me", GetMeArgs())
        return caller.http_client.is_closed

    assert _drive(
        lambda transport: McpToolCaller(url=LIVE_URL, bearer_token=forged, transport=transport),
        scenario,
    )


def test_an_errored_result_maps_back_to_the_subclass_its_code_names(
    mcp_env: SeededMcp,
) -> None:
    """The happy half. Catches a client that reads only the content
    text and ignores `is_error`, which would return the string `"NOT_FOUND"` to
    a node as if it were an answer, and one that collapses every errored result
    into `INTERNAL`, which would tell the turn a real refusal was an outage.

    No kill claim for the SERVER's tenant guard: this case exercises the
    CLIENT's mapping, and the guard itself is pinned by
    `test_mcp_read_tools.py::test_a_tenant_b_token_cannot_read_a_tenant_a_user`.
    The forged token names a tenant A user under a tenant B token, which is the
    cheapest way to make the real server answer one of the eight codes."""
    forged = token_with_claims(mcp_env.tenant_b, {"sub": str(mcp_env.tenant_a.user_id)})

    async def scenario(caller: McpToolCaller) -> ToolError:
        async with caller:
            with pytest.raises(RemoteToolError) as excinfo:
                await caller.call_tool("get_me", GetMeArgs())
            return excinfo.value

    raised = _drive(
        lambda transport: McpToolCaller(url=LIVE_URL, bearer_token=forged, transport=transport),
        scenario,
    )

    assert isinstance(raised, RemoteNotFoundError)
    assert raised.code is ErrorCode.NOT_FOUND
    assert str(raised) != TRANSPORT_FAILURE_MESSAGE


# --- the unhappy half: one opaque INTERNAL ---------------------------


def test_an_unreachable_server_is_one_typed_internal_naming_neither_host_nor_path() -> None:
    """On the raised error rather than the constant.

    Catches a client that lets `httpx2.ConnectError` escape — a node would then
    see an untyped exception where every other failure is a `ToolError`, and
    `retrieve_policy`'s `except ToolError` would not catch it, so the turn would
    raise out of `run_turn` instead of ending as state. Also catches an
    implementation that types the failure correctly but builds its message from
    the URL, putting an internal hostname in front of an end user.

    The transport is what refuses, so nothing about the SERVER can decide this
    row: it is the client's own translation or nothing."""
    transport = _RefusingTransport()
    caller = McpToolCaller(url=OPAQUE_URL, bearer_token=FAKE_TOKEN, transport=transport)

    async def scenario() -> ToolError:
        with pytest.raises(RemoteToolError) as excinfo:
            async with caller:
                await caller.call_tool("get_me", GetMeArgs())
        return excinfo.value

    raised = asyncio.run(scenario())

    assert isinstance(raised, RemoteInternalError)
    assert raised.code is ErrorCode.INTERNAL
    assert str(raised) == TRANSPORT_FAILURE_MESSAGE
    assert OPAQUE_HOST not in str(raised)
    assert OPAQUE_PATH not in str(raised)
    assert caller.http_client.is_closed


def test_a_non_2xx_answer_is_the_same_typed_internal() -> None:
    """The second shape. Catches a client that only guards the
    connect step: a reachable server answering 500 (a bad deploy, a proxy in
    the way) raises inside the SDK's handshake instead of at the socket, so an
    implementation wrapping `connect` alone passes the row above and fails
    here. The answer must be the SAME opaque `INTERNAL`, because a status code
    is as much an internal detail as a hostname."""
    caller = McpToolCaller(
        url=OPAQUE_URL, bearer_token=FAKE_TOKEN, transport=_server_error_transport()
    )

    async def scenario() -> ToolError:
        with pytest.raises(RemoteToolError) as excinfo:
            async with caller:
                await caller.call_tool("get_me", GetMeArgs())
        return excinfo.value

    raised = asyncio.run(scenario())

    assert isinstance(raised, RemoteInternalError)
    assert str(raised) == TRANSPORT_FAILURE_MESSAGE
    assert "500" not in str(raised)
    assert OPAQUE_HOST not in str(raised)


def test_a_tool_name_the_server_does_not_serve_is_a_typed_error(
    mcp_env: SeededMcp,
) -> None:
    """Catches the last escape hatch in `call_tool`: a name the registry cannot
    resolve. Whatever the SDK raises for it, the client owes the turn a
    `ToolError` — an untyped exception here would bypass `retrieve_policy`'s
    `except ToolError` exactly as an unhandled transport failure would, and a
    misspelling in the catalogue is a far likelier cause than a dead socket."""
    user = mcp_env.tenant_a
    unknown = f"no_such_tool_{uuid.uuid4().hex[:8]}"

    async def scenario(caller: McpToolCaller) -> ToolError:
        async with caller:
            with pytest.raises(RemoteToolError) as excinfo:
                await caller.call_tool(unknown, GetMeArgs())
            return excinfo.value

    raised = _drive(
        lambda transport: McpToolCaller(
            url=LIVE_URL, bearer_token=token_for(user), transport=transport
        ),
        scenario,
    )

    assert raised.code in set(ErrorCode)


# --- the two branches no real server can produce (the `error_for` seam) --------


@pytest.mark.parametrize("text", ["", "   ", "not_a_code", "NOT_FOUND extra", "internal"])
def test_an_unparseable_error_text_is_the_opaque_internal(text: str) -> None:
    """The third shape, on the pure seam.

    Our own server writes exactly one of the eight codes and nothing else
    (`pipeline.error_result`), so this branch is unreachable through a real
    session and `error_for` exists to make it reachable at all. Catches an
    implementation that indexes `REMOTE_ERRORS` on a raw string — a
    `KeyError` would escape untyped past `retrieve_policy`'s `except
    ToolError` — and one that lower-cases or partially matches, which would
    read `"internal"` as a code the enum does not carry.

    No kill claim for the transport: nothing here opens a session."""
    raised = error_for(text)

    assert isinstance(raised, RemoteInternalError)
    assert str(raised) == TRANSPORT_FAILURE_MESSAGE


@pytest.mark.parametrize("code", sorted(ErrorCode))
def test_every_code_text_maps_through_the_seam_to_its_own_class(code: ErrorCode) -> None:
    """The other half of the seam, over the whole enum rather than the one code
    a real session can be made to produce. Catches a `strip()` dropped from the
    parse (a code arriving with trailing whitespace would fall to `INTERNAL`)
    and a class filed under the wrong key."""
    raised = error_for(f"  {code.value}  ")

    assert type(raised) is REMOTE_ERRORS[code]
    assert raised.code is code
    assert str(raised) == code.value


@pytest.mark.parametrize("code", sorted(ErrorCode))
def test_a_code_with_a_reason_keeps_both(code: ErrorCode) -> None:
    """Catches a parser that sees `CODE: reason` as unparseable text and
    answers the opaque internal error, or one that keeps the code but drops the
    sentence, so the model never learns why it was refused.

    For every code the class is the code's own and `.reason` is the sentence.
    """
    raised = error_for(f"{code.value}: A review of this scope and period is already open.")

    assert type(raised) is REMOTE_ERRORS[code]
    assert raised.code is code
    assert raised.reason == "A review of this scope and period is already open."


@pytest.mark.parametrize("text", ["NOT_FOUND", "  NOT_FOUND  "])
def test_a_bare_code_has_an_empty_reason(text: str) -> None:
    """Catches a parser that leaves `.reason` as the exception message (the
    code text) or the generic sentence when the server sent none, so a caller
    cannot tell "no reason given" from a reason.

    The reason is the empty string.
    """
    assert error_for(text).reason == ""


def test_a_reason_is_split_off_at_the_first_colon_only() -> None:
    """Catches a parser that splits on every colon and keeps the last piece, or
    refuses a reason that holds a colon.

    The reason keeps its own colon and the code is the first word.
    """
    raised = error_for("POLICY_BLOCKED: Not allowed: the scope is closed.")

    assert raised.code is ErrorCode.POLICY_BLOCKED
    assert raised.reason == "Not allowed: the scope is closed."


@pytest.mark.parametrize(
    "text",
    ["not_found: gone", "NOT_FOUND gone", "MADE_UP: gone", ": gone"],
)
def test_an_unknown_code_or_a_wrong_separator_is_still_the_opaque_internal(text: str) -> None:
    """Unhappy path. Catches a parser that matches the code loosely (lower
    case, a space instead of a colon) or accepts a
    code the enum does not have, so free text could pass as a typed refusal.

    Each text is the opaque internal error with the fixed transport message and
    no reason.
    """
    raised = error_for(text)

    assert isinstance(raised, RemoteInternalError)
    assert str(raised) == TRANSPORT_FAILURE_MESSAGE
    assert raised.reason == ""


def test_a_success_with_no_structured_content_is_the_opaque_internal() -> None:
    """The last shape: a result the client cannot parse.

    The real server always attaches structured content to a success
    (`pipeline._success_result`), so the only way to reach this branch is to
    stand in for the session — hence the private attribute, set here and
    nowhere else. Catches an implementation that returns
    `result.structured_content` unchecked: a node would then be handed `None`
    where it expects a mapping, and the failure would surface as an
    `AttributeError` inside `retrieve_policy` rather than as a typed turn
    ending."""

    class _EmptySession:
        async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
            return mcp_types.CallToolResult(content=[], structured_content=None, is_error=False)

    caller = McpToolCaller(url=OPAQUE_URL, bearer_token=FAKE_TOKEN)
    caller._session = cast(Any, _EmptySession())

    async def scenario() -> ToolError:
        with pytest.raises(RemoteToolError) as excinfo:
            await caller.call_tool("get_me", GetMeArgs())
        return excinfo.value

    raised = asyncio.run(scenario())

    assert isinstance(raised, RemoteInternalError)
    assert str(raised) == TRANSPORT_FAILURE_MESSAGE
