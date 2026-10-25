"""The suite's own Postgres server: its URL, and a check that it is the one.

Postgres roles belong to the whole server, so a test that changes the
`helpdesk_app` password on the app's server breaks the running app's login.
The suite therefore talks only to the compose `postgres-test` service on port
5433, which names itself `helpdesk-test` through `cluster_name`. Pinned by
``tests/support/test_db_server.py``.
"""

from collections.abc import Mapping

DEFAULT_TEST_DATABASE_URL = "postgresql+asyncpg://helpdesk:change-me@localhost:5433/helpdesk_test"

TEST_SERVER_NAME = "helpdesk-test"


def test_database_url(environ: Mapping[str, str]) -> str:
    """`TEST_DATABASE_URL` when set and not empty, else the 5433 default."""
    return environ.get("TEST_DATABASE_URL") or DEFAULT_TEST_DATABASE_URL


# A helper, not a test, though its name starts with `test_`.
test_database_url.__test__ = False  # type: ignore[attr-defined]


def check_server_name(name: str) -> None:
    """Refuse any server whose `cluster_name` is not exactly `helpdesk-test`."""
    if name != TEST_SERVER_NAME:
        raise RuntimeError(
            f"The test suite reached a Postgres server named {name!r}, not "
            f"{TEST_SERVER_NAME!r}. Point TEST_DATABASE_URL at the compose "
            "`postgres-test` service on port 5433 (`docker compose up -d`)."
        )
