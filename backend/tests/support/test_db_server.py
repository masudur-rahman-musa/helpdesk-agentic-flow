"""An earlier change: the suite uses its own Postgres server, on port 5433.

``tests/support/db_server.py`` is not written yet, so this file is red until it
exists. It must expose ``test_database_url(environ)``, ``check_server_name(name)``
and ``DEFAULT_TEST_DATABASE_URL``.

Only ``test_the_suite_talks_to_the_test_server`` reaches a service (`db`): only
the real server can report its own ``cluster_name``. The rest read plain values.
"""

import asyncio
import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from helpdesk.shared.core.config import load_settings
from tests.support import db_server

# Called through the module: `test_database_url` imported by name would be
# collected by pytest as a test.
check_server_name = db_server.check_server_name
url_for = db_server.test_database_url

DEFAULT_URL = "postgresql+asyncpg://helpdesk:change-me@localhost:5433/helpdesk_test"
"""The exact fallback the plan names. Written out here so a changed port or
password in the module cannot slide past by being compared with itself."""


def test_the_test_url_comes_from_TEST_DATABASE_URL() -> None:
    """Catches an implementation that ignores TEST_DATABASE_URL and always
    returns the default, so CI or a laptop with another port cannot move it."""
    url = "postgresql+asyncpg://u:p@localhost:6000/x"
    assert url_for({"TEST_DATABASE_URL": url}) == url


def test_an_unset_test_url_means_port_5433() -> None:
    """Catches a default that points at the app server (port 5432), or at any
    other host, port, login or database than the test server's."""
    assert url_for({}) == DEFAULT_URL


def test_the_default_constant_is_the_5433_url() -> None:
    """Catches a DEFAULT_TEST_DATABASE_URL that drifts from the plan's value
    while the function still returns it, which the two tests above cannot tell
    apart from a matching change in both."""
    assert db_server.DEFAULT_TEST_DATABASE_URL == DEFAULT_URL


def test_an_empty_test_url_means_port_5433() -> None:
    """Catches an implementation using `environ.get(name, default)`, which hands
    back an empty URL when the variable is set but empty (a CI step that exports
    an unset secret)."""
    assert url_for({"TEST_DATABASE_URL": ""}) == DEFAULT_URL


def test_the_check_accepts_the_helpdesk_test_server() -> None:
    """Catches a check that refuses everything, which would fail the suite on the
    right server too. check_server_name raises on a bad name and returns nothing,
    so reaching the next line is the proof."""
    check_server_name("helpdesk-test")


@pytest.mark.parametrize(
    "name",
    [
        "",
        "helpdesk",
        "helpdesk-test\n",
        "helpdesk-test\r\n",
        "helpdesk-test ",
        " helpdesk-test",
        "Helpdesk-Test",
    ],
    ids=["empty", "helpdesk", "newline", "crlf", "trailing-space", "leading-space", "case"],
)
def test_the_check_refuses_any_other_server(name: str) -> None:
    """Catches a check that lets the app server through (its cluster_name is
    empty by default) or one that compares loosely (a prefix, strip(), casefold
    or a `$`-anchored regex that lets a trailing newline pass). The message must
    name TEST_DATABASE_URL and port 5433 so the reader knows the fix."""
    with pytest.raises(RuntimeError) as raised:
        check_server_name(name)
    message = str(raised.value)
    assert "TEST_DATABASE_URL" in message
    assert "5433" in message


def test_the_suite_settings_name_the_test_url() -> None:
    """Catches a conftest that never points DATABASE_URL at the test server, so
    `load_settings()` (the `pg` fixture, the route tests) still lands on the
    app's server at 5432 and a role change reaches the running app."""
    assert load_settings().database_url == url_for(os.environ)


@pytest.mark.db
def test_the_suite_talks_to_the_test_server(pg: object) -> None:
    """Catches a suite connected to any server but the test one (the app server
    reports an empty cluster_name): only the server itself can say its name.
    Depends on `pg` so an unreachable server fails loudly, not as a skip."""
    admin_url = load_settings().database_url

    async def name() -> str:
        engine = create_async_engine(admin_url)
        try:
            async with engine.connect() as conn:
                result = await conn.execute(text("SELECT current_setting('cluster_name')"))
                return str(result.scalar_one())
        finally:
            await engine.dispose()

    assert asyncio.run(name()) == "helpdesk-test"
