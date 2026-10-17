"""The redaction client in `helpdesk/rag/gateway.py`, plus the
three `Settings` fields it is configured from.

Expected implementation surface (the implementer must provide exactly this):

``helpdesk.rag.gateway``
    ``MAX_REDACTION_CHARS: int = 16_384`` — a COPY of the console's
        ``opsconsole.redaction.MAX_REDACTION_INPUT_CHARS``.
    ``CONNECT_TIMEOUT_SECONDS: float = 5.0`` / ``READ_TIMEOUT_SECONDS: float = 30.0``
    ``DATA_CLASS: str = "internal"`` — the ``X-Data-Class`` header value.
    ``GatewayForbiddenError(ToolError)``   — ``code = ErrorCode.FORBIDDEN``
    ``GatewayRequestError(ToolError)``     — ``code = ErrorCode.VALIDATION``
    ``GatewayUnavailableError(ToolError)`` — ``code = ErrorCode.INTERNAL``
    ``@dataclass(frozen=True) class RedactedDocument`` — ``text: str``,
        ``redaction_map_id: str``.
    ``class RedactionClient``
        ``__init__(self, *, base_url: str, api_keys: Mapping[str, str],
                   transport: httpx.AsyncBaseTransport | None = None) -> None``
        ``client: httpx.AsyncClient`` — the constructed client, public, so both
            timeout halves are asserted on IT and not on the constants
.
        ``async def redact_document(self, text: str, tenant_slug: str) -> RedactedDocument``
        ``async def aclose(self) -> None``
        ``@classmethod from_settings(cls, settings: Settings) -> RedactionClient``

``helpdesk.core.settings.Settings`` gains
    ``gateway_url: str`` (required),
    ``gateway_api_keys: dict[str, str]`` parsed from ``"tenant:key,tenant:key"``,
        default ``{}``, a malformed pair raising a ``ValidationError`` naming
        the 1-BASED position of the bad pair,
    ``embedding_cache_dir: pathlib.Path``, default ``Path(".cache/fastembed")``.

The settings rows live here rather than in `test_settings.py` because the
parse they pin only exists to feed this client.

No socket is ever opened: every case drives `httpx.MockTransport`. The async
calls run under a single `asyncio.run(...)` per case, because the repo
installs no event-loop plugin (see `tests/conftest.py`).
"""

import asyncio
import json
import pathlib
from collections.abc import Callable, Mapping

import httpx
import pytest
from pydantic import ValidationError

from helpdesk.mcp_server.services.rag.redaction_client import (
    CONNECT_TIMEOUT_SECONDS,
    DATA_CLASS,
    MAX_REDACTION_CHARS,
    READ_TIMEOUT_SECONDS,
    GatewayForbiddenError,
    GatewayRequestError,
    GatewayUnavailableError,
    RedactedDocument,
    RedactionClient,
)
from helpdesk.shared.core.config import Settings
from helpdesk.shared.core.errors import ErrorCode, ToolError

BASE_URL = "http://gateway.invalid:8080"
TENANT = "nordlicht"
OTHER_TENANT = "cuan"
# Obviously fake, tests only (no real keys): these are not credentials.
API_KEYS: dict[str, str] = {TENANT: "fake-key-nordlicht", OTHER_TENANT: "fake-key-cuan"}

DOCUMENT = "Contact Erika Musterfrau at erika@nordlicht.example for badge access.\n"
REDACTED = "Contact [PERSON_1] at [EMAIL_1] for badge access.\n"
MAP_ID = "map-0000-fake"

# A body and a URL fragment that must never surface in a caller-visible message.
GATEWAY_WORDING = "opsconsole redaction backend exploded while building map 42"


def _ok_body(text: str = REDACTED) -> dict[str, object]:
    """The console's own /redact response shape."""
    return {
        "text": text,
        "messages": None,
        "data_class": "internal",
        "redaction_map_id": MAP_ID,
    }


class _Recorder:
    """A MockTransport handler that records every request it is handed."""

    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.response


def _must_not_be_called(request: httpx.Request) -> httpx.Response:
    """Fails the test if the client builds a request at all."""
    pytest.fail(f"a request was built before the pre-check refused it: {request.url}")


Handler = Callable[[httpx.Request], httpx.Response]


def _client(
    handler: Handler,
    *,
    api_keys: Mapping[str, str] | None = None,
) -> RedactionClient:
    transport = httpx.MockTransport(handler)
    return RedactionClient(
        base_url=BASE_URL,
        api_keys=API_KEYS if api_keys is None else api_keys,
        transport=transport,
    )


def _redact(client: RedactionClient, text: str, tenant: str = TENANT) -> RedactedDocument:
    """Drive one call and close the client, in a single event loop."""

    async def scenario() -> RedactedDocument:
        try:
            return await client.redact_document(text, tenant)
        finally:
            await client.aclose()

    return asyncio.run(scenario())


# --- constants ----------------------------------------------------------------


def test_the_gateway_constants_are_exactly_the_planned_values() -> None:
    """Catches a cap, a timeout or a data class set to any other number or
    string — in particular a MAX_REDACTION_CHARS that drifts from the
    console's MAX_REDACTION_INPUT_CHARS, which would present as a puzzling
    422 on the largest corpus file rather than as a local refusal."""
    assert MAX_REDACTION_CHARS == 16_384
    assert CONNECT_TIMEOUT_SECONDS == 5.0
    assert READ_TIMEOUT_SECONDS == 30.0
    assert DATA_CLASS == "internal"


def test_each_gateway_error_carries_its_spec_code() -> None:
    """Catches an implementation that maps every gateway failure onto one
    code (all-INTERNAL, or all-VALIDATION): the tool boundary would then
    tell a caller "try again" for a permission problem and vice versa."""
    assert GatewayForbiddenError.code is ErrorCode.FORBIDDEN
    assert GatewayRequestError.code is ErrorCode.VALIDATION
    assert GatewayUnavailableError.code is ErrorCode.INTERNAL


# --- the request shape --------------------------------------------------------


def test_redact_document_posts_the_documented_path_headers_and_body() -> None:
    """Catches a client that sends the document to the wrong path, omits
    X-Tenant or X-Data-Class, or authenticates with a key belonging to a
    DIFFERENT tenant than the one in the header — the console answers 403 to
    that mismatch, so the defect would otherwise only show up as a remote
    denial nobody can explain locally."""
    recorder = _Recorder(httpx.Response(200, json=_ok_body()))
    client = _client(recorder)

    _redact(client, DOCUMENT)

    assert len(recorder.requests) == 1
    request = recorder.requests[0]
    assert request.method == "POST"
    assert str(request.url) == f"{BASE_URL}/redact"
    assert request.headers["Authorization"] == f"Bearer {API_KEYS[TENANT]}"
    assert request.headers["X-Tenant"] == TENANT
    assert request.headers["X-Data-Class"] == "internal"
    assert json.loads(request.content) == {"text": DOCUMENT, "data_class": "internal"}


def test_the_other_tenants_key_is_used_for_the_other_tenant() -> None:
    """Catches a client that picks the first configured key (or a single
    module-level key) instead of looking the caller's tenant up in the map,
    which is the whole point of per-tenant keys."""
    recorder = _Recorder(httpx.Response(200, json=_ok_body()))
    client = _client(recorder)

    _redact(client, DOCUMENT, tenant=OTHER_TENANT)

    request = recorder.requests[0]
    assert request.headers["Authorization"] == f"Bearer {API_KEYS[OTHER_TENANT]}"
    assert request.headers["X-Tenant"] == OTHER_TENANT


def test_the_redacted_text_and_the_map_id_are_both_returned() -> None:
    """Catches a client that returns only the redacted string and drops
    `redaction_map_id`: the map id is the only handle an auditor has to
    re-identify a chunk, so losing it makes the stored chunk unreversible
    while everything still looks green."""
    recorder = _Recorder(httpx.Response(200, json=_ok_body()))
    client = _client(recorder)

    result = _redact(client, DOCUMENT)

    assert result.text == REDACTED
    assert result.redaction_map_id == MAP_ID


def test_the_constructed_client_carries_both_timeout_halves() -> None:
    """Catches a client built with a single scalar timeout, with only a
    connect timeout, or with httpx's default: a gateway that accepts the
    connection and then never answers would hang ingestion for ever.
    Asserted on the constructed client, not on the constants, so a
    constant that is declared but never passed to httpx still fails."""
    client = _client(_Recorder(httpx.Response(200, json=_ok_body())))
    try:
        assert client.client.timeout.connect == CONNECT_TIMEOUT_SECONDS
        assert client.client.timeout.read == READ_TIMEOUT_SECONDS
    finally:
        asyncio.run(client.aclose())


# --- status mapping -----------------------------------------------------------


_STATUS_ROWS: tuple[tuple[str, int, type[ToolError], ErrorCode], ...] = (
    ("401", 401, GatewayForbiddenError, ErrorCode.FORBIDDEN),
    ("403", 403, GatewayForbiddenError, ErrorCode.FORBIDDEN),
    ("422", 422, GatewayRequestError, ErrorCode.VALIDATION),
    ("502", 502, GatewayUnavailableError, ErrorCode.INTERNAL),
    ("500", 500, GatewayUnavailableError, ErrorCode.INTERNAL),
    ("503", 503, GatewayUnavailableError, ErrorCode.INTERNAL),
)


@pytest.mark.parametrize(
    ("status", "expected", "code"),
    [(status, expected, code) for _, status, expected, code in _STATUS_ROWS],
    ids=[name for name, _, _, _ in _STATUS_ROWS],
)
def test_a_gateway_status_maps_to_its_typed_error(
    status: int, expected: type[ToolError], code: ErrorCode
) -> None:
    """Catches a client that raises `httpx.HTTPStatusError` (an untyped error
    crossing the tool boundary), and one that collapses 401/403 into the
    retryable INTERNAL class or treats a 5xx as a caller mistake."""
    recorder = _Recorder(httpx.Response(status, text=GATEWAY_WORDING))
    client = _client(recorder)

    with pytest.raises(expected) as excinfo:
        _redact(client, DOCUMENT)

    assert excinfo.value.code is code


@pytest.mark.parametrize(
    "status",
    [status for _, status, _, _ in _STATUS_ROWS],
    ids=[name for name, _, _, _ in _STATUS_ROWS],
)
def test_no_gateway_wording_and_no_url_reaches_the_message(status: int) -> None:
    """Catches a client that builds its message from `response.text` or from
    the request URL: the gateway's body can quote the document it was asked
    to redact, so forwarding it would leak unredacted text and the internal
    host into a user-facing error."""
    recorder = _Recorder(httpx.Response(status, text=GATEWAY_WORDING))
    client = _client(recorder)

    with pytest.raises(ToolError) as excinfo:
        _redact(client, DOCUMENT)

    message = str(excinfo.value)
    assert GATEWAY_WORDING not in message
    assert "exploded" not in message
    assert BASE_URL not in message
    assert "gateway.invalid" not in message


@pytest.mark.parametrize(
    "exception",
    [
        httpx.ConnectError("connection refused"),
        httpx.ReadTimeout("read timed out"),
        httpx.TransportError("transport blew up"),
    ],
    ids=["connect-error", "read-timeout", "transport-error"],
)
def test_an_unreachable_gateway_is_the_typed_internal_error(exception: Exception) -> None:
    """Catches a client that lets httpx's own transport exception escape:
    the ingest caller would see `httpx.ConnectError` instead of a the contract
    code, and the MCP boundary has no mapping for it."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise exception

    client = _client(handler)

    with pytest.raises(GatewayUnavailableError) as excinfo:
        _redact(client, DOCUMENT)

    assert excinfo.value.code is ErrorCode.INTERNAL
    assert BASE_URL not in str(excinfo.value)


# --- pre-checks, all before any request is built -------------------------------


def test_a_document_of_exactly_the_cap_is_accepted() -> None:
    """Catches an exclusive comparison (`len(text) < MAX_REDACTION_CHARS`),
    which would refuse the longest document the console actually accepts and
    make a corpus file un-ingestable for no reason."""
    recorder = _Recorder(httpx.Response(200, json=_ok_body()))
    client = _client(recorder)
    text = "a" * MAX_REDACTION_CHARS

    result = _redact(client, text)

    assert len(text) == 16_384
    assert len(recorder.requests) == 1
    assert result.redaction_map_id == MAP_ID


def test_a_document_one_character_over_the_cap_is_refused_before_any_request() -> None:
    """Catches a missing length pre-check: the oversized document would be
    posted and refused by the console as a 422, so a local input mistake
    would cost a round trip AND put the raw document on the wire. The
    transport fails the test if it is reached, so a check placed after the
    request is built does not pass this row."""
    client = _client(_must_not_be_called)
    text = "a" * (MAX_REDACTION_CHARS + 1)

    with pytest.raises(GatewayRequestError) as excinfo:
        _redact(client, text)

    assert len(text) == 16_385
    assert excinfo.value.code is ErrorCode.VALIDATION


def test_a_tenant_with_no_configured_key_is_refused_before_any_request() -> None:
    """Catches a client that looks the key up with `api_keys.get(tenant, "")`
    and posts anyway: the console would answer 403, which the caller reads as
    a permission problem rather than as a missing local configuration line."""
    client = _client(_must_not_be_called)

    with pytest.raises(GatewayRequestError) as excinfo:
        _redact(client, DOCUMENT, tenant="unconfigured-tenant")

    assert excinfo.value.code is ErrorCode.VALIDATION


@pytest.mark.parametrize(
    "slug",
    ["Nordlicht", "nordlicht\n", "nordlicht ", "nord_licht", "n" * 65],
    ids=["uppercase", "trailing-newline", "trailing-space", "underscore", "over-64"],
)
def test_a_malformed_tenant_slug_is_refused_before_any_request(slug: str) -> None:
    """Catches a client that puts the caller's slug straight into the
    `X-Tenant` header: `nordlicht\\n` is a header-injection shape and
    `Nordlicht` is a second spelling of one tenant. Only the `check_slug`
    call can refuse these, since none of them is in `api_keys` either —
    which is why the assertion is on the code, shared by both pre-checks,
    and the kill claimed here is "no request is built", not "which of the
    two pre-checks spoke"."""
    client = _client(_must_not_be_called)

    with pytest.raises(ToolError) as excinfo:
        _redact(client, DOCUMENT, tenant=slug)

    assert excinfo.value.code is ErrorCode.VALIDATION


def test_a_configured_tenant_whose_slug_is_malformed_is_still_refused() -> None:
    """Catches a client that runs ONLY the key lookup and skips `check_slug`:
    here the malformed slug IS a configured key, so the lookup succeeds and
    the slug check is the single control that can refuse it. Deleting the
    `check_slug` call is the only change that flips this case."""
    client = _client(_must_not_be_called, api_keys={"Nordlicht\n": "fake-key"})

    with pytest.raises(ToolError) as excinfo:
        _redact(client, DOCUMENT, tenant="Nordlicht\n")

    assert excinfo.value.code is ErrorCode.VALIDATION


# --- settings -----------------------------------------------------------------

ENV_NAMES: tuple[str, ...] = (
    "DATABASE_URL",
    "REDIS_URL",
    "QDRANT_URL",
    "JWT_SECRET",
    "GATEWAY_URL",
    "GATEWAY_API_KEYS",
    "EMBEDDING_CACHE_DIR",
)

VALID_ENV: dict[str, str] = {
    "DATABASE_URL": "postgresql+asyncpg://helpdesk:placeholder@localhost:5432/helpdesk",
    "REDIS_URL": "redis://localhost:6379/0",
    "QDRANT_URL": "http://localhost:6333",
    # Obviously fake, tests only; 32+ bytes so the existing length gate passes.
    "JWT_SECRET": "test-only-jwt-secret-0123456789abcdef",
    "GATEWAY_URL": BASE_URL,
}


def _set_env(monkeypatch: pytest.MonkeyPatch, env: Mapping[str, str]) -> None:
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)


def test_the_gateway_api_keys_env_parses_into_a_tenant_to_key_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a field left as a plain string (the client would then index a
    string by tenant name and fail at request time) and a parser that splits
    on the wrong separator, which silently yields one entry whose tenant name
    contains a comma."""
    _set_env(
        monkeypatch,
        {**VALID_ENV, "GATEWAY_API_KEYS": "nordlicht:fake-key-a,cuan:fake-key-b"},
    )

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.gateway_api_keys == {"nordlicht": "fake-key-a", "cuan": "fake-key-b"}
    assert settings.gateway_url == BASE_URL


def test_the_gateway_api_keys_default_to_an_empty_map(monkeypatch: pytest.MonkeyPatch) -> None:
    """Catches a required field: every existing service builds Settings, and
    none of them redacts, so making the ingest-only key map mandatory would
    stop agent-api and mcp-server from starting at all."""
    _set_env(monkeypatch, VALID_ENV)

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.gateway_api_keys == {}


def test_the_embedding_cache_dir_defaults_inside_the_repo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a default of `None` or of `~/.cache/fastembed`: fastembed
    would then download ~130 MB outside this project folder, which the project rules
    forbid."""
    _set_env(monkeypatch, VALID_ENV)

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.embedding_cache_dir == pathlib.Path(".cache/fastembed")
    assert not settings.embedding_cache_dir.is_absolute()


@pytest.mark.parametrize(
    ("value", "position", "not_position"),
    [
        ("a:k,broken,c:k", "2", "1"),
        ("a:k,b:k,broken,d:k", "3", "2"),
    ],
    ids=["bad-second-pair", "bad-third-pair"],
)
def test_a_malformed_gateway_key_pair_names_its_one_based_position(
    monkeypatch: pytest.MonkeyPatch, value: str, position: str, not_position: str
) -> None:
    """Catches a parser that skips a malformed pair silently (the tenant then
    fails as a 401 at ingest time, weeks later) and one that reports a
    0-based index, which sends a reader to the wrong pair in the .env line.
    The assertion reads the error MESSAGES rather than `str(exc)`, whose
    pydantic help-URL carries its own digits; no other numeral appears in
    these values, so the position is the only digit the message can carry."""
    _set_env(monkeypatch, {**VALID_ENV, "GATEWAY_API_KEYS": value})

    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)  # type: ignore[call-arg]

    message = " ".join(str(error["msg"]) for error in excinfo.value.errors())
    assert position in message
    assert not_position not in message


def test_from_settings_builds_a_client_on_the_configured_url_and_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a `from_settings` that reads os.environ itself or hard-codes a
    URL, which would make the constructor's arguments dead and every test
    above prove nothing about the code that actually runs in ingest.

    The URL is read off the client's own `_base_url`, which is where the value
    lands: this client composes an ABSOLUTE URL per call rather than
    configuring httpx's `base_url`, so `client.client.base_url` is the empty
    URL for every instance and asserting on it could never fail."""
    _set_env(
        monkeypatch,
        {**VALID_ENV, "GATEWAY_API_KEYS": f"{TENANT}:fake-key-a"},
    )
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    client = RedactionClient.from_settings(settings)
    try:
        assert client._base_url == BASE_URL
        assert client.client.timeout.connect == CONNECT_TIMEOUT_SECONDS
        assert client.client.timeout.read == READ_TIMEOUT_SECONDS
    finally:
        asyncio.run(client.aclose())
