"""An earlier change §6: the model client in `helpdesk/gateway/client.py`, the only
place in this repo that calls the gateway's chat endpoint.

Expected implementation surface (the implementer must provide exactly this):

``helpdesk.gateway.client``
    ``class ModelAlias(StrEnum)`` — exactly two members, ``"agent-default"``
        and ``"agent-strong"`` (the design, plan decision E).
    ``MAX_SCHEMA_NAME_LENGTH: int = 128`` — a COPY of the console's
        ``providers/base.py::MAX_TOOL_NAME_LENGTH`` (plan §5).
    ``SCHEMA_NAME_PATTERN: re.Pattern[str]`` — mirrors the console's
        ``TOOL_NAME_PATTERN``, matched with ``re.fullmatch``.
    ``DATA_CLASS: str = "confidential"`` (plan decision F).
    ``CONNECT_TIMEOUT_SECONDS: float = 5.0`` /
        ``READ_TIMEOUT_SECONDS: float = 60.0`` — the read half is wider than
        the redaction client's 30.0 because a generation is slower than a
        scan; both are asserted on the CONSTRUCTED client.
    ``ModelForbiddenError(ToolError)``   — ``code = ErrorCode.FORBIDDEN``
    ``ModelRequestError(ToolError)``     — ``code = ErrorCode.VALIDATION``
    ``ModelUnavailableError(ToolError)`` — ``code = ErrorCode.INTERNAL``
    ``@dataclass(frozen=True) class ModelAnswer`` — ``text``, ``alias``,
        ``prompt_tokens``, ``completion_tokens``, ``total_tokens``,
        ``gateway_model``, ``gateway_trace_id``.
    ``class ModelClient``
        ``__init__(self, *, base_url: str, api_keys: Mapping[str, str],
                   transport: httpx.AsyncBaseTransport | None = None) -> None``
        ``client: httpx.AsyncClient`` — public, so both timeout halves are
            asserted on IT and not on the constants.
        ``async def complete(self, messages, *, alias, tenant_slug,
                             session_id=None, traceparent=None,
                             max_tokens=None, temperature=None) -> ModelAnswer``
        ``async def complete_json(self, schema_model, messages, *, <same
            keywords>) -> StructuredAnswer[<schema_model>]``

An earlier change changes exactly one return shape (decision A). ``complete_json``
answers a ``StructuredAnswer`` rather than the bare model:

    ``@dataclass(frozen=True) class StructuredAnswer[M: BaseModel]`` —
        ``value: M`` (the validated model) and ``usage: ModelAnswer`` (the
        answer ``complete`` would have returned for the SAME request, so the
        turn can add its ``total_tokens`` to a budget). On a retried call the
        usage is the SECOND answer's: the first was discarded unparsed, and
        billing a turn for output nobody read would be a number no operator
        can reconcile.
        ``async def aclose(self) -> None``
        ``@classmethod from_settings(cls, settings: Settings) -> ModelClient``

Two contract points these cases fix, so the implementer does not have to
guess: a model output that is still unusable AFTER the one retry is
``ModelUnavailableError`` (``INTERNAL``) — the caller's input was fine, the
model's output was not, and a caller cannot fix it by editing arguments —
and the request goes to ``POST {base_url}/chat/completions``.

No socket is ever opened: every case drives `httpx.MockTransport`. The async
calls run under a single `asyncio.run(...)` per case, because the repo
installs no event-loop plugin (see `tests/conftest.py`). Nothing here needs
Postgres, Redis or Qdrant.
"""

import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, NamedTuple

import httpx
import pytest
from pydantic import BaseModel, ConfigDict, create_model

from helpdesk.agent_api.services.model_gateway.client import (
    CONNECT_TIMEOUT_SECONDS,
    DATA_CLASS,
    MAX_SCHEMA_NAME_LENGTH,
    READ_TIMEOUT_SECONDS,
    SCHEMA_NAME_PATTERN,
    ModelAlias,
    ModelAnswer,
    ModelClient,
    ModelForbiddenError,
    ModelRequestError,
    ModelUnavailableError,
    StructuredAnswer,
)
from helpdesk.agent_api.services.model_gateway.traceparent import TRACEPARENT_PATTERN
from helpdesk.shared.errors import ErrorCode, ToolError

BASE_URL = "http://gateway.invalid:8080"
TENANT = "nordlicht"
OTHER_TENANT = "cuan"
# Obviously fake, tests only (a project rule/8): these are not credentials.
API_KEYS: dict[str, str] = {TENANT: "fake-key-nordlicht", OTHER_TENANT: "fake-key-cuan"}

SESSION_ID = "session-0000-fake"
CALLER_TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"

MESSAGES: list[dict[str, str]] = [
    {"role": "system", "content": "You answer access questions."},
    {"role": "user", "content": "Can I get read access to the payroll report?"},
]

ANSWER_TEXT = "You already hold that entitlement; no request is needed."
GATEWAY_MODEL = "fake-provider/fake-chat-1"
GATEWAY_TRACE_ID = "trace-0000-fake"

# A body that carries a provider name AND the internal URL, so a client that
# builds its message from `response.text` fails the message rows below.
GATEWAY_WORDING = (
    "upstream fake-provider/fake-chat-1 refused at http://gateway.invalid:8080/chat/completions"
)


class Decision(BaseModel):
    """The structured answer `complete_json` is asked for in these cases.

    Forbids extra keys so its schema meets strict mode, which the client now
    checks before sending."""

    model_config = ConfigDict(extra="forbid")

    decision: str
    reason: str


VALID_DECISION: dict[str, str] = {"decision": "grant", "reason": "already entitled"}

STRICT = ConfigDict(extra="forbid")
"""The config a `create_model` schema needs to pass the client's strict-mode
pre-check, so the schema-name cases are decided by the name alone."""


def _ok_body(text: str = ANSWER_TEXT, model: str = "agent-strong") -> dict[str, Any]:
    """The gateway's own chat-completions response shape.

    `model` defaults to the OTHER alias on purpose: the answer's alias must
    be the one the CALLER named, never the one echoed in the body.
    """
    return {
        "id": "chatcmpl-0000-fake",
        "object": "chat.completion",
        "created": 1_700_000_000,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text, "tool_calls": None},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
    }


def _body_with_tokens(text: str, total_tokens: int) -> dict[str, Any]:
    """The same response body with a chosen `usage.total_tokens`.

    The two attempts of a retried structured call are billed differently on
    purpose, so "which answer's usage came back" is decidable from the number
    alone rather than from an ordering the test cannot see.
    """
    body = _ok_body(text=text)
    body["usage"] = {
        "prompt_tokens": 1,
        "completion_tokens": total_tokens - 1,
        "total_tokens": total_tokens,
    }
    return body


FIRST_ATTEMPT_TOKENS = 5
SECOND_ATTEMPT_TOKENS = 41
"""Two different bills for the two attempts of one retried call, and neither
is `_ok_body`'s 18: a client returning any other answer's usage names a number
no row here can produce by accident."""


_OK_HEADERS: dict[str, str] = {
    "X-Gateway-Provider": "fake-provider",
    "X-Gateway-Region": "eu-central-1",
    "X-Gateway-Model": GATEWAY_MODEL,
    "X-Gateway-Cache": "miss",
    "X-Gateway-Redacted": "false",
    "X-Gateway-Trace-Id": GATEWAY_TRACE_ID,
}


def _ok_response(body: dict[str, Any] | None = None) -> httpx.Response:
    return httpx.Response(200, json=_ok_body() if body is None else body, headers=_OK_HEADERS)


class _Recorder:
    """A MockTransport handler that records every request it is handed."""

    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.response


class _Sequence:
    """A MockTransport handler answering a fixed sequence, one per request.

    A request beyond the sequence fails the test rather than repeating the
    last answer, so "the transport saw two requests, not three" is caught at
    the moment of the third call and not only by a count afterwards.
    """

    def __init__(self, responses: Sequence[httpx.Response]) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if len(self.requests) > len(self.responses):
            pytest.fail(
                f"request {len(self.requests)} was built; the sequence has {len(self.responses)}"
            )
        return self.responses[len(self.requests) - 1]


def _must_not_be_called(request: httpx.Request) -> httpx.Response:
    """Fails the test if the client builds a request at all."""
    pytest.fail(f"a request was built before the pre-check refused it: {request.url}")


Handler = Callable[[httpx.Request], httpx.Response]


def _client(handler: Handler, *, api_keys: Mapping[str, str] | None = None) -> ModelClient:
    transport = httpx.MockTransport(handler)
    return ModelClient(
        base_url=BASE_URL,
        api_keys=API_KEYS if api_keys is None else api_keys,
        transport=transport,
    )


def _complete(
    client: ModelClient,
    *,
    alias: ModelAlias = ModelAlias.AGENT_DEFAULT,
    tenant: str = TENANT,
    session_id: str | None = None,
    traceparent: str | None = None,
) -> ModelAnswer:
    """Drive one `complete` call and close the client, in a single event loop."""

    async def scenario() -> ModelAnswer:
        try:
            return await client.complete(
                MESSAGES,
                alias=alias,
                tenant_slug=tenant,
                session_id=session_id,
                traceparent=traceparent,
            )
        finally:
            await client.aclose()

    return asyncio.run(scenario())


def _complete_json(
    client: ModelClient,
    schema_model: type[BaseModel],
    *,
    alias: ModelAlias = ModelAlias.AGENT_DEFAULT,
    tenant: str = TENANT,
) -> StructuredAnswer[BaseModel]:
    """Drive one `complete_json` call and close the client, in one event loop."""

    async def scenario() -> StructuredAnswer[BaseModel]:
        try:
            return await client.complete_json(
                schema_model, MESSAGES, alias=alias, tenant_slug=tenant
            )
        finally:
            await client.aclose()

    return asyncio.run(scenario())


# --- constants and the closed alias set ---------------------


def test_the_model_client_constants_are_exactly_the_planned_values() -> None:
    """Catches a cap, a timeout or a data class set to any other number or
    string — in particular a `DATA_CLASS` copied from the redaction client's
    `"internal"`, which would route chat under a wider class than the design
    fixes, and a `MAX_SCHEMA_NAME_LENGTH` that drifts from the console's
    `MAX_TOOL_NAME_LENGTH` and so presents as a puzzling 422 rather than a
    local refusal."""
    assert MAX_SCHEMA_NAME_LENGTH == 128
    assert DATA_CLASS == "confidential"
    assert CONNECT_TIMEOUT_SECONDS == 5.0
    assert READ_TIMEOUT_SECONDS == 60.0


def test_each_model_error_carries_its_spec_code() -> None:
    """Catches an implementation that maps every gateway failure onto one
    code (all-INTERNAL, or all-VALIDATION): the tool boundary would then tell
    a caller "try again" for a rejected key and "fix your input" for an
    unreachable provider."""
    assert ModelForbiddenError.code is ErrorCode.FORBIDDEN
    assert ModelRequestError.code is ErrorCode.VALIDATION
    assert ModelUnavailableError.code is ErrorCode.INTERNAL


def test_model_alias_has_exactly_the_two_spec_members() -> None:
    """Catches a third member (a provider model smuggled in as an alias), a
    renamed member, and an alias whose VALUE differs from its name's intent —
    the value is what goes into the request's `model` field, so
    `AGENT_DEFAULT = "agent_default"` would be a 404 from the gateway that no
    local test would otherwise see."""
    assert [member.value for member in ModelAlias] == ["agent-default", "agent-strong"]
    assert ModelAlias.AGENT_DEFAULT.value == "agent-default"
    assert ModelAlias.AGENT_STRONG.value == "agent-strong"
    assert str(ModelAlias.AGENT_DEFAULT) == "agent-default"


def test_the_constructed_client_carries_both_timeout_halves() -> None:
    """Catches a client built with a single scalar timeout, with only a
    connect timeout, or with httpx's default: a gateway that accepts the
    connection and then never answers would hang an agent turn for ever.
    Asserted on the constructed client, not on the constants, so a
    constant that is declared but never passed to httpx still fails."""
    client = _client(_Recorder(_ok_response()))
    try:
        assert client.client.timeout.connect == CONNECT_TIMEOUT_SECONDS
        assert client.client.timeout.read == READ_TIMEOUT_SECONDS
    finally:
        asyncio.run(client.aclose())


# --- the request shape --------------------------------------------------------


def test_complete_posts_the_documented_path_headers_and_body() -> None:
    """Catches a client that sends the turn to the wrong path, omits
    X-Tenant or X-Data-Class, authenticates with a key belonging to a
    DIFFERENT tenant than the one in the header, or names the model by
    anything other than the alias's wire value. The console answers 403 to a
    key/tenant mismatch and 404 to an unknown model, so each defect would
    otherwise surface only as a remote denial nobody can explain locally."""
    recorder = _Recorder(_ok_response())
    client = _client(recorder)

    _complete(client, alias=ModelAlias.AGENT_STRONG, session_id=SESSION_ID)

    assert len(recorder.requests) == 1
    request = recorder.requests[0]
    assert request.method == "POST"
    assert str(request.url) == f"{BASE_URL}/chat/completions"
    assert request.headers["Authorization"] == f"Bearer {API_KEYS[TENANT]}"
    assert request.headers["X-Tenant"] == TENANT
    assert request.headers["X-Data-Class"] == "confidential"
    assert request.headers["X-Session"] == SESSION_ID
    assert TRACEPARENT_PATTERN.fullmatch(request.headers["traceparent"]) is not None
    body = json.loads(request.content)
    assert body["model"] == "agent-strong"
    assert body["messages"] == MESSAGES
    assert body["stream"] is False


def test_the_session_header_is_absent_when_no_session_is_given() -> None:
    """Catches a client that always sets `X-Session`, sending an empty value
    when the caller has no session: the console reads the header's PRESENCE,
    so an empty one files the turn under a session that does not exist rather
    than under none. Absent, not empty, is the whole claim."""
    recorder = _Recorder(_ok_response())
    client = _client(recorder)

    _complete(client)

    assert "X-Session" not in recorder.requests[0].headers


def test_a_valid_caller_traceparent_is_propagated_verbatim() -> None:
    """Catches a client that mints its own header regardless of the caller's
    (plan decision C's option ii): the agent turn's trace and the gateway's
    would be two unconnected traces, and the design's single span across both
    services would never form."""
    recorder = _Recorder(_ok_response())
    client = _client(recorder)

    _complete(client, traceparent=CALLER_TRACEPARENT)

    assert recorder.requests[0].headers["traceparent"] == CALLER_TRACEPARENT


def test_the_other_tenants_key_is_used_for_the_other_tenant() -> None:
    """Catches a client that picks the first configured key (or a single
    module-level key) instead of looking the caller's tenant up in the map,
    which is the whole point of a design note's per-tenant keys. The pair is
    asserted together because the console compares them."""
    recorder = _Recorder(_ok_response())
    client = _client(recorder)

    _complete(client, tenant=OTHER_TENANT)

    request = recorder.requests[0]
    assert request.headers["Authorization"] == f"Bearer {API_KEYS[OTHER_TENANT]}"
    assert request.headers["X-Tenant"] == OTHER_TENANT


# --- the answer ---------------------------------------------------------------


def test_complete_maps_the_body_and_the_gateway_headers_onto_the_answer() -> None:
    """Catches four separate mistakes in one shape: reading the alias out of
    the response BODY (the fixture body says `agent-strong` while the caller
    named `agent-default`), dropping the usage counts an operator bills and
    caps turns on, and losing `X-Gateway-Model` / `X-Gateway-Trace-Id`, which
    are the only handles that tie a turn here to a row in the console's log."""
    recorder = _Recorder(_ok_response())
    client = _client(recorder)

    answer = _complete(client, alias=ModelAlias.AGENT_DEFAULT)

    assert answer.text == ANSWER_TEXT
    assert answer.alias is ModelAlias.AGENT_DEFAULT
    assert answer.prompt_tokens == 11
    assert answer.completion_tokens == 7
    assert answer.total_tokens == 18
    assert answer.gateway_model == GATEWAY_MODEL
    assert answer.gateway_trace_id == GATEWAY_TRACE_ID


# --- the status table ---------------------------------------------------------


class _Row(NamedTuple):
    name: str
    status: int
    text: str | None
    body: dict[str, Any] | None
    expected: type[ToolError]
    code: ErrorCode


_STATUS_ROWS: tuple[_Row, ...] = (
    _Row("401", 401, GATEWAY_WORDING, None, ModelForbiddenError, ErrorCode.FORBIDDEN),
    _Row("403", 403, GATEWAY_WORDING, None, ModelForbiddenError, ErrorCode.FORBIDDEN),
    _Row("422", 422, GATEWAY_WORDING, None, ModelRequestError, ErrorCode.VALIDATION),
    _Row("404", 404, GATEWAY_WORDING, None, ModelUnavailableError, ErrorCode.INTERNAL),
    _Row("429", 429, GATEWAY_WORDING, None, ModelUnavailableError, ErrorCode.INTERNAL),
    _Row("451", 451, GATEWAY_WORDING, None, ModelUnavailableError, ErrorCode.INTERNAL),
    _Row("502", 502, GATEWAY_WORDING, None, ModelUnavailableError, ErrorCode.INTERNAL),
    _Row("503", 503, GATEWAY_WORDING, None, ModelUnavailableError, ErrorCode.INTERNAL),
    _Row("unmapped-418", 418, GATEWAY_WORDING, None, ModelUnavailableError, ErrorCode.INTERNAL),
    _Row("non-json-body", 200, GATEWAY_WORDING, None, ModelUnavailableError, ErrorCode.INTERNAL),
    _Row(
        "body-missing-choices",
        200,
        None,
        {"id": "chatcmpl-0000-fake", "error": GATEWAY_WORDING, "usage": {}},
        ModelUnavailableError,
        ErrorCode.INTERNAL,
    ),
)


def _response_for(row: _Row) -> httpx.Response:
    if row.body is not None:
        return httpx.Response(row.status, json=row.body, headers=_OK_HEADERS)
    return httpx.Response(row.status, text=row.text or "", headers=_OK_HEADERS)


@pytest.mark.parametrize("row", _STATUS_ROWS, ids=[row.name for row in _STATUS_ROWS])
def test_a_gateway_status_maps_to_its_typed_error(row: _Row) -> None:
    """Catches a client that raises `httpx.HTTPStatusError` (an untyped error
    crossing the tool boundary), one that collapses 401/403 into the
    retryable INTERNAL class, one that reads a 404 (unknown alias) or a 429
    as the caller's malformed input, and one that trusts a 200 without
    looking at the body — the last two rows are 200s whose body cannot be
    turned into an answer, which is the shape a `response.json()["choices"]`
    with no guard raises `KeyError` on."""
    client = _client(_Recorder(_response_for(row)))

    with pytest.raises(row.expected) as excinfo:
        _complete(client)

    assert excinfo.value.code is row.code


@pytest.mark.parametrize("row", _STATUS_ROWS, ids=[row.name for row in _STATUS_ROWS])
def test_no_gateway_wording_url_or_provider_name_reaches_the_message(row: _Row) -> None:
    """Catches a client that builds its message from `response.text` or from
    the request URL. The gateway's body can quote the prompt it was given —
    the one string on this path carrying confidential company data — and it
    names the provider and model that served the turn, which a project rule
    keeps out of this repo entirely."""
    client = _client(_Recorder(_response_for(row)))

    with pytest.raises(ToolError) as excinfo:
        _complete(client)

    message = str(excinfo.value)
    assert GATEWAY_WORDING not in message
    assert "fake-provider" not in message
    assert "fake-chat-1" not in message
    assert BASE_URL not in message
    assert "gateway.invalid" not in message
    assert "/chat/completions" not in message


# --- pre-checks, all before any request is built -------------------------------


def test_a_session_id_of_exactly_the_slug_cap_is_accepted() -> None:
    """Catches an exclusive comparison (`len(session_id) < SLUG_LENGTH`),
    which would refuse the longest session id `check_slug` accepts and make
    those turns un-runnable for no reason. The 64-character value is a valid
    slug shape, so `check_slug` is the only gate it meets."""
    recorder = _Recorder(_ok_response())
    client = _client(recorder)
    session_id = "a" * 64

    _complete(client, session_id=session_id)

    assert len(session_id) == 64
    assert recorder.requests[0].headers["X-Session"] == session_id


def test_a_session_id_one_character_over_the_cap_is_refused_before_any_request() -> None:
    """Catches a missing pre-check on a value that lands in a HEADER: an
    unbounded `X-Session` is one of the unsized stores a project rule names,
    and an over-long one would be refused by the console after the prompt was
    already on the wire. The transport fails the test if it is reached, so a
    check placed after the request is built does not pass this row."""
    client = _client(_must_not_be_called)
    session_id = "a" * 65

    with pytest.raises(ModelRequestError) as excinfo:
        _complete(client, session_id=session_id)

    assert len(session_id) == 65
    assert excinfo.value.code is ErrorCode.VALIDATION


def test_a_schema_name_of_exactly_the_cap_is_accepted() -> None:
    """Catches an exclusive comparison (`< MAX_SCHEMA_NAME_LENGTH`) on the
    response-format name, which would refuse the longest name the console
    accepts. 128 characters of `A` also matches `SCHEMA_NAME_PATTERN`, so
    only the length comparison can decide this row."""
    name = "A" * MAX_SCHEMA_NAME_LENGTH
    schema_model: type[BaseModel] = create_model(
        name, __config__=STRICT, decision=(str, ...), reason=(str, ...)
    )
    recorder = _Recorder(_ok_response(_ok_body(text=json.dumps(VALID_DECISION))))
    client = _client(recorder)

    result = _complete_json(client, schema_model)

    assert len(name) == 128
    assert len(recorder.requests) == 1
    assert (
        json.loads(recorder.requests[0].content)["response_format"]["json_schema"]["name"] == name
    )
    assert result.value.model_dump() == VALID_DECISION


def test_a_schema_name_one_character_over_the_cap_is_refused_before_any_request() -> None:
    """Catches a missing pre-check on the one field of the request body the
    console bounds itself: the oversized name would be posted and answered
    422, costing a round trip AND putting the prompt on the wire for a
    request that could never succeed."""
    name = "A" * (MAX_SCHEMA_NAME_LENGTH + 1)
    schema_model: type[BaseModel] = create_model(
        name, __config__=STRICT, decision=(str, ...), reason=(str, ...)
    )
    client = _client(_must_not_be_called)

    with pytest.raises(ModelRequestError) as excinfo:
        _complete_json(client, schema_model)

    assert len(name) == 129
    assert excinfo.value.code is ErrorCode.VALIDATION


@pytest.mark.parametrize(
    "name",
    ["Bad Name", "decision.model", "schema\n", "schema "],
    ids=["space", "dot", "trailing-newline", "trailing-space"],
)
def test_a_schema_name_outside_the_pattern_is_refused_before_any_request(name: str) -> None:
    """Catches a client that checks only the LENGTH of the schema name: every
    row here is well under 128, so the cap cannot decide any of them and only
    `SCHEMA_NAME_PATTERN` can. The last two also catch `re.match` in place of
    `re.fullmatch`, which accepts a name ending in a
    newline — a value that goes into a JSON body the console re-emits."""
    schema_model: type[BaseModel] = create_model(
        name, __config__=STRICT, decision=(str, ...), reason=(str, ...)
    )
    client = _client(_must_not_be_called)

    assert len(name) <= MAX_SCHEMA_NAME_LENGTH
    assert SCHEMA_NAME_PATTERN.fullmatch(name) is None
    with pytest.raises(ModelRequestError) as excinfo:
        _complete_json(client, schema_model)

    assert excinfo.value.code is ErrorCode.VALIDATION


def test_a_malformed_caller_traceparent_is_refused_before_any_request() -> None:
    """Catches a client that puts the caller's traceparent straight into a
    header: `...-01\\n` is a header-injection shape, and the console answers
    422 to a header it cannot parse, so the turn's prompt would be sent for a
    request that was never going to succeed. The transport fails the test if
    it is reached, so a check made while building the headers does not pass
    this row."""
    client = _client(_must_not_be_called)

    with pytest.raises(ModelRequestError) as excinfo:
        _complete(client, traceparent=CALLER_TRACEPARENT + "\n")

    assert excinfo.value.code is ErrorCode.VALIDATION


# --- structured output and its single retry -----------------------------------


def test_complete_json_sends_the_response_format_pydantic_generates() -> None:
    """Catches a client that sends `{"type": "json_object"}` (no schema at
    all, so the model is free to answer any shape), one that hand-writes a
    schema instead of asking Pydantic for it, and one that nests the schema
    under the wrong keys — the console validates this envelope, so any of the
    three is a 422 that no local test would otherwise see."""
    recorder = _Recorder(_ok_response(_ok_body(text=json.dumps(VALID_DECISION))))
    client = _client(recorder)

    result = _complete_json(client, Decision)

    body = json.loads(recorder.requests[0].content)
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "Decision",
            "schema": Decision.model_json_schema(),
            "strict": True,
        },
    }
    assert isinstance(result, StructuredAnswer)
    assert isinstance(result.value, Decision)
    assert result.value.decision == "grant"
    assert result.value.reason == "already entitled"


def test_complete_json_retries_exactly_once_and_returns_the_second_answer() -> None:
    """Catches a client that gives up on the first unparseable answer (SPEC
    §10.1 step 2's retry missing, so one malformed generation fails a turn a
    second call would have completed). The request COUNT is the assertion: a
    client that retries in a loop would also return the model here, and only
    the count of two tells the two implementations apart."""
    handler = _Sequence(
        [
            _ok_response(_ok_body(text="Sure! Here is the decision: {not json")),
            _ok_response(_ok_body(text=json.dumps(VALID_DECISION))),
        ]
    )
    client = _client(handler)

    result = _complete_json(client, Decision)

    assert len(handler.requests) == 2
    assert isinstance(result.value, Decision)
    assert result.value.decision == "grant"


def test_complete_json_retries_once_when_valid_json_fails_the_models_validation() -> None:
    """Catches a client that only catches `json.JSONDecodeError`: a
    well-formed object with the wrong fields parses fine and then fails
    `model_validate`, which is the FAR more common failure of a structured
    generation. Without this row a client that skips the retry on a
    `ValidationError` passes the invalid-JSON case above."""
    handler = _Sequence(
        [
            _ok_response(_ok_body(text=json.dumps({"verdict": "grant"}))),
            _ok_response(_ok_body(text=json.dumps(VALID_DECISION))),
        ]
    )
    client = _client(handler)

    result = _complete_json(client, Decision)

    assert len(handler.requests) == 2
    assert isinstance(result.value, Decision)
    assert result.value.reason == "already entitled"


def test_complete_json_gives_up_after_the_second_invalid_answer() -> None:
    """Catches an unbounded retry loop, which turns one badly behaved model
    into an open-ended spend and a hung turn — the `_Sequence` handler fails
    the test on the third request rather than answering it. Also catches a
    raw `ValidationError` or `JSONDecodeError` escaping to the caller, which
    the MCP boundary has no mapping for; the code is INTERNAL because the
    caller's arguments were fine and editing them cannot help."""
    handler = _Sequence(
        [
            _ok_response(_ok_body(text="{not json")),
            _ok_response(_ok_body(text="{still not json")),
        ]
    )
    client = _client(handler)

    with pytest.raises(ModelUnavailableError) as excinfo:
        _complete_json(client, Decision)

    assert len(handler.requests) == 2
    assert excinfo.value.code is ErrorCode.INTERNAL


def test_complete_json_returns_the_validated_value_beside_the_calls_own_usage() -> None:
    """An earlier change decision A. Catches a `complete_json` that returns the bare
    model and throws the `ModelAnswer` away, which is the shape this method
    had before: the agent's `tokens_used` would then have nothing to read for
    the extraction or for any planning round, and `MAX_TURN_TOKENS` could
    never bite however long a turn ran.

    The usage is asserted field by field against the body and the headers the
    transport answered — the same facts `complete` returns — so a client that
    fabricates a zeroed `ModelAnswer` beside the value fails here too."""
    recorder = _Recorder(_ok_response(_ok_body(text=json.dumps(VALID_DECISION))))
    client = _client(recorder)

    result = _complete_json(client, Decision, alias=ModelAlias.AGENT_STRONG)

    assert isinstance(result, StructuredAnswer)
    assert isinstance(result.value, Decision)
    assert result.value.model_dump() == VALID_DECISION
    assert isinstance(result.usage, ModelAnswer)
    assert result.usage.total_tokens == 18
    assert result.usage.prompt_tokens == 11
    assert result.usage.completion_tokens == 7
    assert result.usage.alias is ModelAlias.AGENT_STRONG
    assert result.usage.gateway_model == GATEWAY_MODEL
    assert result.usage.gateway_trace_id == GATEWAY_TRACE_ID
    assert result.usage.text == json.dumps(VALID_DECISION)


def test_the_usage_of_a_retried_call_is_the_second_answers_not_the_first() -> None:
    """Catches a client that keeps the first attempt's `ModelAnswer` and pairs
    it with the second attempt's value — the pair would then describe two
    different requests, and a turn's token counter would be reconciled against
    a bill nobody was charged.

    The two attempts carry different token counts and neither is the default
    18, so only the second answer's usage can produce the expected number; the
    request count keeps the single retry pinned at the same time."""
    handler = _Sequence(
        [
            _ok_response(_body_with_tokens("{not json", FIRST_ATTEMPT_TOKENS)),
            _ok_response(_body_with_tokens(json.dumps(VALID_DECISION), SECOND_ATTEMPT_TOKENS)),
        ]
    )
    client = _client(handler)

    result = _complete_json(client, Decision)

    assert len(handler.requests) == 2
    assert result.value.model_dump() == VALID_DECISION
    assert result.usage.total_tokens == SECOND_ATTEMPT_TOKENS
    assert result.usage.text == json.dumps(VALID_DECISION)


# --- an earlier change: strict structured outputs ----------------------------------------


class StrictDecision(BaseModel):
    """`Decision` as strict mode needs it: extra keys forbidden, no defaults.

    Local to the an earlier change cases so they do not lean on an earlier change §8's edit to
    `Decision`: the client refuses a non-strict schema before sending, so a
    case built on the old model would go red for the pre-check, not for the
    `strict` flag it means to pin.
    """

    model_config = ConfigDict(extra="forbid")

    decision: str
    reason: str


class NoteDecision(BaseModel):
    """A schema strict mode rejects: `note` is optional with a default.

    Pydantic leaves `note` out of `required` and emits `"default": null`, so
    the schema breaks two of the checker's rules while forbidding extra keys,
    which keeps the third rule out of play.
    """

    model_config = ConfigDict(extra="forbid")

    decision: str
    note: str | None = None


def test_complete_json_sends_strict_true_inside_json_schema() -> None:
    """Catches a client that sends a strict-mode-ready schema but never ASKS
    for strict mode, so the gateway only suggests the shape to the model; and
    one that sends the flag outside `json_schema` or as a string, which the
    `is True` and the exact key set both refuse."""
    recorder = _Recorder(_ok_response(_ok_body(text=json.dumps(VALID_DECISION))))
    client = _client(recorder)

    _complete_json(client, StrictDecision)

    body = json.loads(recorder.requests[0].content)
    json_schema = body["response_format"]["json_schema"]
    assert json_schema["strict"] is True
    assert set(json_schema) == {"name", "schema", "strict"}


def test_a_non_strict_schema_is_refused_before_any_request() -> None:
    """Catches a client with no strict-schema pre-check: the schema would be
    posted and the gateway would answer 400, after the turn's prompt was
    already on the wire. The transport fails the test if it is reached, so a
    check placed after the request is built does not pass this row."""
    client = _client(_must_not_be_called)

    with pytest.raises(ModelRequestError) as excinfo:
        _complete_json(client, NoteDecision)

    assert excinfo.value.code is ErrorCode.VALIDATION


# --- a project rule: no provider model name anywhere ----------------------------

_REPO_ROOT = Path(__file__).resolve().parents[5]
_SWEPT_TREES: tuple[Path, ...] = (_REPO_ROOT / "backend" / "helpdesk",)

_PROVIDER_PREFIXES: tuple[str, ...] = (
    "gpt-",
    "gpt4",
    "claude-3",
    "claude-4",
    "claude-sonnet",
    "claude-opus",
    "claude-haiku",
    "gemini-",
    "llama-3",
    "mistral-",
    "mixtral",
    "command-r",
    "deepseek-",
    "qwen",
    "grok-",
    "text-davinci",
    "text-embedding-ada",
)
"""Provider model ids, lowercased. A literal list, so a provider whose ids
match none of these would pass (plan §5 names that limit).

`BAAI/bge-small-en-v1.5` is deliberately NOT here: it is the local fastembed
dense model in `helpdesk/rag/embedding.py`, an argument to a library that
runs in this process, not a model named at the gateway. a project rule is about
gateway calls, which are named by alias.
"""

_LOCAL_EMBEDDING_MODEL_ID = "BAAI/bge-small-en-v1.5"


def _swept_files() -> list[Path]:
    return sorted(path for tree in _SWEPT_TREES for path in tree.rglob("*.py"))


def test_no_provider_model_name_appears_in_the_repo() -> None:
    """Catches the exact regression a project rule forbids: a provider model id
    pasted into a call, a constant or a docstring under
    `packages/helpdesk/helpdesk/` or `services/`, which is how "we only use
    aliases" quietly stops being true. Scoped to those two trees, so a plan
    file, a test fixture or an ADR discussing a provider is not swept."""
    offenders = [
        (path, prefix)
        for path in _swept_files()
        for prefix in _PROVIDER_PREFIXES
        if prefix in path.read_text().lower()
    ]

    assert offenders == []


def test_the_provider_sweep_reads_real_source_and_spares_the_local_embedding_model() -> None:
    """Guard against a vacuous sweep: a wrong root, a `glob` that never
    recurses or a tree that moved would make the test above pass over zero
    files and prove nothing. It also pins the one deliberate exclusion — the
    fastembed dense model id IS in the swept text, and stays clean only
    because no prefix matches it, so a future prefix like `bge-` would turn
    this into a false alarm here rather than a puzzling red elsewhere."""
    files = _swept_files()
    embedding = (
        _REPO_ROOT / "backend" / "helpdesk" / "mcp_server" / "services" / "rag" / "embedding.py"
    )

    assert len(files) > 20
    assert embedding in files
    assert _LOCAL_EMBEDDING_MODEL_ID in embedding.read_text()
    assert not any(prefix in _LOCAL_EMBEDDING_MODEL_ID.lower() for prefix in _PROVIDER_PREFIXES)
