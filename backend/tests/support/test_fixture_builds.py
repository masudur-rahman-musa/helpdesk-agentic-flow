"""The shared session fixtures are built once per run, not once per test file.

pytest builds a session fixture that a test module imports by name once for
that module. So the seeded MCP database was made (and argon2-seeded) up to 38
times. ``tests/conftest.py`` now loads ``tests.support.mcp`` once as a plugin;
these cases keep it that way.

The first case needs the real database, so it carries the ``db`` marker. The
rest read source files as text and need no service.
"""

import re
from pathlib import Path

import pytest

from tests.support import mcp
from tests.support.mcp import SeededMcp

TESTS = Path(__file__).resolve().parents[1]
THIS_FILE = Path(__file__).resolve()

SHARED_FIXTURES = frozenset(
    {
        "seeded_mcp_db",
        "mcp_pg",
        "mcp_redis_base_url",
        "mcp_redis_url",
        "mcp_env",
        "mcp_qdrant",
    }
)
SEED_TESTS = TESTS / "scripts" / "test_seed.py"

_IMPORT_BLOCK = re.compile(
    r"from\s+tests\.support\.mcp\s+import\s+(?:\((?P<many>[^)]*)\)|(?P<one>[^\n]*))"
)
_SEED_CALL = re.compile(r"\bseed_all\s*\(")


def _imported_fixtures(source: str) -> set[str]:
    """Shared fixture names a source imports from ``tests.support.mcp``."""
    found: set[str] = set()
    for match in _IMPORT_BLOCK.finditer(source):
        body = match.group("many") or match.group("one") or ""
        lines = (line.split("#")[0] for line in body.split("\n"))
        names = {part.strip() for line in lines for part in line.split(",")}
        found |= names & SHARED_FIXTURES
    return found


def _test_files() -> list[Path]:
    return sorted(path for path in TESTS.rglob("test_*.py") if path.resolve() != THIS_FILE)


@pytest.mark.db
def test_the_seeded_database_is_built_once_per_run(seeded_mcp_db: SeededMcp) -> None:
    """Kills a fixture imported per file: each import would build it again, and
    the counter would read more than 1 by the time this test runs."""
    assert mcp.SEEDED_DB_BUILDS == 1


def test_no_test_file_imports_a_shared_fixture() -> None:
    """Kills an import such as ``from tests.support.mcp import seeded_mcp_db``,
    which makes pytest build that session fixture again for the importing file."""
    offenders = {
        str(path.relative_to(TESTS)): sorted(found)
        for path in _test_files()
        if (found := _imported_fixtures(path.read_text()))
    }
    assert offenders == {}


def test_an_imported_shared_fixture_is_caught() -> None:
    """Kills a scan that cannot fail: a sample importing the fixture must be flagged."""
    sample = (
        "from tests.support.mcp import (\n"
        "    SeededMcp,\n"
        "    seeded_mcp_db,  # noqa: F401\n"
        "    call_tool,\n"
        ")\n"
    )
    assert _imported_fixtures(sample) == {"seeded_mcp_db"}
    assert _imported_fixtures("from tests.support.mcp import SeededMcp, call_tool\n") == set()


def test_only_the_seed_tests_seed_a_database() -> None:
    """Kills a file that seeds its own database again: argon2 makes each seed slow,
    and a copy of the shared one (``TempDatabaseFactory.clone``) costs milliseconds."""
    seeders = {
        str(path.relative_to(TESTS))
        for path in _test_files()
        if _SEED_CALL.search(path.read_text())
    }
    assert seeders == {str(SEED_TESTS.relative_to(TESTS))}
