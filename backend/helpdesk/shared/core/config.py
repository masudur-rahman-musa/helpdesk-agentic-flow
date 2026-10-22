"""Typed runtime settings. Every value is checked at load, so a bad one fails at startup."""

import math
import os
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

DEFAULT_WEB_ORIGIN = "http://localhost:3000"
"""The one browser origin agent-api sends CORS headers for. Never a wildcard."""


def _validated_web_origin(value: str) -> str:
    """Raise unless a bare origin: CORS compares it byte for byte with `Origin`."""
    if any(character.isspace() for character in value):
        raise ValueError("WEB_ORIGIN must contain no whitespace")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("WEB_ORIGIN must be an absolute http or https URL")
    if parsed.path or parsed.query or parsed.fragment:
        raise ValueError("WEB_ORIGIN must be a bare origin with no path, query or fragment")
    return value


DEFAULT_MCP_ALLOWED_HOSTS = ["127.0.0.1:*", "localhost:*", "[::1]:*", "helpdesk-mcp-server:*"]
"""Loopback names plus the compose service name, which agent-api dials."""

MCP_ALLOWED_HOST_MAX_BYTES = 253


def _validated_mcp_allowed_hosts(value: list[str]) -> list[str]:
    """Raise on an empty list (it blocks everyone) or an entry that can never match."""
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
    """Read at import time, where full `Settings` cannot load yet. Same rule as the field."""
    configured = os.environ.get("MCP_ALLOWED_HOSTS")
    if configured is None:
        return list(DEFAULT_MCP_ALLOWED_HOSTS)
    return _validated_mcp_allowed_hosts(configured.split(","))


def web_origin_from_env() -> str:
    """Read at import time, since CORS is fixed before startup. Same rule as the field."""
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
    """Where the MCP server's streamable HTTP transport is mounted."""

    gateway_api_keys: Annotated[dict[str, str], NoDecode] = {}
    """One gateway key per tenant slug, from `tenant:key,...`; the gateway refuses a mismatch."""

    langfuse_public_key: str | None = None
    """`None` turns tracing off; a trace never gates a turn."""

    langfuse_secret_key: str | None = None

    langfuse_host: str = "https://cloud.langfuse.com"

    qdrant_timeout_seconds: float = 30.0
    """So a dead vector store cannot hang ingestion."""

    embedding_cache_dir: Path = Path(".cache/fastembed")
    """Where fastembed keeps models: inside the repo, not fastembed's `~/.cache`."""

    policy_dir: Path = Path("policies")
    """The policy folder ingestion walks, as `{tenant_slug}/{version}/{file}`."""

    web_origin: str = DEFAULT_WEB_ORIGIN

    mcp_allowed_hosts: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(DEFAULT_MCP_ALLOWED_HOSTS)
    )

    role_suggestion_floor: float = 0.70
    """A suggested role must score at least this. 0 to 1 inclusive."""

    policy_refusal_floor: float = 0.35
    """A policy answer whose best passage scores below this is refused. 0 to 1 inclusive."""

    save_turn_steps: bool = True
    """Save each step of a chat turn in LangGraph's checkpoint tables, which
    `python -m helpdesk.agent_api.cli` makes. Off runs the same graph, saving nothing."""

    eval_routes_enabled: bool = False
    """Turns on `POST /admin/test/reset` and `POST /v1/eval/conversation`. Off in production."""

    @field_validator("role_suggestion_floor", "policy_refusal_floor")
    @classmethod
    def _cutoff_is_between_zero_and_one(cls, value: float) -> float:
        # NaN fails every comparison, so test the range the positive way round.
        if not (math.isfinite(value) and 0.0 <= value <= 1.0):
            raise ValueError("a cut-off must be a number from 0 to 1 inclusive")
        return value

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
        """A pasted trailing newline would otherwise show up later as a DNS error."""
        parsed = urlsplit(value)
        if any(character.isspace() for character in value):
            raise ValueError("MCP_URL must contain no whitespace")
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("MCP_URL must be an absolute http or https URL")
        return value

    @field_validator("policy_dir", mode="before")
    @classmethod
    def _policy_dir_is_a_usable_root(cls, value: object) -> object:
        """Runs before parsing: `Path("")` becomes `Path(".")` and would walk the cwd."""
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
        return _validated_web_origin(value)

    @field_validator("mcp_allowed_hosts", mode="before")
    @classmethod
    def _mcp_allowed_hosts_are_usable_patterns(cls, value: object) -> object:
        if isinstance(value, str):
            return _validated_mcp_allowed_hosts(value.split(","))
        if isinstance(value, list):
            return _validated_mcp_allowed_hosts([str(entry) for entry in value])
        return value

    @field_validator("gateway_api_keys", mode="before")
    @classmethod
    def _parse_key_pairs(cls, value: object) -> object:
        """Parse into a map; a bad pair raises with its position instead of a later 401."""
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
    """Build Settings from the environment, keeping mypy's one ignore here."""
    return Settings()  # type: ignore[call-arg]
