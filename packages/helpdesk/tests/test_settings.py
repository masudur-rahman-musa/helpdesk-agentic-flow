"""Tests for helpdesk.core.settings.

API under test:
    class helpdesk.core.settings.Settings(pydantic_settings.BaseSettings)
        database_url: str   <- env DATABASE_URL, required, no default
        redis_url: str      <- env REDIS_URL, required, no default
        qdrant_url: str     <- env QDRANT_URL, required, no default

Tests pass ``_env_file=None`` so a developer's real ``.env`` cannot leak in;
env vars themselves are controlled with monkeypatch.
"""

import pytest
from helpdesk.core.settings import Settings
from pydantic import ValidationError

ENV_NAMES: tuple[str, str, str] = ("DATABASE_URL", "REDIS_URL", "QDRANT_URL")

VALID_ENV: dict[str, str] = {
    "DATABASE_URL": "postgresql+asyncpg://helpdesk:placeholder@localhost:5432/helpdesk",
    "REDIS_URL": "redis://localhost:6379/0",
    "QDRANT_URL": "http://localhost:6333",
}


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def _set_env(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    _clear_env(monkeypatch)
    for name, value in env.items():
        monkeypatch.setenv(name, value)


def test_settings_load_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, VALID_ENV)

    settings = Settings(_env_file=None)

    assert settings.database_url == VALID_ENV["DATABASE_URL"]
    assert settings.redis_url == VALID_ENV["REDIS_URL"]
    assert settings.qdrant_url == VALID_ENV["QDRANT_URL"]


@pytest.mark.parametrize("missing", ENV_NAMES)
def test_missing_required_value_fails_validation(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    env = {name: value for name, value in VALID_ENV.items() if name != missing}
    _set_env(monkeypatch, env)

    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)

    # The error must point at the missing field, not something incidental.
    failed_fields = {str(err["loc"][0]).upper() for err in excinfo.value.errors()}
    assert missing in failed_fields


def test_all_missing_fails_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)
