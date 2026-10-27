"""The model client: the only code here that calls the gateway's chat endpoint.

Models are named by alias only. Inputs are checked before sending, and no
gateway text, URL or provider name reaches an error, since it can quote the prompt.
"""

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from helpdesk.agent_api.services.model_gateway.strict_schema import strict_schema_problems
from helpdesk.agent_api.services.model_gateway.traceparent import traceparent_for
from helpdesk.shared.core.config import Settings
from helpdesk.shared.core.errors import ErrorCode, ToolError
from helpdesk.shared.core.naming import GATEWAY_USER_PATTERN, check_slug
from helpdesk.shared.gateway.keys import checked_tenant_key

CHAT_PATH = "/chat/completions"
"""The gateway's OpenAI-style chat endpoint, under the configured base URL."""

MAX_SCHEMA_NAME_LENGTH = 128
"""The gateway's schema-name cap, copied from the Ops Console, a separate repo."""

SCHEMA_NAME_PATTERN = re.compile(r"[a-zA-Z0-9_-]{1,128}")
"""The console's allowed name characters, matched in full so a trailing newline fails."""

DATA_CLASS = "confidential"
"""Chat carries company data, so its class is fixed and never a parameter."""

CONNECT_TIMEOUT_SECONDS = 5.0
"""Enough for a slow TLS handshake; a dead gateway errors instead of hanging."""

READ_TIMEOUT_SECONDS = 60.0
"""Generous, since a strong model answering a long turn is slow."""

STRUCTURED_OUTPUT_ATTEMPTS = 2
"""One call plus one retry, for bad model output only, never transport errors."""

PROMPT_VERSION_PATTERN = re.compile(r"[0-9a-f]{64}")
"""A prompt version is the sha256 hex digest of the prompt file, matched in full."""

BUDGET_EXCEEDED_CODE = "BUDGET_EXCEEDED"
"""The gateway's `error.code` on a 429 when a tenant's or a person's budget is spent."""

MAX_BUDGET_MESSAGE_CHARACTERS = 1_000
"""The longest gateway budget message shown to the person, counted after trimming."""


class ModelAlias(StrEnum):
    """The only model names this repo may send. The value is the wire name."""

    AGENT_DEFAULT = "agent-default"
    AGENT_STRONG = "agent-strong"


class ModelForbiddenError(ToolError):
    """The gateway refused the key, or the key does not own the tenant. Retrying cannot help."""

    code = ErrorCode.FORBIDDEN


class ModelRequestError(ToolError):
    """The request is malformed, found here or by the gateway. Fix the input."""

    code = ErrorCode.VALIDATION


class ModelUnavailableError(ToolError):
    """The gateway failed or its answer was unusable. The same turn may work again."""

    code = ErrorCode.INTERNAL


class ModelBudgetExceededError(ModelUnavailableError):
    """The gateway refused the call because a budget is spent.

    `gateway_message` is the gateway's own words for the person, shown as the
    reply. It stays off the log message, since the error message is for logs."""

    def __init__(self, gateway_message: str) -> None:
        super().__init__("the model gateway refused this turn: budget spent")
        self.gateway_message = gateway_message


@dataclass(frozen=True)
class ModelAnswer:
    """One completed turn. `alias` is what the caller asked for, not the gateway's echo.

    The provider and cost are the gateway's own headers, empty when it sent none."""

    text: str
    alias: ModelAlias
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    gateway_model: str
    gateway_trace_id: str
    gateway_provider: str = ""
    gateway_cost_usd: str = ""


@dataclass(frozen=True)
class ToolCall:
    """One tool call the model asked for, its arguments already read as a JSON object."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolCallAnswer:
    """One tool-calling turn: the text, the calls, or both, with its usage."""

    text: str | None
    calls: tuple[ToolCall, ...]
    usage: ModelAnswer


M = TypeVar("M", bound=BaseModel)


@dataclass(frozen=True)
class StructuredAnswer[T: BaseModel]:
    """A checked structured answer, with the usage of the call that made it."""

    value: T
    usage: ModelAnswer


class ModelClient:
    """One httpx client, three call shapes, typed refusals."""

    def __init__(
        self,
        *,
        base_url: str,
        api_keys: Mapping[str, str],
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_keys = dict(api_keys)
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=CONNECT_TIMEOUT_SECONDS,
                read=READ_TIMEOUT_SECONDS,
                write=READ_TIMEOUT_SECONDS,
                pool=CONNECT_TIMEOUT_SECONDS,
            ),
            transport=transport,
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> "ModelClient":
        """Build the client from loaded settings."""
        return cls(base_url=settings.gateway_url, api_keys=settings.gateway_api_keys)

    @property
    def base_url(self) -> str:
        """The gateway's configured base URL, which the readiness check probes."""
        return self._base_url

    async def aclose(self) -> None:
        """Close the underlying connection pool."""
        await self.client.aclose()

    async def complete(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        alias: ModelAlias,
        tenant_slug: str,
        session_id: str | None = None,
        user: str | None = None,
        traceparent: str | None = None,
        prompt_version: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> ModelAnswer:
        """One non-streaming turn."""
        headers = self._headers(tenant_slug, session_id, user, traceparent, prompt_version)
        body = self._body(messages, alias, max_tokens, temperature)
        response = await self._post(body, headers)
        return self._parsed(response, alias)

    async def complete_json(
        self,
        schema_model: type[M],
        messages: Sequence[Mapping[str, str]],
        *,
        alias: ModelAlias,
        tenant_slug: str,
        session_id: str | None = None,
        user: str | None = None,
        traceparent: str | None = None,
        prompt_version: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> StructuredAnswer[M]:
        """One turn held to `schema_model`'s shape, in strict mode, and checked here too.

        A reply that does not fit is retried once, then raised.
        """
        headers = self._headers(tenant_slug, session_id, user, traceparent, prompt_version)
        body = self._body(messages, alias, max_tokens, temperature)
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": _checked_schema_name(schema_model.__name__),
                "schema": _checked_strict_schema(schema_model),
                "strict": True,
            },
        }

        for attempt in range(STRUCTURED_OUTPUT_ATTEMPTS):
            answer = self._parsed(await self._post(body, headers), alias)
            try:
                return StructuredAnswer(
                    value=schema_model.model_validate_json(answer.text), usage=answer
                )
            except ValidationError:
                if attempt == STRUCTURED_OUTPUT_ATTEMPTS - 1:
                    raise ModelUnavailableError(
                        "the model did not return output matching the requested schema"
                    ) from None
        raise AssertionError("unreachable: the loop returns or raises on its last attempt")

    async def complete_with_tools(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
        *,
        alias: ModelAlias,
        tenant_slug: str,
        session_id: str | None = None,
        user: str | None = None,
        traceparent: str | None = None,
        prompt_version: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        answer_only: bool = False,
    ) -> ToolCallAnswer:
        """One turn that may call tools. Messages go as given, assistant
        `tool_calls` and `tool` turns included. No tools sends neither `tools`
        nor `tool_choice`, so the model can only answer in text. `answer_only`
        sends the tools with `tool_choice: "none"`, so a text-only step keeps
        the same tool part as every other call and the gateway can cache it."""
        headers = self._headers(tenant_slug, session_id, user, traceparent, prompt_version)
        body = self._body(messages, alias, max_tokens, temperature)
        if tools:
            body["tools"] = [dict(tool) for tool in tools]
            body["tool_choice"] = "none" if answer_only else "auto"
        response = await self._post(body, headers)
        message, usage = self._read(response, alias)
        text = message.get("content")
        if text is not None and not isinstance(text, str):
            raise ModelUnavailableError("the model gateway returned a body this client cannot read")
        return ToolCallAnswer(
            text=text, calls=_tool_calls(message.get("tool_calls")), usage=usage(text or "")
        )

    def _headers(
        self,
        tenant_slug: str,
        session_id: str | None,
        user: str | None,
        traceparent: str | None,
        prompt_version: str | None,
    ) -> dict[str, str]:
        """The headers, with caller parts checked. `X-Session`, `X-User` and
        `X-Prompt-Version` are left out, not sent empty. `X-User` is the random
        gateway id of a signed-in user; a call with no user, a job
        or an eval, sends none."""
        try:
            slug, key = checked_tenant_key(tenant_slug, self._api_keys)
            checked_session = None if session_id is None else check_slug(session_id)
            trace = traceparent_for(traceparent)
        except ToolError as exc:
            raise ModelRequestError(str(exc)) from exc

        headers = {
            "Authorization": f"Bearer {key}",
            "X-Tenant": slug,
            "X-Data-Class": DATA_CLASS,
            "traceparent": trace,
        }
        if checked_session is not None:
            headers["X-Session"] = checked_session
        if user is not None:
            if GATEWAY_USER_PATTERN.fullmatch(user) is None:
                raise ModelRequestError("a gateway user id must be u_ and 32 lowercase hex")
            headers["X-User"] = user
        if prompt_version is not None:
            if PROMPT_VERSION_PATTERN.fullmatch(prompt_version) is None:
                raise ModelRequestError("a prompt version must be a lowercase sha256 hex digest")
            headers["X-Prompt-Version"] = prompt_version
        return headers

    def _body(
        self,
        messages: Sequence[Mapping[str, Any]],
        alias: ModelAlias,
        max_tokens: int | None,
        temperature: float | None,
    ) -> dict[str, Any]:
        """The OpenAI-style body. Unset options are left out so gateway defaults apply."""
        body: dict[str, Any] = {
            "model": alias.value,
            "messages": [dict(message) for message in messages],
            "stream": False,
        }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if temperature is not None:
            body["temperature"] = temperature
        return body

    async def _post(self, body: Mapping[str, Any], headers: Mapping[str, str]) -> httpx.Response:
        """Send one request, mapping a transport failure onto the typed code."""
        try:
            return await self.client.post(
                f"{self._base_url}{CHAT_PATH}", json=dict(body), headers=dict(headers)
            )
        except httpx.HTTPError as exc:
            # The exception text holds the URL; keep it in the cause, not our message.
            raise ModelUnavailableError("the model gateway could not be reached") from exc

    def _parsed(self, response: httpx.Response, alias: ModelAlias) -> ModelAnswer:
        """A `ModelAnswer`, or a typed error: 401/403 is forbidden, 422 bad input, else internal."""
        message, usage = self._read(response, alias)
        text = message.get("content")
        if not isinstance(text, str):
            raise ModelUnavailableError("the model gateway returned a body this client cannot read")
        return usage(text)

    def _read(
        self, response: httpx.Response, alias: ModelAlias
    ) -> tuple[dict[str, Any], Callable[[str], ModelAnswer]]:
        """The answer's message and a builder of its `ModelAnswer`, or a typed error."""
        if response.status_code in (401, 403):
            raise ModelForbiddenError("the model gateway refused this tenant's API key")
        if response.status_code == 422:
            raise ModelRequestError("the model gateway refused this request")
        if response.status_code == 429 and (message := _budget_message(response)) is not None:
            raise ModelBudgetExceededError(message)
        if response.status_code != 200:
            raise ModelUnavailableError("the model gateway failed to complete this turn")

        try:
            body = response.json()
            message = body["choices"][0]["message"]
            usage = body["usage"]
            counts = (
                usage["prompt_tokens"],
                usage["completion_tokens"],
                usage["total_tokens"],
            )
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ModelUnavailableError(
                "the model gateway returned a body this client cannot read"
            ) from exc
        if not isinstance(message, dict) or not all(isinstance(count, int) for count in counts):
            raise ModelUnavailableError("the model gateway returned a body this client cannot read")

        def answer(text: str) -> ModelAnswer:
            return ModelAnswer(
                text=text,
                alias=alias,
                prompt_tokens=counts[0],
                completion_tokens=counts[1],
                total_tokens=counts[2],
                gateway_model=response.headers.get("X-Gateway-Model", ""),
                gateway_trace_id=response.headers.get("X-Gateway-Trace-Id", ""),
                gateway_provider=response.headers.get("X-Gateway-Provider", ""),
                gateway_cost_usd=response.headers.get("X-Gateway-Cost-USD", ""),
            )

        return message, answer


def _budget_message(response: httpx.Response) -> str | None:
    """The trimmed `error.message` of a budget refusal, or None when the body is
    not one or its text is not 1 to `MAX_BUDGET_MESSAGE_CHARACTERS` characters.
    None falls back to the generic error."""
    try:
        error = response.json()["error"]
        code, message = error["code"], error["message"]
    except (ValueError, KeyError, TypeError):
        return None
    if code != BUDGET_EXCEEDED_CODE or not isinstance(message, str):
        return None
    message = message.strip()
    if not 1 <= len(message) <= MAX_BUDGET_MESSAGE_CHARACTERS:
        return None
    return message


def _tool_calls(raw: object) -> tuple[ToolCall, ...]:
    """The answer's tool calls, each with its arguments read as one JSON object.
    Anything else is unusable model output, so `INTERNAL`."""
    if raw is None:
        return ()
    unreadable = ModelUnavailableError("the model asked for a tool call this client cannot read")
    if not isinstance(raw, list):
        raise unreadable
    calls: list[ToolCall] = []
    for entry in raw:
        try:
            call_id = entry["id"]
            name = entry["function"]["name"]
            arguments = json.loads(entry["function"]["arguments"])
        except (ValueError, KeyError, TypeError) as exc:
            raise unreadable from exc
        if not isinstance(call_id, str) or not isinstance(name, str):
            raise unreadable
        if not isinstance(arguments, dict):
            raise unreadable
        calls.append(ToolCall(id=call_id, name=name, arguments=arguments))
    return tuple(calls)


def _checked_schema_name(name: str) -> str:
    """The schema name if allowed, else a typed refusal before any request."""
    if len(name) > MAX_SCHEMA_NAME_LENGTH:
        raise ModelRequestError(
            f"a schema name longer than {MAX_SCHEMA_NAME_LENGTH} characters cannot be sent: "
            f"{len(name)}"
        )
    if SCHEMA_NAME_PATTERN.fullmatch(name) is None:
        raise ModelRequestError(f"a schema name must match {SCHEMA_NAME_PATTERN.pattern}")
    return name


def _checked_strict_schema(schema_model: type[BaseModel]) -> dict[str, Any]:
    """The model's JSON schema, or a typed refusal naming each strict-mode problem."""
    schema = schema_model.model_json_schema()
    problems = strict_schema_problems(schema)
    if problems:
        raise ModelRequestError(
            f"the {schema_model.__name__} schema breaks strict mode: " + "; ".join(problems)
        )
    return schema
