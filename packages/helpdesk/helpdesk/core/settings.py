"""Typed runtime configuration, loaded from the environment (or .env locally).

Every service builds its `Settings` from here; nothing reads os.environ
directly, `web_origin_from_env` below being the one environment read this
module owns for the callers that cannot build full `Settings` yet. The four backing-service values are required so a misconfigured
deployment fails at startup, not on first use; every other value carries a
default with its reason next to the field. A default is not an excuse for a
missing check: `MCP_URL` and `GATEWAY_API_KEYS` are both validated here, so a
malformed value is refused when the process starts rather than on the first
call that uses it.
"""

import os
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


DEFAULT_WEB_ORIGIN = "http://localhost:3000"
"""The one browser origin agent-api answers with CORS headers for.

The web app's development origin, which is also the only origin that talks to
agent-api at all: the browser reaches Next, and Next reaches this service
server-side. A deployment sets `WEB_ORIGIN` to its own origin; the value is
never a wildcard, on any route or any method. Pinned by
`test_settings.py::test_the_default_web_origin_is_exactly_the_planned_value`.
"""


def _validated_web_origin(value: str) -> str:
    """Return `value` unchanged if it is an exact origin, else raise.

    An exact origin is scheme, host and optional port and NOTHING else: a
    browser's `Origin` header carries exactly that, and `CORSMiddleware`
    compares the two strings byte for byte. So a trailing slash, a path, a
    query or a fragment does not fail loudly -- it silently matches no browser
    request, and the deployment reports a CORS error with no server-side error
    to grep for.

    Whitespace is refused for the same reason `MCP_URL` refuses it: a value
    copied into `.env` with a trailing newline parses as a perfectly good URL
    whose host carries the stray character. Here it is worse than a bad host,
    because this value is written into a RESPONSE HEADER.

    One function rather than two checks: the field
    validator and `web_origin_from_env` both call it, so a service building
    full `Settings` and a service reading the environment at import time apply
    the same rule. Pinned by
    `test_settings.py::test_a_malformed_web_origin_is_refused_at_load` and, for
    the field's own path, by
    `test_settings.py::test_the_settings_field_and_the_helper_share_one_validator`.
    """
    if any(character.isspace() for character in value):
        raise ValueError("WEB_ORIGIN must contain no whitespace")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("WEB_ORIGIN must be an absolute http or https URL")
    if parsed.path or parsed.query or parsed.fragment:
        raise ValueError("WEB_ORIGIN must be a bare origin with no path, query or fragment")
    return value


def web_origin_from_env() -> str:
    """The allowed browser origin, read from the environment at import time.

    agent-api builds its `app` at module import and Starlette freezes the
    middleware stack before the first request, so the CORS origin cannot come
    from `lifespan` and cannot come from `load_settings()`, which requires four
    backing-service values no test process has. This
    helper reads the one variable, shares `Settings.web_origin`'s default and
    runs the same validator, so there is still one default, one rule and one
    env name. Pinned by
    `test_settings.py::test_the_default_web_origin_is_exactly_the_planned_value`
    and `::test_an_accepted_web_origin_is_returned_unchanged`.
    """
    return _validated_web_origin(os.environ.get("WEB_ORIGIN", DEFAULT_WEB_ORIGIN))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    redis_url: str
    qdrant_url: str
    jwt_secret: str

    gateway_url: str = "http://localhost:8080/v1"
    """The Ops Console gateway's versioned base URL; `/redact` hangs off it."""

    mcp_url: str = "http://localhost:8081/mcp"
    """Where the MCP server's streamable HTTP transport is mounted (the design).

    Here rather than in an agent-api-local environment read (an earlier change decision
    F): every other URL, secret and key map already loads and validates in this
    class, and a second loader is a second place a misconfiguration hides until
    the first turn.

    Validated at load by `_mcp_url_is_an_absolute_http_url`, so a malformed
    base URL fails when the process starts instead of on the first tool call.
    Pinned by
    `test_settings.py::test_a_malformed_mcp_url_is_refused_at_settings_load`.
    """

    gateway_api_keys: Annotated[dict[str, str], NoDecode] = {}
    """One gateway API key per tenant slug, read from `GATEWAY_API_KEYS` in the
    form `tenant:key,tenant:key`.

    `NoDecode` because pydantic-settings JSON-decodes a `dict` field before any
    validator sees it, so without it the configured string fails as a JSON
    parse error naming neither the pair nor the reason.

    The gateway answers 403 when `X-Tenant` does not match the presented key,
    so one shared key cannot ingest two tenants under their own slugs. The
    pairs are parsed HERE rather than at the call site, so a malformed pair
    fails when configuration is loaded, naming its 1-based position, instead
    of surfacing as an unexplained 401 at request time. Pinned by
    `test_rag_gateway.py::test_a_malformed_gateway_key_pair_names_its_one_based_position`.

    Empty by default, unlike the four values above: ingestion is a script, not
    a served request, so an unconfigured deployment is refused by
    `RedactionClient`'s typed `VALIDATION` pre-check on the one tenant it was
    asked to ingest, rather than by refusing every service its settings.
    Pinned by
    `test_rag_gateway.py::test_a_tenant_with_no_configured_key_is_refused_before_any_request`.
    """

    langfuse_public_key: str | None = None
    """The Langfuse project's public key, or `None` when tracing is off.

    Optional, unlike the four backing services above: a trace is a record of a
    turn and never a condition of one, so a deployment
    with no Langfuse account serves turns and traces nothing rather than
    refusing to start.
    """

    langfuse_secret_key: str | None = None
    """The other half of the credential. BOTH are needed or neither is used.

    `helpdesk.obs.tracing.configure_tracing` builds no client unless both are
    set: half a credential is an exporter that can only retry a 401 behind
    every turn. Pinned by
    `test_obs_tracing.py::test_a_half_configured_deployment_builds_no_client`.
    """

    langfuse_host: str = "https://cloud.langfuse.com"
    """Where the traces go. Langfuse Cloud's EU endpoint by default.

    A default rather than a required field so the two keys alone configure the
    common case, and explicit rather than left to the SDK:
    a self-hosted deployment sets this, and a facade that ignored it would
    ship its traces to a service nobody configured. Pinned by
    `test_obs_tracing.py::test_the_three_langfuse_settings_carry_exactly_the_planned_defaults`
    and asserted on the CONSTRUCTED client by
    `::test_both_keys_configured_build_exactly_one_client_on_the_configured_host`.
    """

    qdrant_timeout_seconds: float = 30.0
    """The timeout every Qdrant client this project builds carries.

    Explicit rather than the library's own default:
    ingestion upserts a whole document's points in one request, and a socket
    with no deadline turns an unreachable vector store into a walker that
    never returns. 30 seconds is the read half 14b's gateway client already
    uses, so the two external calls of one ingest wait the same. Pinned by
    `test_rag_vectors.py::test_the_settings_default_timeout_is_exactly_the_planned_value`,
    and asserted on the CONSTRUCTED client by
    `test_rag_vectors.py::test_the_constructed_client_carries_the_configured_timeout`.
    """

    embedding_cache_dir: Path = Path(".cache/fastembed")
    """Where fastembed keeps its downloaded models.

    Inside the repo and gitignored: a project rule says
    this project writes nothing outside its own folder, and fastembed's own
    default is `~/.cache`.
    """

    web_origin: str = DEFAULT_WEB_ORIGIN
    """The single browser origin agent-api allows cross-origin requests from.

    A field as well as `web_origin_from_env`'s env read so every service
    building full `Settings` validates it too: without
    it the value would be checked on the import-time path only, and a
    malformed origin would reach whatever reads `settings.web_origin` later.
    Pinned by
    `test_settings.py::test_the_settings_field_and_the_helper_share_one_validator`.
    """

    @field_validator("jwt_secret")
    @classmethod
    def _jwt_secret_at_least_32_bytes(cls, value: str) -> str:
        # Bytes, not characters: HS256 key strength is measured in key bytes.
        if len(value.encode()) < 32:
            raise ValueError("JWT_SECRET must be at least 32 bytes")
        return value

    @field_validator("mcp_url")
    @classmethod
    def _mcp_url_is_an_absolute_http_url(cls, value: str) -> str:
        """An absolute http(s) URL with a host, and no whitespace anywhere.

        The whitespace half is not decoration: a value copied into `.env` with
        a trailing newline or a trailing space parses as a perfectly good URL
        whose host carries the stray character, and the failure surfaces as a
        DNS error on the first tool call rather than here. Refused at load
        instead, which is the whole point of the field living in this class. Pinned by
        `test_settings.py::test_a_malformed_mcp_url_is_refused_at_settings_load`.
        """
        parsed = urlsplit(value)
        if any(character.isspace() for character in value):
            raise ValueError("MCP_URL must contain no whitespace")
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("MCP_URL must be an absolute http or https URL")
        return value

    @field_validator("web_origin")
    @classmethod
    def _web_origin_is_an_exact_origin(cls, value: str) -> str:
        """Delegate to `_validated_web_origin`, the one rule both paths share.

        A body rather than a second copy of the checks: decision A1's point is
        that the field and the import-time helper cannot disagree. Pinned by
        `test_settings.py::test_the_settings_field_and_the_helper_share_one_validator`.
        """
        return _validated_web_origin(value)

    @field_validator("gateway_api_keys", mode="before")
    @classmethod
    def _parse_key_pairs(cls, value: object) -> object:
        """Parse `tenant:key,tenant:key` into a map, or fail naming the pair.

        Pydantic reads a `dict[str, str]` field from the environment as JSON,
        which is not the shape the design's configuration uses, so the string
        form is converted before validation. A pair with no colon, an empty
        tenant or an empty key raises naming its 1-based position: the
        alternative is a key silently dropped and a 401 at ingest time with
        nothing to grep for.
        """
        if not isinstance(value, str):
            return value
        pairs: dict[str, str] = {}
        for position, item in enumerate(value.split(","), start=1):
            entry = item.strip()
            if not entry:
                continue
            tenant, separator, key = entry.partition(":")
            if not separator or not tenant.strip() or not key.strip():
                raise ValueError(f"GATEWAY_API_KEYS pair {position} is not tenant:key")
            pairs[tenant.strip()] = key.strip()
        return pairs


def load_settings() -> Settings:
    """Build Settings from the environment.

    mypy synthesizes a field-based __init__ for pydantic models
    (dataclass_transform), so the env-sourced no-arg call needs one ignore,
    kept here so call sites stay clean.
    """
    return Settings()  # type: ignore[call-arg]
