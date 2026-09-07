"""Typed runtime configuration, loaded from the environment (or .env locally).

Every service builds its `Settings` from here; nothing reads os.environ
directly. The four backing-service values are required so a misconfigured
deployment fails at startup, not on first use; the four ingestion values
added by an earlier change carry defaults, each with its reason next to the
field.
"""

from pathlib import Path
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    redis_url: str
    qdrant_url: str
    jwt_secret: str

    gateway_url: str = "http://localhost:8080/v1"
    """The Ops Console gateway's versioned base URL; `/redact` hangs off it."""

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

    @field_validator("jwt_secret")
    @classmethod
    def _jwt_secret_at_least_32_bytes(cls, value: str) -> str:
        # Bytes, not characters: HS256 key strength is measured in key bytes.
        if len(value.encode()) < 32:
            raise ValueError("JWT_SECRET must be at least 32 bytes")
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
