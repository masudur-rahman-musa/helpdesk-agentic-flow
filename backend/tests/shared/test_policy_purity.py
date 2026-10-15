"""`helpdesk.shared.policy` is pure.

The check is static: every .py file under the package is parsed with `ast` and
its Import / ImportFrom nodes inspected. That avoids the false positives a
runtime sys.modules walk would get from transitive stdlib imports, and it fails
even for an import hidden inside a function body.

A second test runs a fresh interpreter that imports only `helpdesk.shared.policy` and
reports which `helpdesk.*` modules that pulled in, so the import graph is
proven and not just the source text.
"""

import ast
import importlib
import pkgutil
import subprocess
import sys
from pathlib import Path

import pytest

import helpdesk.shared.policy

ALLOWED_MODULES: frozenset[str] = frozenset({"enum", "uuid", "collections", "typing", "pydantic"})
"""An allowlist, not a blacklist: a new I/O dependency must be argued for here
rather than slipping past a list of the ones we happened to think of."""

FORBIDDEN_CALLS: frozenset[str] = frozenset({"open", "input", "print", "uuid4", "uuid1", "now"})
"""Impurity that needs no import: reading a file, a clock, or fresh randomness."""

PACKAGE = "helpdesk.shared.policy"
PACKAGE_DIR = Path(str(helpdesk.shared.policy.__file__)).parent
SOURCE_FILES: list[Path] = sorted(PACKAGE_DIR.glob("*.py"))


def _imported_module_names(tree: ast.AST) -> set[str]:
    """Every module a file imports, as dotted names, relative names resolved."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # `from . import x` / `from .types import y`
                base = PACKAGE if node.module is None else f"{PACKAGE}.{node.module}"
                names.add(base)
            elif node.module is not None:
                names.add(node.module)
                names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def test_the_policy_package_has_source_files_to_check() -> None:
    """Guard: the purity tests must not pass vacuously on an empty package."""
    assert PACKAGE_DIR.is_dir()
    assert len(SOURCE_FILES) >= 2
    assert (PACKAGE_DIR / "__init__.py") in SOURCE_FILES
    assert (PACKAGE_DIR / "engine.py") in SOURCE_FILES


@pytest.mark.parametrize("source_file", SOURCE_FILES, ids=[path.name for path in SOURCE_FILES])
def test_no_helpdesk_import_outside_the_policy_package(source_file: Path) -> None:
    tree = ast.parse(source_file.read_text(encoding="utf-8"))

    outside = {
        name
        for name in _imported_module_names(tree)
        if name == "helpdesk" or (name.startswith("helpdesk.") and not name.startswith(PACKAGE))
    }

    assert outside == set(), f"{source_file.name} imports outside {PACKAGE}: {sorted(outside)}"


@pytest.mark.parametrize("source_file", SOURCE_FILES, ids=[path.name for path in SOURCE_FILES])
def test_only_allowlisted_modules_are_imported(source_file: Path) -> None:
    tree = ast.parse(source_file.read_text(encoding="utf-8"))

    unexpected = {
        name
        for name in _imported_module_names(tree)
        if not name.startswith(PACKAGE) and name.split(".")[0] not in ALLOWED_MODULES
    }

    assert unexpected == set(), (
        f"{source_file.name} imports outside the pure allowlist: {sorted(unexpected)}"
    )


@pytest.mark.parametrize("source_file", SOURCE_FILES, ids=[path.name for path in SOURCE_FILES])
def test_no_call_reaches_for_io_a_clock_or_randomness(source_file: Path) -> None:
    """`uuid` is allowed for type annotations, not for minting new ids."""
    tree = ast.parse(source_file.read_text(encoding="utf-8"))

    called: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if isinstance(function, ast.Name):
            called.add(function.id)
        elif isinstance(function, ast.Attribute):
            called.add(function.attr)

    forbidden = called & FORBIDDEN_CALLS

    assert forbidden == set(), f"{source_file.name} is not pure, it calls: {sorted(forbidden)}"


def test_every_submodule_imports_cleanly_and_is_covered_by_the_static_check() -> None:
    submodules = [info.name for info in pkgutil.iter_modules([str(PACKAGE_DIR)])]

    assert submodules, "no submodules found under helpdesk.shared.policy"
    for name in submodules:
        module = importlib.import_module(f"{PACKAGE}.{name}")
        assert Path(str(module.__file__)) in SOURCE_FILES


def test_a_fresh_interpreter_importing_policy_pulls_in_nothing_else_from_helpdesk() -> None:
    code = (
        "import sys, helpdesk.shared.policy, pkgutil, importlib\n"
        "for i in pkgutil.iter_modules(helpdesk.shared.policy.__path__):\n"
        "    importlib.import_module('helpdesk.shared.policy.' + i.name)\n"
        "print('\\n'.join(sorted(m for m in sys.modules if m.startswith('helpdesk'))))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )

    assert completed.returncode == 0, completed.stderr
    loaded = {line for line in completed.stdout.splitlines() if line}
    # A package always loads its parents first, so `helpdesk` and `helpdesk.shared`
    # come with any import of the policy engine; both are empty packages.
    parents = {"helpdesk", "helpdesk.shared"}
    unexpected = {name for name in loaded if name not in parents and not name.startswith(PACKAGE)}

    assert unexpected == set(), f"importing {PACKAGE} loaded {sorted(unexpected)}"


def test_classify_is_exported_from_the_package() -> None:
    module = importlib.import_module(PACKAGE)

    assert hasattr(module, "classify")
    assert module.classify is importlib.import_module(f"{PACKAGE}.engine").classify
