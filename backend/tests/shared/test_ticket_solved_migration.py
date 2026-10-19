"""The ticket-solved migration: its place in the chain.

The migration file's name carries a generated revision id, so it is found by
the `_ticket_solved_columns.py` suffix. No database is needed: the file is
loaded and its revision pointers are read.
"""

import importlib.util
from pathlib import Path
from types import ModuleType

VERSIONS = Path(__file__).resolve().parents[2] / "migrations" / "versions"
PREVIOUS_HEAD = "b7e3d91a4c2f"


def _migration() -> ModuleType:
    matches = sorted(VERSIONS.glob("*_ticket_solved_columns.py"))
    assert len(matches) == 1, f"expected one ticket_solved_columns migration, got {matches}"
    spec = importlib.util.spec_from_file_location("ticket_solved_columns_migration", matches[0])
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_sits_on_the_previous_head() -> None:
    """Catches a migration chained to the wrong parent: a second head, or one
    that skips the readable-numbers migration, makes `alembic upgrade head`
    fail or run out of order."""
    assert _migration().down_revision == PREVIOUS_HEAD
