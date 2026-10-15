"""Typed runtime configuration, loaded from the environment (or .env locally).

Every service builds its `Settings` from here; nothing reads os.environ
directly, `web_origin_from_env` below being the one environment read this
module owns for the callers that cannot build full `Settings` yet. The four
backing-service values are required so a misconfigured
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

from pydantic import Field, field_validator
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


DEFAULT_MCP_ALLOWED_HOSTS = ["127.0.0.1:*", "localhost:*", "[::1]:*", "helpdesk-mcp-server:*"]
"""The `Host` headers the MCP transport's DNS-rebinding protection accepts.

The SDK infers `127.0.0.1:*`, `localhost:*` and `[::1]:*` when it is handed a
loopback `host=` argument, which is why every call from inside compose was
answered 421: agent-api dials the service name. The fourth value is this
repository's own compose service name, so the shipped stack works with no edit, and the wildcard port is used rather than `:8081` because
a republished port is not a security boundary while the name is (decision E).

A deployment reaching the server by any other name -- a cluster service, an
ingress hostname -- sets `MCP_ALLOWED_HOSTS` to its own list, which REPLACES
this one so the variable can narrow as well as widen. Pinned by
`test_settings.py::test_the_default_mcp_allowed_hosts_are_exactly_the_planned_values`
and, for the replacing half, by
`test_mcp_transport.py::test_a_configured_list_replaces_the_default_set`.
"""

MCP_ALLOWED_HOST_MAX_BYTES = 253
"""The DNS name limit, and the inclusive cap on an entry's host part."""


def _validated_mcp_allowed_hosts(value: list[str]) -> list[str]:
    """Return `value` unchanged if every entry is a usable pattern, else raise.

    The entries are matched against a request's `Host` header byte for byte
    (exact, or `startswith(base + ":")` for the `host:*` form), so a pattern
    carrying a stray byte does not fail loudly -- it matches no request that was
    ever sent, and the deployment reports 421 on every call with a value that
    looks correct in the `.env` that produced it. Whitespace is refused per
    ENTRY for that reason, the same rule `WEB_ORIGIN` applies to its own value.

    An empty list is refused because an empty allow-list is not a disabled
    protection but one that matches NOTHING, including the loopback callers
    (an earlier change decision D: there is no escape hatch, and this is where the
    absence is enforced).

    The length cap is measured on the HOST PART, not the whole entry: the `:*`
    or `:<port>` suffix is not part of any DNS name, so measuring the string
    would refuse a legal 253-byte name. 253 is accepted and 254 is not.

    One function rather than two checks: the
    field validator and `mcp_allowed_hosts_from_env` both call it, so a service
    building full `Settings` and `create_app()`'s import-time read apply the
    same rule. Pinned by
    `test_settings.py::test_an_empty_mcp_allowed_hosts_is_refused_at_load`,
    `::test_an_mcp_allowed_host_with_whitespace_is_refused_at_load`,
    `::test_an_mcp_allowed_host_of_exactly_253_bytes_is_accepted_and_254_is_not`
    and, for the two paths agreeing,
    `::test_the_mcp_allowed_hosts_field_and_the_helper_share_one_validator`.
    """
    if not value:
        raise ValueError("MCP_ALLOWED_HOSTS must list at least one host pattern")
    for entry in value:
        if not entry:
            raise ValueError("MCP_ALLOWED_HOSTS must contain no empty entry")
        if any(character.isspace() for character in entry):
            raise ValueError("MCP_ALLOWED_HOSTS entries must contain no whitespace")
        host_part = entry.rsplit(":", 1)[0] if ":" in entry else entry
        if len(host_part.encode()) > MCP_ALLOWED_HOST_MAX_BYTES:
            raise ValueError(
                f"MCP_ALLOWED_HOSTS host parts must be at most {MCP_ALLOWED_HOST_MAX_BYTES} bytes"
            )
    return value


def mcp_allowed_hosts_from_env() -> list[str]:
    """The transport's allowed `Host` patterns, read at import time.

    `create_app()` cannot call `load_settings()`: it needs the four
    backing-service values no test process has, which is the wall an earlier change
    decision A1 already hit for `WEB_ORIGIN`. This helper reads the one
    variable, shares `Settings.mcp_allowed_hosts`'s default and runs the same
    validator, so there is one default, one rule and one env name.

    The configured list REPLACES the default rather than extending it, so a
    deployment can narrow the set as well as widen it. Pinned by
    `test_settings.py::test_the_default_mcp_allowed_hosts_are_exactly_the_planned_values`
    and `test_mcp_transport.py::test_a_configured_list_replaces_the_default_set`.
    """
    configured = os.environ.get("MCP_ALLOWED_HOSTS")
    if configured is None:
        return list(DEFAULT_MCP_ALLOWED_HOSTS)
    return _validated_mcp_allowed_hosts(configured.split(","))


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
    # .env.demo holds a working value for every name; .env adds only the real
    # paid keys on top. Later files win, so .env overrides .env.demo.
    model_config = SettingsConfigDict(env_file=(".env.demo", ".env"), extra="ignore")

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

    policy_dir: Path = Path("policies")
    """The corpus root ingestion walks, as `{tenant_slug}/{version}/{file}`.

    A setting rather than the script's hardcoded constant so the repository
    works as a template: an adopter points `POLICY_DIR` at their own folder
    and re-ingests, without editing a file this project owns (an earlier change
    section 1). Relative by default, resolved against the process's working
    directory the way `embedding_cache_dir` is.

    It shapes no stored value: `rag/pipeline.py` writes `source_path`
    corpus-relative, so a long root cannot push a row past the
    `SOURCE_PATH_LENGTH` cap. Pinned by
    `test_settings.py::test_the_default_policy_dir_is_exactly_the_planned_value`.
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

    mcp_allowed_hosts: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(DEFAULT_MCP_ALLOWED_HOSTS)
    )
    """The `Host` headers the MCP transport accepts, from `MCP_ALLOWED_HOSTS`.

    A field as well as `mcp_allowed_hosts_from_env`'s env read so every service
    building full `Settings` validates it too: without it
    the value would be checked on the import-time path only, and a malformed
    pattern would reach whatever reads `settings.mcp_allowed_hosts` later.

    `NoDecode` because pydantic-settings JSON-decodes a `list` field before any
    validator sees it, so the configured `a,b` form would fail as a JSON parse
    error naming neither the entry nor the reason. A `default_factory` rather
    than the constant itself so no caller can mutate the shared default. Pinned
    by
    `test_settings.py::test_the_mcp_allowed_hosts_field_and_the_helper_share_one_validator`.
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

    @field_validator("policy_dir", mode="before")
    @classmethod
    def _policy_dir_is_a_usable_root(cls, value: object) -> object:
        """Refuse an empty value and one with surrounding whitespace.

        Before validation, not after: `Path("")` is a legal `Path` equal to
        `Path(".")`, so an empty `POLICY_DIR=` line would pass an after-mode
        check and silently retarget the whole corpus walk at the working
        directory. Interior spaces are ACCEPTED, unlike `WEB_ORIGIN`'s rule: this is a filesystem path, and a folder with a
        space in its name is one an adopter really has, while the trailing
        newline from a pasted `.env` value is the failure worth catching.
        Pinned by
        `test_settings.py::test_an_empty_policy_dir_is_refused_at_load`,
        `::test_a_policy_dir_with_surrounding_whitespace_is_refused_at_load`
        and, for the accepted half,
        `::test_a_policy_dir_with_an_interior_space_is_accepted`.
        """
        if not isinstance(value, str):
            return value
        if not value:
            raise ValueError("POLICY_DIR must not be empty")
        if value != value.strip():
            raise ValueError("POLICY_DIR must have no leading or trailing whitespace")
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

    @field_validator("mcp_allowed_hosts", mode="before")
    @classmethod
    def _mcp_allowed_hosts_are_usable_patterns(cls, value: object) -> object:
        """Split the configured string, then delegate to the shared rule.

        Before validation, not after, because `NoDecode` hands the environment's
        `a,b` string through untouched: the split belongs with the check that
        judges the parts, and an after-mode validator would see a one-element
        list holding the whole line. A value already given as a list (a service
        constructing `Settings` in code, or a test) is validated as it stands.

        A body rather than a second copy of the checks: decision A's point is
        that the field and the import-time helper cannot disagree. Pinned by
        `test_settings.py::test_the_mcp_allowed_hosts_field_and_the_helper_share_one_validator`.
        """
        if isinstance(value, str):
            return _validated_mcp_allowed_hosts(value.split(","))
        if isinstance(value, list):
            return _validated_mcp_allowed_hosts([str(entry) for entry in value])
        return value

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
