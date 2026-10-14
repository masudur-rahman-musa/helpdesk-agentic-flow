"""The restructure landed: one `backend/` project, one `helpdesk` package with
the four subpackages, and nothing left at the old roots.

Pure import and filesystem checks: no Postgres, Redis, Qdrant, fixture or
network. The happy path proves the new names resolve; the unhappy path proves
the old ones are gone, so a stray re-export stub or a forgotten `services/`
folder is red here rather than passing quietly.
"""

import importlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND = Path(__file__).resolve().parents[2]

NEW_PACKAGES: tuple[str, ...] = (
    "helpdesk",
    "helpdesk.shared",
    "helpdesk.agent_api",
    "helpdesk.mcp_server",
    "helpdesk.scheduler",
)
"""The one package and its four subpackages, all under backend/helpdesk/."""

OLD_ROOTS: tuple[str, ...] = ("services", "packages", "policies", "project-evals", "alembic")
"""The top-level folders the move emptied. None may survive as even a shell."""

OLD_IMPORTS: tuple[str, ...] = (
    "agent_api",
    "mcp_server",
    "scheduler",
    "helpdesk.auth",
    "helpdesk.policy",
    "helpdesk.rag",
)
"""Import names from the old layout. The three apps were top-level packages;
`helpdesk.auth`/`policy`/`rag` were the shared library before it moved under
`helpdesk.shared`. All must now fail to import."""


def test_apps_import_under_new_names() -> None:
    """Every new package resolves. Imports `helpdesk.agent_api` and the rest,
    which pulls each app's package __init__, so a half-moved package is red."""
    for name in NEW_PACKAGES:
        module = importlib.import_module(name)
        assert module.__name__ == name


def test_old_roots_are_gone() -> None:
    """The unhappy path: the old folders are deleted and the old import names
    no longer resolve, so no compatibility shim or re-export stub stayed behind.
    """
    surviving = [name for name in OLD_ROOTS if (REPO_ROOT / name).exists()]
    assert surviving == [], f"old top-level folders still present: {surviving}"

    importable = []
    for name in OLD_IMPORTS:
        try:
            importlib.import_module(name)
            importable.append(name)
        except ModuleNotFoundError:
            continue
    assert importable == [], f"old import names still resolve: {importable}"


def test_the_package_lives_under_backend() -> None:
    """The source really sits at backend/helpdesk, not at an old location that
    happens to be on the path."""
    shared = importlib.import_module("helpdesk.shared")
    assert Path(str(shared.__file__)).resolve().is_relative_to(BACKEND / "helpdesk" / "shared")
