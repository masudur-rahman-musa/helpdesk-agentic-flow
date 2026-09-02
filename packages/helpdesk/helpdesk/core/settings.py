"""Typed runtime configuration, loaded from the environment (or .env locally).

Every service builds its `Settings` from here; nothing reads os.environ
directly. All values are required so a misconfigured deployment fails at
startup, not on first use.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    redis_url: str
    qdrant_url: str


def load_settings() -> Settings:
    """Build Settings from the environment.

    mypy synthesizes a field-based __init__ for pydantic models
    (dataclass_transform), so the env-sourced no-arg call needs one ignore,
    kept here so call sites stay clean.
    """
    return Settings()  # type: ignore[call-arg]
