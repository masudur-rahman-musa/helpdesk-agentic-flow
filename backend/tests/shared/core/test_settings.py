"""Tests for helpdesk.core.settings, including ``jwt_secret``.

API under test:
    class helpdesk.core.settings.Settings(pydantic_settings.BaseSettings)
        database_url: str   <- env DATABASE_URL, required, no default
        redis_url: str      <- env REDIS_URL, required, no default
        qdrant_url: str     <- env QDRANT_URL, required, no default
        jwt_secret: str     <- env JWT_SECRET, required, no default,
                               rejected when shorter than 32 BYTES

Tests pass ``_env_file=None`` so a developer's real ``.env`` cannot leak in;
env vars themselves are controlled with monkeypatch. The ``load_settings``
tests chdir into an empty tmp dir for the same reason (its Settings() call
reads the relative ``.env``).
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from helpdesk.shared.core.config import (
    DEFAULT_MCP_ALLOWED_HOSTS,
    DEFAULT_WEB_ORIGIN,
    Settings,
    load_settings,
    mcp_allowed_hosts_from_env,
    web_origin_from_env,
)

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


# --- jwt_secret length gate (>= 32 BYTES, not characters) --

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


# --- MCP_URL, validated at load ------------------------
#
# `MCP_URL` is not in `ENV_NAMES`: it carries a default, so `_set_env` must not
# clear it for the tests above. These cases clear it themselves, so a developer
# with `MCP_URL` exported cannot make the default row pass or fail by accident.

MCP_URL_DEFAULT = "http://localhost:8081/mcp"
"""The default the field declares, written out rather than imported.

importing the constant and asserting it equals itself pins
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
    remaining clause, so deleting any single clause turns at least one row red."""
    _set_env_without_mcp_url(monkeypatch)
    monkeypatch.setenv("MCP_URL", value)

    if valid:
        assert Settings(_env_file=None).mcp_url == value
    else:
        with pytest.raises(ValidationError) as excinfo:
            Settings(_env_file=None)
        failed_fields = {str(err["loc"][0]).upper() for err in excinfo.value.errors()}
        assert "MCP_URL" in failed_fields


# --- WEB_ORIGIN, the one allowed CORS origin -----------
#
# `WEB_ORIGIN` is not in `ENV_NAMES` for `MCP_URL`'s reason: it carries a
# default, so `_set_env` must not clear it for the cases above, and these cases
# clear it themselves so a developer with `WEB_ORIGIN` exported cannot make the
# default row pass or fail by accident.

WEB_ORIGIN_DEFAULT = "http://localhost:3000"
"""The default the field and `web_origin_from_env` share, written out.

importing `DEFAULT_WEB_ORIGIN` and asserting it equals itself
pins nothing, and this value is the exact string agent-api echoes back into
`access-control-allow-origin` when the environment says nothing."""


def _set_env_without_web_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, VALID_ENV)
    monkeypatch.delenv("WEB_ORIGIN", raising=False)


def test_the_default_web_origin_is_exactly_the_planned_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches an implementation whose unconfigured default is anything but the
    dev web origin - a wildcard, agent-api's own port (8000), `https` where the
    local app serves `http`, or a value carrying a trailing slash: each one
    makes an unconfigured deployment answer the browser with an
    `access-control-allow-origin` the web app's origin does not match, which
    the browser reports as a CORS failure with no server-side error at all.

    Also catches a `web_origin_from_env` that does not share the field's
    default (the whole point): the second assertion reads the helper
    with `WEB_ORIGIN` unset, so a helper defaulting to `""` or to its own
    literal is red here even though `DEFAULT_WEB_ORIGIN` is right.
    """
    _set_env_without_web_origin(monkeypatch)

    assert DEFAULT_WEB_ORIGIN == WEB_ORIGIN_DEFAULT
    assert web_origin_from_env() == WEB_ORIGIN_DEFAULT


@pytest.mark.parametrize(
    "value",
    ["http://localhost:3000", "https://helpdesk.example"],
    ids=["the http dev origin", "an https deployed origin"],
)
def test_an_accepted_web_origin_is_returned_unchanged(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Catches a helper that normalizes what it was given instead of returning
    it: lowercasing is harmless, but appending a trailing slash, stripping the
    port, or rebuilding the string through `urlunsplit` all produce a value
    that no longer equals the browser's `Origin` header byte for byte, and the
    CORS middleware compares those two strings exactly.

    Also catches a validator so strict it refuses a legitimate origin - both
    rows are shapes named as accepted, and `https` with no
    port is the deployed shape a localhost-only check would reject.
    """
    _set_env_without_web_origin(monkeypatch)
    monkeypatch.setenv("WEB_ORIGIN", value)

    assert web_origin_from_env() == value


# Each rejected row is refused by exactly ONE clause, so no row passes on a
# neighbouring check's behalf: the three whitespace rows parse as good URLs
# with a scheme and a netloc, the trailing slash and the path rows differ only
# in what follows the slash, the query and fragment rows carry an empty path,
# `ftp://` has a netloc and no whitespace, `localhost:3000` parses with scheme
# `localhost` and no netloc, and `https://` has a legal scheme and an empty
# host. Deleting any single clause of the validator turns at least one row red.
WEB_ORIGIN_CASES: tuple[tuple[str, str, bool], ...] = (
    ("the default", WEB_ORIGIN_DEFAULT, True),
    ("an https deployed origin", "https://helpdesk.example", True),
    ("trailing slash", "http://localhost:3000/", False),
    ("a path", "https://helpdesk.example/app", False),
    ("a query", "https://helpdesk.example?x=1", False),
    ("a fragment", "https://helpdesk.example#f", False),
    ("ftp scheme", "ftp://helpdesk.example", False),
    ("no scheme, so no netloc", "localhost:3000", False),
    ("empty host", "https://", False),
    ("trailing newline", "http://localhost:3000\n", False),
    ("trailing CRLF", "http://localhost:3000\r\n", False),
    ("an interior space", "http://localhost :3000", False),
)


@pytest.mark.parametrize(
    ("value", "valid"),
    [(value, valid) for _label, value, valid in WEB_ORIGIN_CASES],
    ids=[label for label, _value, _valid in WEB_ORIGIN_CASES],
)
def test_a_malformed_web_origin_is_refused_at_load(
    monkeypatch: pytest.MonkeyPatch, value: str, valid: bool
) -> None:
    """Catches a helper that reads the environment and hands the string
    straight to the CORS middleware.

    The value this field carries is written into a RESPONSE HEADER, so the two
    whitespace rows are the ones that matter most: `http://localhost:3000\\n`
    copied out of a `.env` reaches the middleware with a stray newline, and the
    trailing-slash and path rows are the shapes an author writes from habit -
    a browser's `Origin` header never carries either, so the allowed origin
    silently matches nothing and every browser call fails with no server error.

    Deleting the whitespace clause leaves the three whitespace rows green under
    no other check (they parse cleanly), and deleting the
    path/query/fragment clause leaves four rows that `urlsplit` is perfectly
    happy with, so no row here is caught by a neighbour.
    """
    _set_env_without_web_origin(monkeypatch)
    monkeypatch.setenv("WEB_ORIGIN", value)

    if valid:
        assert web_origin_from_env() == value
    else:
        with pytest.raises(ValueError, match="WEB_ORIGIN"):
            web_origin_from_env()


def test_the_settings_field_and_the_helper_share_one_validator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a diff that validates only in `web_origin_from_env` and leaves
    `Settings.web_origin` as a plain `str` field.

    The origin rule lives in one place; without a `field_validator` on the
    field, every service that builds full `Settings` (rather than calling the
    import-time helper) accepts a malformed origin, and the malformed value
    reaches whatever reads `settings.web_origin` later. The kwarg is passed
    directly rather than through the environment so the FIELD's own validation
    path is the only thing under test - an env read could be answered by the
    helper's check and pass on its neighbour's behalf.
    """
    _clear_env(monkeypatch)

    with pytest.raises(ValidationError) as excinfo:
        Settings(
            _env_file=None,
            database_url=VALID_ENV["DATABASE_URL"],
            redis_url=VALID_ENV["REDIS_URL"],
            qdrant_url=VALID_ENV["QDRANT_URL"],
            jwt_secret=VALID_ENV["JWT_SECRET"],
            web_origin="http://a/",
        )

    failed_fields = {str(err["loc"][0]).upper() for err in excinfo.value.errors()}
    assert "WEB_ORIGIN" in failed_fields


# --- POLICY_DIR, the bring-your-own policies root -------
#
# `POLICY_DIR` is not in `ENV_NAMES` for `MCP_URL`'s and `WEB_ORIGIN`'s reason:
# it carries a default, so `_set_env` must not clear it for the cases above,
# and `test_missing_required_value_fails_validation` asserts every name in that
# tuple is REQUIRED, which `policy_dir` is not. These cases clear it
# themselves, so a developer with `POLICY_DIR` exported in their own shell
# cannot make the default row pass or fail by accident.

POLICY_DIR_DEFAULT = Path("policies")
"""The default the field declares, written out rather than imported.

importing the field's default and asserting it equals itself
pins nothing, and this path is the corpus root every unconfigured ingest run
walks."""


def _set_env_without_policy_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, VALID_ENV)
    monkeypatch.delenv("POLICY_DIR", raising=False)


def test_the_default_policy_dir_is_exactly_the_planned_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches an implementation whose unconfigured default is anything but the
    repository's own corpus directory - an absolute path off the author's
    machine, `policies/` with a trailing separator that makes the walked
    `{tenant}/{version}` path differ, or a required field with no default at
    all, which would make every existing service fail to build `Settings`
    because nobody exports `POLICY_DIR` today.
    """
    _set_env_without_policy_dir(monkeypatch)

    assert Settings(_env_file=None).policy_dir == POLICY_DIR_DEFAULT


def test_a_policy_dir_of_one_character_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a validator that refuses the smallest usable root.

    This is the inclusive minimum boundary of the length gate: one character
    passes, zero does not (the case below). A validator written as "must be
    longer than one character", or one that requires a separator in the value,
    is red here while every longer path it is tried against stays green, so
    only this row can catch it. An adopter running the ingest from inside their
    own policies checkout gives exactly `.`.
    """
    _set_env_without_policy_dir(monkeypatch)
    monkeypatch.setenv("POLICY_DIR", ".")

    assert Settings(_env_file=None).policy_dir == Path(".")


def test_an_empty_policy_dir_is_refused_at_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a plain `policy_dir: Path` field with no validator at all.

    `Path("")` is a legal `Path` that pydantic builds without complaint and
    that compares equal to `Path(".")`, so an empty `POLICY_DIR=` line in an
    adopter's `.env` would silently redirect the whole corpus walk to the
    process's working directory instead of failing at startup. Nothing else in
    the load path refuses it: the type is satisfied, and the walk itself only
    reports `CorpusDirectoryNotFoundError` much later, per tenant.
    """
    _set_env_without_policy_dir(monkeypatch)
    monkeypatch.setenv("POLICY_DIR", "")

    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)

    failed_fields = {str(err["loc"][0]).upper() for err in excinfo.value.errors()}
    assert "POLICY_DIR" in failed_fields


# Every row here is a value `Path` accepts happily, and every one differs from
# the accepted `"policies"` only by whitespace at an END. No other clause of
# the field can decide them: they are non-empty, longer than one character and
# contain no interior space, so deleting the surrounding-whitespace clause is
# the only edit that turns them green (a rejection table
# applied to a filesystem path).
POLICY_DIR_WHITESPACE_CASES: tuple[tuple[str, str], ...] = (
    ("leading space", " policies"),
    ("trailing space", "policies "),
    ("trailing newline", "policies\n"),
    ("trailing CRLF", "policies\r\n"),
    ("leading tab", "\tpolicies"),
)


@pytest.mark.parametrize(
    "value",
    [value for _label, value in POLICY_DIR_WHITESPACE_CASES],
    ids=[label for label, _value in POLICY_DIR_WHITESPACE_CASES],
)
def test_a_policy_dir_with_surrounding_whitespace_is_refused_at_load(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Catches a validator that checks only for emptiness, and a field that
    silently `strip()`s instead of refusing.

    A value pasted into `.env` carries the trailing newline or the stray space
    the editor left, and `Path("policies\\n")` is a real directory name that no
    filesystem holds, so the failure surfaces as a corpus walk finding nothing
    rather than as a bad configuration value. The two rows that matter most are
    the newline and the CRLF: they are invisible in the file that produced
    them. Every row also proves the refusal is not a quiet normalisation, since
    a stripping implementation returns a valid `Settings` and raises nothing.
    """
    _set_env_without_policy_dir(monkeypatch)
    monkeypatch.setenv("POLICY_DIR", value)

    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)

    failed_fields = {str(err["loc"][0]).upper() for err in excinfo.value.errors()}
    assert "POLICY_DIR" in failed_fields


def test_a_policy_dir_with_an_interior_space_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a validator copied from `WEB_ORIGIN`, which refuses whitespace
    ANYWHERE in the value.

    The two are split deliberately: `WEB_ORIGIN` lands in a response
    header where any space is a defect, but this is a filesystem path and
    `/Users/me/My Policies` is a directory an adopter really has. This is the
    only row of the whole `POLICY_DIR` table that separates the two rules - the
    refused rows above pass under either one, so without this case the
    stricter, wrong validator is green everywhere.
    """
    _set_env_without_policy_dir(monkeypatch)
    monkeypatch.setenv("POLICY_DIR", "my policies")

    assert Settings(_env_file=None).policy_dir == Path("my policies")


# --- MCP_ALLOWED_HOSTS, what the transport matches Host against ------
#
# `MCP_ALLOWED_HOSTS` is not in `ENV_NAMES` for the reason `MCP_URL`,
# `WEB_ORIGIN` and `POLICY_DIR` are not: it carries a default, so `_set_env`
# must not clear it for the cases above, and
# `test_missing_required_value_fails_validation` asserts every name in that
# tuple is REQUIRED, which `mcp_allowed_hosts` is not. These cases clear it
# themselves, so a developer with `MCP_ALLOWED_HOSTS` exported in their own
# shell cannot make the default row pass or fail by accident.

MCP_ALLOWED_HOSTS_DEFAULT = [
    "127.0.0.1:*",
    "localhost:*",
    "[::1]:*",
    "helpdesk-mcp-server:*",
]
"""The default the field and `mcp_allowed_hosts_from_env` share, written out.

importing `DEFAULT_MCP_ALLOWED_HOSTS` and asserting it equals
itself pins nothing. These four values are exactly what the MCP transport's
DNS-rebinding middleware matches an incoming `Host` header against when the
environment says nothing, so this list decides whether the shipped compose
stack can reach the server at all."""

# The DNS name limit, and the boundary that is inclusive. The
# check is on the HOST PART of an entry, not on the whole entry: a pattern
# carries a `:*` or a `:<port>` suffix that no DNS name contains, so measuring
# the whole string would refuse a legal 253-byte name written as `<name>:*`.
MCP_ALLOWED_HOST_MAX_BYTES = 253


def _set_env_without_mcp_allowed_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, VALID_ENV)
    monkeypatch.delenv("MCP_ALLOWED_HOSTS", raising=False)


def test_the_default_mcp_allowed_hosts_are_exactly_the_expected_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches an implementation whose unconfigured default is the SDK's own
    loopback three - `127.0.0.1:*`, `localhost:*`, `[::1]:*` and nothing else -
    which is a real failure: agent-api dials
    `helpdesk-mcp-server:8081` inside compose, the middleware matches that
    header against a list the service name is not in, and every call is
    answered 421 before any project code runs.

    Asserted as an exact ORDERED list of four values, not as a membership
    check, so it also catches a widening default: `*`, a bare `*:*` or an extra
    hostname added later would leave a `"helpdesk-mcp-server:*" in ...` check
    green while the protection was effectively off. The list
    is compared to a literal written out here rather than to the imported
    constant, so the constant CHANGING is what turns this red.

    The second assertion reads `mcp_allowed_hosts_from_env` with the variable
    unset, so it also catches a helper that does not share the field's default
    (the point): a helper defaulting to `[]` or to its own literal is
    red here even though `DEFAULT_MCP_ALLOWED_HOSTS` is right, and an empty
    allow-list refuses every host including the loopback ones.
    """
    _set_env_without_mcp_allowed_hosts(monkeypatch)

    assert DEFAULT_MCP_ALLOWED_HOSTS == MCP_ALLOWED_HOSTS_DEFAULT
    assert mcp_allowed_hosts_from_env() == MCP_ALLOWED_HOSTS_DEFAULT
    assert Settings(_env_file=None).mcp_allowed_hosts == MCP_ALLOWED_HOSTS_DEFAULT


def test_an_empty_mcp_allowed_hosts_is_refused_at_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches an implementation that reads `MCP_ALLOWED_HOSTS=` as "nothing
    configured" and falls back to the default, and equally one that accepts the
    empty list as configuration.

    Both are wrong in the same direction: an empty
    allow-list is not a disabled protection, it is a protection that matches
    NOTHING, so `_validate_host` returns False for every header and the server
    answers 421 to its own loopback callers too. An `os.environ.get(name,
    DEFAULT)` helper hands back the empty string here, because the variable IS
    set, and a `.split(",")` of it yields `[""]` - an entry no host can equal.
    Nothing else in the load path refuses it: `list[str]` is satisfied.

    The message must name the variable so the operator who wrote the empty line
    is told which line, which is the requirement.
    """
    _set_env_without_mcp_allowed_hosts(monkeypatch)
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "")

    with pytest.raises(ValueError, match="MCP_ALLOWED_HOSTS"):
        mcp_allowed_hosts_from_env()


# Every row is a value a `list[str]` field accepts happily, and each differs
# from an accepted pattern only by whitespace. No other clause can decide them:
# they are non-empty, and their host parts are far under 253 bytes, so deleting
# the whitespace clause is the only edit that turns them green.
MCP_ALLOWED_HOSTS_WHITESPACE_CASES: tuple[tuple[str, str], ...] = (
    ("trailing newline", "helpdesk-mcp-server:*\n"),
    ("trailing CRLF", "helpdesk-mcp-server:*\r\n"),
    ("trailing space", "helpdesk-mcp-server:* "),
    ("leading space", " helpdesk-mcp-server:*"),
    ("an interior space", "helpdesk mcp server:*"),
    ("a tab inside", "helpdesk\tmcp-server:*"),
    ("whitespace on the second entry only", "127.0.0.1:*,helpdesk-mcp-server:*\n"),
)


@pytest.mark.parametrize(
    "value",
    [value for _label, value in MCP_ALLOWED_HOSTS_WHITESPACE_CASES],
    ids=[label for label, _value in MCP_ALLOWED_HOSTS_WHITESPACE_CASES],
)
def test_an_mcp_allowed_host_with_whitespace_is_refused_at_load(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Catches a helper that splits the variable on commas and hands the parts
    to `TransportSecuritySettings` unexamined, and equally one that silently
    `strip()`s them.

    The middleware compares a pattern to the request's `Host` header byte for
    byte (exact match, or `startswith(base + ":")` for the `host:*` form), so
    `helpdesk-mcp-server:*\\n` copied out of a `.env` matches no request that
    was ever sent. The failure then surfaces as every call answered 421 in the
    deployed arrangement - the same symptom again, with a configured
    value that LOOKS correct in the file that produced it. The newline and CRLF
    rows are the ones that matter most, because they are invisible there.

    The last row carries the stray byte on the SECOND entry only, so it catches
    a check applied to the joined string or to `parts[0]` rather than per
    entry. A stripping implementation returns a valid list and raises nothing,
    so every row also proves the refusal is not a quiet normalisation.
    """
    _set_env_without_mcp_allowed_hosts(monkeypatch)
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", value)

    with pytest.raises(ValueError, match="MCP_ALLOWED_HOSTS"):
        mcp_allowed_hosts_from_env()


def test_an_mcp_allowed_host_of_exactly_253_bytes_is_accepted_and_254_is_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches an off-by-one on the length gate, in either direction, and a
    gate measuring the WRONG PART of the entry.

    Both sides are in one case because only the pair pins the boundary: a check
    written `> 253` and one written `>= 253` agree on every value but 253
    itself, and 253 is the DNS name limit, so a `>= 253` gate refuses the
    longest legal cluster hostname an operator is entitled to configure
    (the boundary is inclusive). A `> 254` gate accepts a name
    no resolver will answer, which the middleware then matches against nothing.

    Both values are built as `"a" * N + ":*"`, so the entry is two bytes longer
    than the part under test. That is deliberate and is the second kill: a gate
    measuring `len(entry)` rather than the host part refuses the 253-byte row
    (255 bytes of entry) and is red here, while a gate measuring only the host
    part is green on it. No other clause can decide either row - both are
    non-empty and carry no whitespace.
    """
    _set_env_without_mcp_allowed_hosts(monkeypatch)

    accepted = "a" * MCP_ALLOWED_HOST_MAX_BYTES + ":*"
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", accepted)
    assert mcp_allowed_hosts_from_env() == [accepted]

    refused = "a" * (MCP_ALLOWED_HOST_MAX_BYTES + 1) + ":*"
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", refused)
    with pytest.raises(ValueError, match="MCP_ALLOWED_HOSTS"):
        mcp_allowed_hosts_from_env()


def test_the_mcp_allowed_hosts_field_and_the_helper_share_one_validator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a diff that validates only in `mcp_allowed_hosts_from_env` and
    leaves `Settings.mcp_allowed_hosts` as a plain `list[str]` field.

    There is one default and one rule, in one place. Without a
    `field_validator` on the field, every service that builds full `Settings`
    (rather than calling the import-time helper `create_app()` needs) accepts a
    malformed pattern, and the two paths drift: the mcp-server refuses a
    trailing newline at startup while agent-api's settings load happily carries
    the same value. The SAME bad value is fed to both paths in one case, so the
    assertion is that they AGREE, not merely that each refuses something.

    The field is given its value as a keyword argument rather than through the
    environment, so the FIELD's own validation path is the only thing under
    test: an env read on this object could be answered by the helper's check
    and pass on its neighbour's behalf.
    """
    _clear_env(monkeypatch)
    bad_value = "helpdesk-mcp-server:*\n"

    monkeypatch.setenv("MCP_ALLOWED_HOSTS", bad_value)
    with pytest.raises(ValueError, match="MCP_ALLOWED_HOSTS"):
        mcp_allowed_hosts_from_env()
    monkeypatch.delenv("MCP_ALLOWED_HOSTS", raising=False)

    with pytest.raises(ValidationError) as excinfo:
        Settings(
            _env_file=None,
            database_url=VALID_ENV["DATABASE_URL"],
            redis_url=VALID_ENV["REDIS_URL"],
            qdrant_url=VALID_ENV["QDRANT_URL"],
            jwt_secret=VALID_ENV["JWT_SECRET"],
            mcp_allowed_hosts=[bad_value],
        )

    failed_fields = {str(err["loc"][0]).upper() for err in excinfo.value.errors()}
    assert "MCP_ALLOWED_HOSTS" in failed_fields
