"""The shared-only rule the restructure adds is enforced by import-linter. `backend/.importlinter` declares two contracts: the three apps are
independent of each other, and the shared library imports no app.

The happy path runs the real config and expects it kept. The unhappy path runs
a config that declares a contract the code is known to break, so the check
proves it can FAIL, not just that today's code happens to be clean. Both shell
out to `lint-imports` the way CI does, from the backend/ project root.

Pure: builds the import graph from the installed package; no backing service.
"""

import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
IMPORTLINTER = BACKEND / ".importlinter"


LINT_IMPORTS = Path(sys.executable).parent / "lint-imports"


def _lint_imports(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(LINT_IMPORTS), *args],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        check=False,
    )


def test_import_contracts_pass() -> None:
    """`lint-imports` over backend/.importlinter succeeds: each app imports only
    helpdesk.shared and the shared library imports no app."""
    assert IMPORTLINTER.is_file(), f"missing contract file: {IMPORTLINTER}"
    result = _lint_imports()
    assert result.returncode == 0, f"lint-imports failed:\n{result.stdout}\n{result.stderr}"
    assert "Contracts: 3 kept, 0 broken." in result.stdout


def test_audit_has_no_deferred_import() -> None:
    """After an earlier change: anchor.py has no `import` inside a function body (the
    cycle is removed, not hidden), and the module loads cleanly."""
    import ast
    import importlib

    anchor_src = (BACKEND / "helpdesk" / "shared" / "audit" / "anchor.py").read_text()
    tree = ast.parse(anchor_src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                if isinstance(child, (ast.Import, ast.ImportFrom)):
                    raise AssertionError(
                        f"deferred import found in function {node.name!r} "
                        f"at line {child.lineno} of anchor.py"
                    )
    # The module itself must load without error (no cycle).
    mod = importlib.import_module("helpdesk.shared.audit.anchor")
    assert mod is not None


def test_apps_do_not_import_each_other(tmp_path: Path) -> None:
    """The mechanism really catches a violation. A throwaway config forbids
    helpdesk.agent_api from importing helpdesk.shared, which it does, so
    lint-imports must report the contract broken and exit non-zero. If this
    passed, the kept result above would prove nothing."""
    config = tmp_path / ".importlinter"
    config.write_text(
        "[importlinter]\n"
        "root_package = helpdesk\n\n"
        "[importlinter:contract:deliberately-broken]\n"
        "name = agent_api must not import shared (it does)\n"
        "type = forbidden\n"
        "source_modules =\n"
        "    helpdesk.agent_api\n"
        "forbidden_modules =\n"
        "    helpdesk.shared\n"
    )
    result = _lint_imports("--config", str(config))
    assert result.returncode != 0, f"a broken contract was not caught:\n{result.stdout}"
    assert "BROKEN" in result.stdout, result.stdout
