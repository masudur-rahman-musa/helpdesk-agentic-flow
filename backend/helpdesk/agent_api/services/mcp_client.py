"""The agent's side of MCP: one session per turn, answers or typed errors.

The caller's token is forwarded as is. Any failure the protocol did not decide
becomes one opaque `INTERNAL` naming no host or path.
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
from helpdesk.shared.core.errors import ErrorCode, ToolError

MCP_CONNECT_TIMEOUT_SECONDS = 5.0
"""Short on purpose: an unreachable server should be reported, not waited on."""

MCP_READ_TIMEOUT_SECONDS = 30.0
"""Long enough for the slowest tool, a policy search that embeds and queries vectors."""

TRANSPORT_FAILURE_MESSAGE = "the policy service could not be reached"
"""A fixed string, since it can reach a user and must not name a host or path."""


class RemoteToolError(ToolError):
    """Base for this client's errors, one per code; only the code crosses the wire."""


class RemoteForbiddenError(RemoteToolError):
    """The server refused this caller. A decision, never an outage."""

    code = ErrorCode.FORBIDDEN


class RemoteNotFoundError(RemoteToolError):
    """Not in the caller's tenant, or not there at all. One answer on purpose."""

    code = ErrorCode.NOT_FOUND


class RemoteSodConflictError(RemoteToolError):
    """Segregation of duties blocked it. Something the user can act on."""

    code = ErrorCode.SOD_CONFLICT


class RemotePolicyBlockedError(RemoteToolError):
    """A policy rule blocked it. Also something the user can act on."""

    code = ErrorCode.POLICY_BLOCKED


class RemoteIdempotencyConflictError(RemoteToolError):
    """The same key was reused for a different request."""

    code = ErrorCode.IDEMPOTENCY_CONFLICT


class RemoteToolDisabledError(RemoteToolError):
    """An admin tripped the tool's kill switch. Not a permission decision."""

    code = ErrorCode.TOOL_DISABLED


class RemoteValidationError(RemoteToolError):
    """The server refused the arguments, in practice a tool list out of date."""

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
"""Every code has a class, so an actionable one never collapses into `INTERNAL`."""


def error_for(text: str) -> RemoteToolError:
    """The typed error an errored result's text names, or the opaque `INTERNAL`."""
    try:
        code = ErrorCode(text.strip())
    except ValueError:
        return RemoteInternalError(TRANSPORT_FAILURE_MESSAGE)
    return REMOTE_ERRORS[code](text.strip())


@runtime_checkable
class ToolCaller(Protocol):
    """What a node may do with MCP. A protocol, so node tests can use a stub."""

    async def __aenter__(self) -> "ToolCaller": ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: pytypes.TracebackType | None,
    ) -> None: ...

    async def call_tool(self, name: str, arguments: BaseModel) -> Mapping[str, Any]: ...


class McpToolCaller:
    """One MCP session over streamable HTTP, for one turn. `transport` is for tests."""

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
        """Open the client and session, or close both and refuse; `__aexit__` would not run."""
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
        """One `tools/call`, as a mapping or a raised `ToolError`."""
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
"""Keeps a tool span from clashing with a graph node of the same name."""


class TracingToolCaller:
    """A `ToolCaller` that wraps each call in a span under the calling node.

    No argument is recorded, since a search query comes from the user's message.
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
        """The inner caller's answer, unchanged, inside one `tool:{name}` span."""
        with span(f"{TOOL_SPAN_PREFIX}{name}"):
            return await self.inner.call_tool(name, arguments)


def _first_text(content: object) -> str:
    """The text of an errored result's first block, or "" for anything unexpected."""
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
