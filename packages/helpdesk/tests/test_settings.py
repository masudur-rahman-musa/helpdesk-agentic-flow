"""Tests for helpdesk.core.settings (an earlier change, section 6, step 2; extended by
an earlier change for ``jwt_secret``).

API under test:
    class helpdesk.core.settings.Settings(pydantic_settings.BaseSettings)
        database_url: str   <- env DATABASE_URL, required, no default
        redis_url: str      <- env REDIS_URL, required, no default
        qdrant_url: str     <- env QDRANT_URL, required, no default
        jwt_secret: str     <- env JWT_SECRET, required, no default,
                               rejected when shorter than 32 BYTES (the design)

Tests pass ``_env_file=None`` so a developer's real ``.env`` cannot leak in;
env vars themselves are controlled with monkeypatch. The ``load_settings``
tests chdir into an empty tmp dir for the same reason (its Settings() call
reads the relative ``.env``).
"""

import pytest
from helpdesk.core.settings import Settings, load_settings
from pydantic import ValidationError

ENV_NAMES: tuple[str, ...] = ("DATABASE_URL", "REDIS_URL", "QDRANT_URL", "JWT_SECRET")

VALID_ENV: dict[str, str] = {
    "DATABASE_URL": "postgresql+asyncpg://helpdesk:placeholder@localhost:5432/helpdesk",
    "REDIS_URL": "redis://localhost:6379/0",
    "QDRANT_URL": "http://localhost:6333",
    # Obviously fake, tests only; exactly 32+ bytes so the length gate passes.
    "JWT_SECRET": "test-only-jwt-secret-0123456789abcdef",
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
    assert settings.jwt_secret == VALID_ENV["JWT_SECRET"]


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


# --- an earlier change: jwt_secret length gate (the design: >= 32 BYTES, not characters) --

# Boundary rows: 31 ascii bytes fails, 32 passes; a 16-char string of 2-byte
# UTF-8 characters is 32 bytes and must pass (the rule counts bytes).
JWT_SECRET_LENGTH_CASES: tuple[tuple[str, str, bool], ...] = (
    ("empty", "", False),
    ("31 ascii bytes", "a" * 31, False),
    ("32 ascii bytes", "a" * 32, True),
    ("33 ascii bytes", "a" * 33, True),
    ("16 two-byte chars = 32 bytes", "é" * 16, True),
    ("15 two-byte chars = 30 bytes", "é" * 15, False),
)


@pytest.mark.parametrize(
    ("secret", "valid"),
    [(secret, valid) for _label, secret, valid in JWT_SECRET_LENGTH_CASES],
    ids=[label for label, _secret, _valid in JWT_SECRET_LENGTH_CASES],
)
def test_jwt_secret_length_boundary(
    monkeypatch: pytest.MonkeyPatch, secret: str, valid: bool
) -> None:
    _set_env(monkeypatch, {**VALID_ENV, "JWT_SECRET": secret})

    if valid:
        assert Settings(_env_file=None).jwt_secret == secret
    else:
        with pytest.raises(ValidationError) as excinfo:
            Settings(_env_file=None)
        failed_fields = {str(err["loc"][0]).upper() for err in excinfo.value.errors()}
        assert "JWT_SECRET" in failed_fields


def test_load_settings_fails_when_jwt_secret_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    monkeypatch.chdir(str(tmp_path))  # no .env here, so nothing can leak in
    _set_env(monkeypatch, {k: v for k, v in VALID_ENV.items() if k != "JWT_SECRET"})

    with pytest.raises(ValidationError):
        load_settings()


def test_load_settings_fails_when_jwt_secret_too_short(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    monkeypatch.chdir(str(tmp_path))
    _set_env(monkeypatch, {**VALID_ENV, "JWT_SECRET": "a" * 31})

    with pytest.raises(ValidationError):
        load_settings()


# --- an earlier change: MCP_URL, validated at load (decision F) ------------------------
#
# `MCP_URL` is not in `ENV_NAMES`: it carries a default, so `_set_env` must not
# clear it for the tests above. These cases clear it themselves, so a developer
# with `MCP_URL` exported cannot make the default row pass or fail by accident.

MCP_URL_DEFAULT = "http://localhost:8081/mcp"
"""The default the field declares, written out rather than imported.

a project rule: importing the constant and asserting it equals itself pins
nothing, and this value is what every agent turn dials when the environment
says nothing."""


def _set_env_without_mcp_url(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, VALID_ENV)
    monkeypatch.delenv("MCP_URL", raising=False)


def test_the_mcp_url_default_is_exactly_the_planned_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches an implementation that defaults `MCP_URL` to the gateway's port
    (8080), to a bare origin with no `/mcp` path, or to a value only the author's
    machine resolves: every one of those makes an unconfigured deployment open
    its per-turn MCP session against the wrong process and fail on the first
    tool call rather than at startup."""
    _set_env_without_mcp_url(monkeypatch)

    assert Settings(_env_file=None).mcp_url == MCP_URL_DEFAULT


# Each rejected row is refused by exactly ONE clause of the validator, so no row
# passes on a neighbouring check's behalf: the whitespace rows all parse as
# perfectly good URLs with a scheme and a netloc, `ftp://` and the scheme-less
# form both have a netloc and no whitespace, and `http:///mcp` has a legal
# scheme and no whitespace. Accepted rows are the two shapes deployment uses.
MCP_URL_CASES: tuple[tuple[str, str, bool], ...] = (
    ("the default", MCP_URL_DEFAULT, True),
    ("https with a port", "https://mcp.internal.invalid:8443/mcp", True),
    ("trailing newline", MCP_URL_DEFAULT + "\n", False),
    ("trailing CRLF", MCP_URL_DEFAULT + "\r\n", False),
    ("trailing space", MCP_URL_DEFAULT + " ", False),
    ("leading space", " " + MCP_URL_DEFAULT, False),
    ("internal space", "http://localhost:8081/m cp", False),
    ("a tab inside", "http://localhost:8081/m\tcp", False),
    ("ftp scheme", "ftp://localhost:8081/mcp", False),
    ("no scheme at all", "localhost:8081/mcp", False),
    ("a relative path", "/mcp", False),
    ("empty netloc", "http:///mcp", False),
    ("empty string", "", False),
)


@pytest.mark.parametrize(
    ("value", "valid"),
    [(value, valid) for _label, value, valid in MCP_URL_CASES],
    ids=[label for label, _value, _valid in MCP_URL_CASES],
)
def test_a_malformed_mcp_url_is_refused_at_settings_load(
    monkeypatch: pytest.MonkeyPatch, value: str, valid: bool
) -> None:
    """Catches a validator written as `urlsplit(value)` and nothing more.

    The three trailing-whitespace rows are the ones such an implementation
    passes: `urlsplit` parses `http://localhost:8081/mcp\\n` into a scheme, a
    netloc and a path without complaint, and the stray byte then surfaces as a
    DNS failure on a user's first turn instead of at process start. The `ftp`,
    scheme-less, relative and empty-netloc rows are each refused by one
    remaining clause, so deleting any single clause turns at least one row red
    (a project rule's rejection table, applied to a URL rather than a slug)."""
    _set_env_without_mcp_url(monkeypatch)
    monkeypatch.setenv("MCP_URL", value)

    if valid:
        assert Settings(_env_file=None).mcp_url == value
    else:
        with pytest.raises(ValidationError) as excinfo:
            Settings(_env_file=None)
        failed_fields = {str(err["loc"][0]).upper() for err in excinfo.value.errors()}
        assert "MCP_URL" in failed_fields
