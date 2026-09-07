"""The model client: the only place in this repo that calls the gateway's chat
endpoint (a project rule, the design).

The model is named by ALIAS and never by a provider model id. `ModelAlias` is
a closed StrEnum of exactly the two names the design routes, so mypy refuses a
bare string where an alias belongs and
`test_gateway_client.py::test_no_provider_model_name_appears_in_the_repo`
sweeps the two source trees for the ids that would make "we only use aliases"
quietly untrue.

Every request carries five headers. `Authorization` and `X-Tenant` come from
ONE lookup (`gateway/keys.py`, a design note), so the pair the gateway compares can
never be assembled from different places; `X-Data-Class` is fixed at
`confidential` (decision F) because chat carries company data the wider
`internal` class must not route; `traceparent` is the caller's or a fresh one
(decision C), which is what makes the two services' spans one trace; and
`X-Session` is present exactly when a session id was given, never present and
empty, because the console reads its PRESENCE
(`test_gateway_client.py::test_the_session_header_is_absent_when_no_session_is_given`).

Three pre-checks run BEFORE any request is built — the tenant's slug and key,
the session id (`check_slug`), the caller's `traceparent`, and for structured
output the schema name — so a refusal is a typed `VALIDATION` with the prompt
never put on the wire, rather than a 4xx read back from the gateway. Pinned by
`test_gateway_client.py::test_a_session_id_one_character_over_the_cap_is_refused_before_any_request`,
`::test_a_schema_name_one_character_over_the_cap_is_refused_before_any_request` and
`::test_a_malformed_caller_traceparent_is_refused_before_any_request`, each
asserting the transport was never called.

No gateway wording, no URL and no provider name reaches a caller's message:
the gateway's body can quote the prompt it was given, which on this path is
the one string carrying confidential company data, and it names the provider
and model that served the turn. Pinned by
`test_gateway_client.py::test_no_gateway_wording_url_or_provider_name_reaches_the_message`.

Non-streaming only (decision B): row 19 owns SSE to the browser, where a
stream has a consumer, and adds a method rather than rewriting this one.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from helpdesk.core.settings import Settings
from helpdesk.errors import ErrorCode, ToolError
from helpdesk.gateway.keys import checked_tenant_key
from helpdesk.gateway.traceparent import traceparent_for
from helpdesk.rag.naming import check_slug

CHAT_PATH = "/chat/completions"
"""The gateway's OpenAI-shaped chat endpoint, hung off the versioned base URL
`Settings.gateway_url` already carries."""

MAX_SCHEMA_NAME_LENGTH = 128
"""The gateway's cap on a response-format schema name, copied from
`opsconsole.providers.base`'s `MAX_TOOL_NAME_LENGTH`.

A COPY, not an import: the console is a separate repository and nothing here
depends on it at build time, exactly as `MAX_REDACTION_CHARS` copies its
redaction cap. The value is pinned by
`test_gateway_client.py::test_the_model_client_constants_are_exactly_the_planned_values`,
and this docstring names the module it mirrors, so a console change surfaces
as a 422 whose cause is one grep away rather than a local failure.
"""

SCHEMA_NAME_PATTERN = re.compile(r"[a-zA-Z0-9_-]{1,128}")
"""The character set a schema name may use, mirroring the console's
`TOOL_NAME_PATTERN` and matched with `re.fullmatch`.

The name goes into a JSON body the console re-emits to a provider, so a value
ending in a newline is refused here rather than travelling. It is the narrower
of the two gates for a name containing anything outside the class, while
`MAX_SCHEMA_NAME_LENGTH` decides length alone; both boundaries are pinned by
`test_gateway_client.py::test_a_schema_name_of_exactly_the_cap_is_accepted`,
`::test_a_schema_name_one_character_over_the_cap_is_refused_before_any_request`
and `::test_a_schema_name_outside_the_pattern_is_refused_before_any_request`.
"""

DATA_CLASS = "confidential"
"""the design's class for chat. Fixed, never a parameter (decision F): a
parameter is an invitation to send a chat turn under a wider class, which is
the one routing mistake residency cannot recover from. Policy ingestion's
`internal` lives one package over in `rag/gateway.py`."""

CONNECT_TIMEOUT_SECONDS = 5.0
"""Long enough for a TLS handshake on a slow link, short enough that an
unreachable gateway is an error rather than a hang."""

READ_TIMEOUT_SECONDS = 60.0
"""Wider than the redaction client's 30.0: a generation is slower than a scan,
and a strong alias answering a long turn spends most of it generating. Both
halves are asserted on the constructed client by
`test_gateway_client.py::test_the_constructed_client_carries_both_timeout_halves`."""

STRUCTURED_OUTPUT_ATTEMPTS = 2
"""One call plus exactly one retry (the design step 2, decision D).

The retry is about the model's OUTPUT, never the transport: a 5xx or a timeout
is raised rather than retried, because the caller that knows whether a turn can
be re-run is the agent runtime. The count is pinned from both sides by
`test_gateway_client.py::test_complete_json_retries_exactly_once_and_returns_the_second_answer`
and `::test_complete_json_gives_up_after_the_second_invalid_answer`, whose
handler fails the test on a third request."""


class ModelAlias(StrEnum):
    """The only routable model names (the design). Exactly two, closed.

    The VALUE is what goes into the request's `model` field, so the hyphen
    spelling is the contract and not a style choice. Pinned by
    `test_gateway_client.py::test_model_alias_has_exactly_the_two_spec_members`.
    """

    AGENT_DEFAULT = "agent-default"
    AGENT_STRONG = "agent-strong"


class ModelForbiddenError(ToolError):
    """The gateway refused the key, or the key does not own the tenant.

    A 403 here is the control a design note exists for: the tenant in the header
    and the tenant the key owns disagreed. Neither is something a retry fixes.
    """

    code = ErrorCode.FORBIDDEN


class ModelRequestError(ToolError):
    """This request is malformed, or would be. Raised for the local pre-checks
    and for the gateway's own 422 alike, because both say the same thing to a
    caller: fix the input, retrying unchanged cannot help."""

    code = ErrorCode.VALIDATION


class ModelUnavailableError(ToolError):
    """The gateway could not be reached, failed on its side, or answered
    something this client cannot turn into an answer.

    Deliberately not `VALIDATION`, including for a structured output still
    invalid after its one retry: the caller's arguments were fine and editing
    them cannot help, while the same turn run again may well succeed.
    """

    code = ErrorCode.INTERNAL


@dataclass(frozen=True)
class ModelAnswer:
    """One completed turn, with the operator facts that trace it.

    `alias` is the one the CALLER named, never the name echoed in the body:
    the body's `model` is the gateway's own echo, and reading it back would
    make an answer describe a routing decision this process did not make.

    `gateway_model` and `gateway_trace_id` are operator facts for a log and a
    trace — they are the only handles tying a turn here to a row in the
    console's log — and no code path puts either into a prompt. Frozen so a
    caller cannot rewrite the counts a turn is billed and capped by.
    """

    text: str
    alias: ModelAlias
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    gateway_model: str
    gateway_trace_id: str


M = TypeVar("M", bound=BaseModel)


@dataclass(frozen=True)
class StructuredAnswer[T: BaseModel]:
    """A validated structured answer and the usage of the call that produced it.

    `complete_json` returns this rather than the bare model (an earlier change decision
    A) because the `ModelAnswer` carries the `total_tokens` a caller's budget
    is spent from: a method that discarded it left the agent's `tokens_used`
    with nothing to read, so `MAX_TURN_TOKENS` could never bite however long a
    turn ran. One return shape rather than a second method, so a later caller
    cannot reach the model without also being handed its bill.

    On a RETRIED call `usage` is the second answer's, never the first's: the
    first attempt's text was discarded unparsed, and pairing it with the value
    would describe two different requests in one object. Pinned by
    `test_gateway_client.py::test_the_usage_of_a_retried_call_is_the_second_answers_not_the_first`.

    Frozen for `ModelAnswer`'s reason: a caller may not rewrite the counts a
    turn is billed and capped by.
    """

    value: T
    usage: ModelAnswer


class ModelClient:
    """One httpx client, two call shapes, typed refusals.

    Mirrors `helpdesk.rag.gateway.RedactionClient` deliberately: pre-checks
    first, one `httpx.AsyncClient` carrying both timeout halves, an injectable
    transport so the suite opens no socket, and a `_parsed` mapping statuses
    onto the typed codes.
    """

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
        """Build the client from loaded configuration.

        The key map is already parsed and already validated by `Settings`, so
        a malformed pair has failed at load time rather than here.
        """
        return cls(base_url=settings.gateway_url, api_keys=settings.gateway_api_keys)

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
        """One non-streaming turn, as a `ModelAnswer`.

        Every pre-check runs before the body is built, so a refusal costs no
        round trip and never puts the prompt on the wire
        (`test_gateway_client.py::test_a_session_id_one_character_over_the_cap_is_refused_before_any_request`).
        """
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
        """One turn constrained to `schema_model`, validated here as well.

        The schema is the one Pydantic generates, so the shape asked for and
        the shape validated against cannot drift apart. An answer that is not
        JSON, or is JSON the model refuses, is retried EXACTLY once (SPEC
        §10.1 step 2): both failures take the same path, because a well-formed
        object with the wrong fields is the far more common bad generation.
        After the second failure the turn is `ModelUnavailableError`. Pinned by
        `test_gateway_client.py::test_complete_json_retries_exactly_once_and_returns_the_second_answer`,
        `::test_complete_json_retries_once_when_valid_json_fails_the_models_validation`
        and `::test_complete_json_gives_up_after_the_second_invalid_answer`.

        The answer comes back as a `StructuredAnswer`: the validated value
        beside the `ModelAnswer` of the attempt that produced it, so a caller
        spending a token budget reads the gateway's own count rather than
        estimating one. Pinned by
        `::test_complete_json_returns_the_validated_value_beside_the_calls_own_usage`.
        """
        headers = self._headers(tenant_slug, session_id, traceparent)
        body = self._body(messages, alias, max_tokens, temperature)
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": _checked_schema_name(schema_model.__name__),
                "schema": schema_model.model_json_schema(),
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
        """The five headers, every client-supplied part of them pre-checked.

        `X-Session` is added only when a session id was given, so the console
        never reads a present-but-empty header as a session
        (`test_gateway_client.py::test_the_session_header_is_absent_when_no_session_is_given`).
        """
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
        """The OpenAI-shaped request body, naming the alias and nothing else.

        `stream` is always `false` here (decision B); the two optional fields
        are omitted rather than sent as null, so the gateway applies its own
        defaults instead of being told "no limit" explicitly.
        """
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
            # The exception's own message carries the URL, so it stays in the
            # chained cause where a log line reads it, and never in ours.
            raise ModelUnavailableError("the model gateway could not be reached") from exc

    def _parsed(self, response: httpx.Response, alias: ModelAlias) -> ModelAnswer:
        """Map a response onto the typed codes, or onto a `ModelAnswer`.

        401 and 403 are one answer rather than two: both mean the key this
        process holds cannot call for this tenant. Every other non-200 —
        including 404 for an alias the gateway does not route and 429 — is
        `INTERNAL`, because none of them is something the caller's arguments
        can fix, and a 200 whose body cannot be read is the same answer. The
        whole table is pinned by
        `test_gateway_client.py::test_a_gateway_status_maps_to_its_typed_error`.
        """
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
        )


def _checked_schema_name(name: str) -> str:
    """The accepted schema name, or a typed refusal before any request.

    Two gates, and the pattern is the narrower one for a name containing
    anything outside `[a-zA-Z0-9_-]`; the cap decides length alone. Both are
    checked because a 129-character name of legal characters passes the
    pattern's own `{1,128}` only by the cap's comparison being run first.
    """
    if len(name) > MAX_SCHEMA_NAME_LENGTH:
        raise ModelRequestError(
            f"a schema name longer than {MAX_SCHEMA_NAME_LENGTH} characters cannot be sent: "
            f"{len(name)}"
        )
    if SCHEMA_NAME_PATTERN.fullmatch(name) is None:
        raise ModelRequestError(f"a schema name must match {SCHEMA_NAME_PATTERN.pattern}")
    return name
