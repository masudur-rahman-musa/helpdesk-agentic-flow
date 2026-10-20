"""The model client: the only code here that calls the gateway's chat endpoint.

Models are named by alias only. Inputs are checked before sending, and no
gateway text, URL or provider name reaches an error, since it can quote the prompt.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from helpdesk.agent_api.services.model_gateway.strict_schema import strict_schema_problems
from helpdesk.agent_api.services.model_gateway.traceparent import traceparent_for
from helpdesk.shared.core.config import Settings
from helpdesk.shared.core.errors import ErrorCode, ToolError
from helpdesk.shared.core.naming import check_slug
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


@dataclass(frozen=True)
class ModelAnswer:
    """One completed turn. `alias` is what the caller asked for, not the gateway's echo.

    The provider, region and cost are the gateway's own headers, empty when it sent none."""

    text: str
    alias: ModelAlias
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    gateway_model: str
    gateway_trace_id: str
    gateway_provider: str = ""
    gateway_region: str = ""
    gateway_cost_usd: str = ""


M = TypeVar("M", bound=BaseModel)


@dataclass(frozen=True)
class StructuredAnswer[T: BaseModel]:
    """A checked structured answer, with the usage of the call that made it."""

    value: T
    usage: ModelAnswer


class ModelClient:
    """One httpx client, two call shapes, typed refusals."""

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
        traceparent: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> ModelAnswer:
        """One non-streaming turn."""
        headers = self._headers(tenant_slug, session_id, traceparent)
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
        traceparent: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> StructuredAnswer[M]:
        """One turn held to `schema_model`'s shape, in strict mode, and checked here too.

        A reply that does not fit is retried once, then raised.
        """
        headers = self._headers(tenant_slug, session_id, traceparent)
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

    def _headers(
        self, tenant_slug: str, session_id: str | None, traceparent: str | None
    ) -> dict[str, str]:
        """The headers, with caller parts checked. `X-Session` is left out, not sent empty."""
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
        return headers

    def _body(
        self,
        messages: Sequence[Mapping[str, str]],
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
        if response.status_code in (401, 403):
            raise ModelForbiddenError("the model gateway refused this tenant's API key")
        if response.status_code == 422:
            raise ModelRequestError("the model gateway refused this request")
        if response.status_code != 200:
            raise ModelUnavailableError("the model gateway failed to complete this turn")

        try:
            body = response.json()
            text = body["choices"][0]["message"]["content"]
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
        if not isinstance(text, str) or not all(isinstance(count, int) for count in counts):
            raise ModelUnavailableError("the model gateway returned a body this client cannot read")

        return ModelAnswer(
            text=text,
            alias=alias,
            prompt_tokens=counts[0],
            completion_tokens=counts[1],
            total_tokens=counts[2],
            gateway_model=response.headers.get("X-Gateway-Model", ""),
            gateway_trace_id=response.headers.get("X-Gateway-Trace-Id", ""),
            gateway_provider=response.headers.get("X-Gateway-Provider", ""),
            gateway_region=response.headers.get("X-Gateway-Region", ""),
            gateway_cost_usd=response.headers.get("X-Gateway-Cost-USD", ""),
        )


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
