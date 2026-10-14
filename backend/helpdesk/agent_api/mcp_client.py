"""The agent's side of the MCP boundary: one session per turn, typed errors.

agent-api and mcp-server are separate deployables that talk over streamable
HTTP and never by import (the design and §5). This module is the whole of that
seam: it opens the session, forwards the caller's token, and turns whatever
comes back into either a plain mapping or a `ToolError` — the same currency
every other failure in this repo is denominated in.

**The token is forwarded verbatim**. The agent signs nothing and
sends no identity as a tool argument: the bearer the route verified is put on
the HTTP client as-is, and the SERVER decides what that person may do. A token
for another tenant is refused there, not here, pinned by
`test_agent_mcp_client.py::test_an_errored_result_maps_back_to_the_subclass_its_code_names`.

**One session per turn** (decision B). The initialize handshake is a round
trip and an earlier change makes several calls on one turn, so `run_turn` opens the
caller once and closes it with the turn. No node owns a connection, and the
close happens on the failing path too, pinned by
`::test_the_session_is_closed_when_the_call_raises`.

**One opaque `INTERNAL` for everything the protocol did not decide.** An
unreachable host, a 500, an unparseable answer and a missing structured body
are all the same `RemoteInternalError` carrying `TRANSPORT_FAILURE_MESSAGE`,
which names neither the URL nor the transport: a hostname or a status code in
front of an end user is an internal detail leaked through a chat window
(`::test_an_unreachable_server_is_one_typed_internal_naming_neither_host_nor_path`,
`::test_a_non_2xx_answer_is_the_same_typed_internal`).

`asyncio.CancelledError` is NOT converted, the same reasoning as
`mcp_server.pipeline`'s: a cancelled call did not fail, and typing it as one
would fabricate a tool failure. It is a `BaseException`, so the `except
Exception` below cannot reach it, and neither can it reach a `BaseExceptionGroup`
carrying one.
"""

import types as pytypes
from collections.abc import Mapping
from contextlib import AsyncExitStack
from types import MappingProxyType
from typing import Any, Protocol, Self, runtime_checkable

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from pydantic import BaseModel

from helpdesk.agent_api.services.tracing import span
from helpdesk.shared.errors import ErrorCode, ToolError

MCP_CONNECT_TIMEOUT_SECONDS = 5.0
"""How long a turn waits to reach the MCP server at all.

Short on purpose: an unreachable server is a state a turn should report, not
sit in. a project rule and 14 — the value is pinned by
`test_agent_mcp_client.py::test_the_client_timeouts_are_exactly_the_planned_values`
and asserted on the CONSTRUCTED client by
`::test_the_constructed_client_carries_both_halves_of_the_timeout`.
"""

MCP_READ_TIMEOUT_SECONDS = 30.0
"""How long a turn waits for a tool to answer once the request is in.

The slowest tool the agent calls is `search_policy`, which embeds a query and
searches a vector store, so this is the same deadline the ingest gives Qdrant
(`Settings.qdrant_timeout_seconds`). A read half as short as the connect half
would fail turns the server was about to answer.
"""

TRANSPORT_FAILURE_MESSAGE = "the policy service could not be reached"
"""The single message every undecided failure ends on.

A constant, never an f-string over the URL: this string reaches a user through
`fail_turn`, and the host, the port, the path and the status code are all
internal detail. Pinned by
`test_agent_mcp_client.py::test_the_transport_failure_message_names_neither_host_nor_path`.
"""


class RemoteToolError(ToolError):
    """Base for the eight errors this client can raise.

    One class per the design code, because the wire carries the CODE and
    nothing else: the specific class the server raised (`GrantNotActiveError`,
    `NotAnApproverError`, ...) does not survive the boundary, so recovering it
    is impossible and pretending otherwise would be a guess. A caller catches
    `ToolError` and reads `.code`, which is this repo's idiom everywhere else.
    """


class RemoteForbiddenError(RemoteToolError):
    """The server refused this caller. A decision, never an outage."""

    code = ErrorCode.FORBIDDEN


class RemoteNotFoundError(RemoteToolError):
    """Not in the caller's tenant, or not there at all — deliberately one answer."""

    code = ErrorCode.NOT_FOUND


class RemoteSodConflictError(RemoteToolError):
    """Segregation of duties blocked it. Something the user can act on."""

    code = ErrorCode.SOD_CONFLICT


class RemotePolicyBlockedError(RemoteToolError):
    """A policy rule blocked it. Also something the user can act on."""

    code = ErrorCode.POLICY_BLOCKED


class RemoteIdempotencyConflictError(RemoteToolError):
    """The same key was reused for a different request (the design)."""

    code = ErrorCode.IDEMPOTENCY_CONFLICT


class RemoteToolDisabledError(RemoteToolError):
    """An admin tripped the tool's kill switch. Not a permission decision."""

    code = ErrorCode.TOOL_DISABLED


class RemoteValidationError(RemoteToolError):
    """The server refused the arguments — in practice, catalogue drift."""

    code = ErrorCode.VALIDATION


class RemoteInternalError(RemoteToolError):
    """Everything the protocol did not decide, under one opaque message."""

    code = ErrorCode.INTERNAL


REMOTE_ERRORS: Mapping[ErrorCode, type[RemoteToolError]] = MappingProxyType(
    {
        ErrorCode.FORBIDDEN: RemoteForbiddenError,
        ErrorCode.NOT_FOUND: RemoteNotFoundError,
        ErrorCode.SOD_CONFLICT: RemoteSodConflictError,
        ErrorCode.POLICY_BLOCKED: RemotePolicyBlockedError,
        ErrorCode.IDEMPOTENCY_CONFLICT: RemoteIdempotencyConflictError,
        ErrorCode.TOOL_DISABLED: RemoteToolDisabledError,
        ErrorCode.VALIDATION: RemoteValidationError,
        ErrorCode.INTERNAL: RemoteInternalError,
    }
)
"""Every one of the design's eight codes, each to the class that declares it.

A row for every member of `ErrorCode`, not for the ones this plan's single
tool happens to return: a code with no row would be read as an unparseable
answer and collapsed into `INTERNAL`, which is exactly how a
`SOD_CONFLICT` a user could act on becomes "something went wrong". Pinned
over the whole enum by
`test_agent_mcp_client.py::test_remote_errors_covers_every_error_code_exactly_once`.
"""


def error_for(text: str) -> RemoteToolError:
    """The typed error an errored result's content text names.

    A pure seam, deliberately not inlined: an answer whose text is not one of
    the eight codes cannot be produced by our own server (`pipeline.error_result`
    writes the code and nothing else), so the only way to reach that branch is
    to call this function directly. Pinned by
    `test_agent_mcp_client.py::test_an_unparseable_error_text_is_the_opaque_internal`.
    """
    try:
        code = ErrorCode(text.strip())
    except ValueError:
        return RemoteInternalError(TRANSPORT_FAILURE_MESSAGE)
    return REMOTE_ERRORS[code](text.strip())


@runtime_checkable
class ToolCaller(Protocol):
    """What a node is allowed to do with the MCP boundary.

    A protocol rather than the concrete client, so every node test runs
    against a stub and only this module's own test opens a real session. The
    context-manager half is part of the contract because the SESSION's lifetime
    is the turn's (decision B), and `run_turn` is what enters it.
    """

    async def __aenter__(self) -> "ToolCaller": ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: pytypes.TracebackType | None,
    ) -> None: ...

    async def call_tool(self, name: str, arguments: BaseModel) -> Mapping[str, Any]: ...


class McpToolCaller:
    """One MCP session over streamable HTTP, for the length of one turn.

    The httpx client is built in `__init__` and entered in `__aenter__`, so
    both halves of its timeout and the forwarded `Authorization` header are
    assertable on the constructed object rather than on the constants.

    `transport` is the in-process seam the tests drive: an `ASGITransport` over
    the real `mcp_server` app, which is how the transport is proven end to end
    without opening a socket. Production passes nothing and httpx2 dials for
    real.
    """

    def __init__(
        self,
        *,
        url: str,
        bearer_token: str,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self.url = url
        self.http_client = httpx2.AsyncClient(
            transport=transport,
            timeout=httpx2.Timeout(
                connect=MCP_CONNECT_TIMEOUT_SECONDS,
                read=MCP_READ_TIMEOUT_SECONDS,
                write=MCP_READ_TIMEOUT_SECONDS,
                pool=MCP_CONNECT_TIMEOUT_SECONDS,
            ),
            headers={"Authorization": f"Bearer {bearer_token}"},
        )
        self._stack: AsyncExitStack | None = None
        self._session: Client | None = None

    async def __aenter__(self) -> Self:
        """Open the HTTP client and the MCP session, or close both and refuse.

        A handshake that fails leaves nothing open: the stack is unwound here
        rather than in `__aexit__`, which Python does not call when `__aenter__`
        raises. That is what makes
        `test_agent_mcp_client.py::test_an_unreachable_server_is_one_typed_internal_naming_neither_host_nor_path`
        able to assert the client is closed after a refused dial.
        """
        stack = AsyncExitStack()
        try:
            await stack.enter_async_context(self.http_client)
            self._session = await stack.enter_async_context(
                Client(streamable_http_client(self.url, http_client=self.http_client))
            )
        except Exception as exc:
            await stack.aclose()
            raise RemoteInternalError(TRANSPORT_FAILURE_MESSAGE) from exc
        self._stack = stack
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: pytypes.TracebackType | None,
    ) -> None:
        """Close the session and the pool, whatever happened inside the turn."""
        stack, self._stack, self._session = self._stack, None, None
        if stack is not None:
            await stack.aclose()

    async def call_tool(self, name: str, arguments: BaseModel) -> Mapping[str, Any]:
        """One `tools/call`, answered as a mapping or raised as a `ToolError`.

        The arguments are a Pydantic model from `tools/catalogue.py` and are
        dumped in JSON mode, so a uuid or a date reaches the wire in the shape
        the server's own model parses. Nothing about the caller's identity is
        put here: it rides on the header set in `__init__`.
        """
        if self._session is None:  # pragma: no cover - `run_turn` always enters first
            raise RemoteInternalError(TRANSPORT_FAILURE_MESSAGE)

        try:
            result = await self._session.call_tool(name, arguments.model_dump(mode="json"))
        except ToolError:
            raise
        except Exception as exc:
            raise RemoteInternalError(TRANSPORT_FAILURE_MESSAGE) from exc

        if result.is_error:
            raise error_for(_first_text(result.content))
        structured: Mapping[str, Any] | None = result.structured_content
        if structured is None:
            raise RemoteInternalError(TRANSPORT_FAILURE_MESSAGE)
        return structured


TOOL_SPAN_PREFIX = "tool:"
"""What a tool span's name begins with, so it reads apart from a node's.

One constant rather than an f-string at the call site: the prefix is what
separates `tool:search_policy` from a node named `search_policy` in a
Langfuse view, and a bare tool name would collide with one. a project rule,
pinned by
`test_agent_tracing.py::test_the_tool_span_prefix_is_exactly_the_planned_value`.
"""


class TracingToolCaller:
    """A `ToolCaller` that opens one span per call around whichever it wraps.

    A decorator over the PROTOCOL rather than a span inside
    `McpToolCaller.call_tool`: it covers every call made
    through the protocol — `retrieve_policy`'s `search_policy` included — and
    it is testable against the same stub every node test already uses, with no
    session and no socket.

    The span wraps the CALL and not the session. `__aenter__` and `__aexit__`
    are forwarded untraced, so the span sits under the node that made the call
    rather than under the turn, which is the only question a tool span answers
    (`test_agent_tracing.py::test_the_tool_call_opens_a_span_under_the_node_that_made_it`).

    **No argument reaches the span** (decision G). `search_policy`'s argument
    is a query derived from the user's message, so a wrapper capturing inputs
    would publish that message as metadata; the span carries the tool's name
    and nothing else, pinned by
    `::test_no_tool_argument_reaches_the_span_that_wraps_the_call`.
    """

    def __init__(self, inner: ToolCaller) -> None:
        self.inner = inner

    async def __aenter__(self) -> "TracingToolCaller":
        await self.inner.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: pytypes.TracebackType | None,
    ) -> None:
        await self.inner.__aexit__(exc_type, exc, tb)

    async def call_tool(self, name: str, arguments: BaseModel) -> Mapping[str, Any]:
        """The inner caller's answer, verbatim, inside one `tool:{name}` span.

        The answer is returned unchanged and a `ToolError` still propagates:
        every node reads the mapping the tool returned, so a wrapper reshaping
        it would break the turn where no span assertion could see it
        (`test_agent_tracing.py::test_the_tracing_caller_returns_the_inner_callers_answer`).
        """
        with span(f"{TOOL_SPAN_PREFIX}{name}"):
            return await self.inner.call_tool(name, arguments)


def _first_text(content: object) -> str:
    """The text of an errored result's first content block, or the empty string.

    Defensive rather than trusting: an errored result from our own server is
    exactly one `TextContent` carrying the code (`pipeline.error_result`), but
    this client also has to survive a server that answers something else, and
    an empty string falls through `error_for` to the opaque `INTERNAL`.
    """
    blocks = list(content) if isinstance(content, list) else []
    if not blocks:
        return ""
    text = getattr(blocks[0], "text", "")
    return text if isinstance(text, str) else ""


__all__ = [
    "MCP_CONNECT_TIMEOUT_SECONDS",
    "MCP_READ_TIMEOUT_SECONDS",
    "REMOTE_ERRORS",
    "TOOL_SPAN_PREFIX",
    "TRANSPORT_FAILURE_MESSAGE",
    "McpToolCaller",
    "RemoteForbiddenError",
    "RemoteIdempotencyConflictError",
    "RemoteInternalError",
    "RemoteNotFoundError",
    "RemotePolicyBlockedError",
    "RemoteSodConflictError",
    "RemoteToolDisabledError",
    "RemoteToolError",
    "RemoteValidationError",
    "ToolCaller",
    "TracingToolCaller",
    "error_for",
]
