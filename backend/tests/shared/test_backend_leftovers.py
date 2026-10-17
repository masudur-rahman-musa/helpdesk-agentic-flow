"""An earlier change: the backend leftovers that are import and file shape.

Pure import and filesystem checks: no Postgres, Redis, Qdrant, fixture or
network, so every case here runs in the fast set. The happy halves prove the
new homes resolve; the unhappy halves prove the old names are really gone.
"""

import importlib
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]
PRODUCTION = BACKEND / "helpdesk"

DEAD_RAG_PATH = "helpdesk.rag."
"""The top-level path the code left in an earlier change. The real module is
`helpdesk.mcp_server.services.rag.*`."""


def test_shared_rag_package_is_gone() -> None:
    """Catches a plan that empties `shared/rag/` but leaves its `__init__.py`
    behind: the empty package would keep importing, so only the import error
    proves the folder was deleted."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("helpdesk.shared.rag")
    assert not (PRODUCTION / "shared" / "rag").exists()


def test_the_naming_checks_live_in_core() -> None:
    """Catches a move that copies the naming checks to `shared/core/naming.py`
    but leaves the old `policy_docs/naming.py` in place (two homes, and the
    model layer free to keep importing the service), or a move that drops one
    of the six names the importers read."""
    core = importlib.import_module("helpdesk.shared.core.naming")
    for name in (
        "check_slug",
        "SLUG_LENGTH",
        "MAX_QUERY_CHARS",
        "MAX_TOP_K",
        "COLLECTION_NAME_LENGTH",
        "InvalidPolicyNameError",
    ):
        assert hasattr(core, name), name

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("helpdesk.shared.services.policy_docs.naming")


def test_no_production_source_names_the_dead_rag_path() -> None:
    """Catches a docstring or comment sweep that misses a file: any production
    `.py` still naming `helpdesk.rag.` points a reader at a module that no
    longer exists. Reddens on every stale path, and lists them all."""
    stale = sorted(
        f"{path.relative_to(BACKEND)}:{number}"
        for path in PRODUCTION.rglob("*.py")
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if DEAD_RAG_PATH in line
    )
    assert stale == [], f"stale `{DEAD_RAG_PATH}` paths: {stale}"


@pytest.mark.parametrize(
    ("module", "alias"),
    [
        ("helpdesk.mcp_server.tools.rollback", "RequestNotExecutedError"),
        ("helpdesk.mcp_server.tools.rollback", "RollbackWindowExpiredError"),
        ("helpdesk.mcp_server.tools.killswitch", "TOOL_NAME_MAX_LENGTH"),
        ("helpdesk.shared.core.redis", "_redis"),
    ],
)
def test_the_test_only_aliases_left_production(module: str, alias: str) -> None:
    """Catches a production module that keeps a re-export only so a test can
    read it: the name must be absent from the module, so the tests that need
    it import the real owner instead. One row per alias, so a half-done
    removal names the alias still there."""
    assert not hasattr(importlib.import_module(module), alias)
