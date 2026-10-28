"""The scripts that CI, the `Makefile`, Playwright and tests run sit where the
references say they do.

This file reads files and imports a few modules. It touches no service and no
network.
"""

from __future__ import annotations

import importlib
import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]

NEW_HOMES = [
    ".github/scripts/check_root.py",
    ".github/scripts/test_shard.py",
    ".github/scripts/test_durations.json",
    "infra/scripts/build_images.py",
    "backend/scripts/e2e_reset.py",
    "backend/scripts/e2e_accounts.py",
    "backend/scripts/e2e/gateway_stub.py",
    "backend/scripts/e2e/fixtures/01-wiki-viewer.json",
    "backend/scripts/e2e/fixtures/02-gitlab-maintainer.json",
    "backend/scripts/e2e/fixtures/03-payments-initiator.json",
    "backend/scripts/e2e/fixtures/04-offboard.json",
]


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def _load_by_path(alias: str, rel: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(alias, REPO_ROOT / rel)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


def test_each_moved_file_sits_at_its_new_home() -> None:
    """Catches a move that leaves a script, the durations table, the stub or a fixture
    out of its new folder."""
    missing = [rel for rel in NEW_HOMES if not (REPO_ROOT / rel).is_file()]
    assert missing == [], f"missing at the new home: {missing}"


def test_every_tool_file_names_the_new_paths() -> None:
    """Catches a tool file (Makefile, CI, pre-commit, Playwright) that was not
    pointed at the new home, or that names a path that does not exist."""
    # (file, pattern that must match, path that must exist).
    checks = [
        ("Makefile", r"scripts/e2e_reset\.py", "backend/scripts/e2e_reset.py"),
        ("Makefile", r"scripts/e2e_accounts\.py", "backend/scripts/e2e_accounts.py"),
        ("Makefile", r"infra/scripts/build_images\.py", "infra/scripts/build_images.py"),
        (
            ".github/workflows/ci.yml",
            r"\.github/scripts/check_root\.py",
            ".github/scripts/check_root.py",
        ),
        (
            ".github/workflows/ci.yml",
            r"\.github/scripts/test_shard\.py",
            ".github/scripts/test_shard.py",
        ),
        (
            ".pre-commit-config.yaml",
            r"\.github/scripts/check_root\.py",
            ".github/scripts/check_root.py",
        ),
        (
            "web/playwright.config.ts",
            r"backend/scripts/e2e/gateway_stub\.py",
            "backend/scripts/e2e/gateway_stub.py",
        ),
    ]
    problems: list[str] = []
    for rel, pattern, target in checks:
        if not re.search(pattern, _read(rel)):
            problems.append(f"{rel} does not match {pattern}")
        if not (REPO_ROOT / target).is_file():
            problems.append(f"{target} does not exist")
    assert problems == []


def test_moved_scripts_still_find_the_repo_root() -> None:
    """Catches a moved script whose parents[N] still counts folders from the old home,
    so it looks for the repo root, the backend or the durations table in the wrong place."""
    shard = _load_by_path("moved_test_shard", ".github/scripts/test_shard.py")
    build = importlib.import_module("infra.scripts.build_images")
    reset = importlib.import_module("scripts.e2e_reset")
    assert shard.REPO_ROOT == REPO_ROOT
    assert build.REPO_ROOT == REPO_ROOT
    assert reset._REPO_ROOT == REPO_ROOT
    assert shard.DURATIONS == REPO_ROOT / ".github" / "scripts" / "test_durations.json"


def test_moved_backend_scripts_carry_no_internal_references() -> None:
    """Catches a move into backend/scripts that leaves comments naming plans, tests or
    other internal references, which the backend comment check rejects."""
    checker = _load_by_path("moved_plain_comments", "backend/tests/shared/test_plain_comments.py")
    files = [
        "backend/scripts/e2e_reset.py",
        "backend/scripts/e2e_accounts.py",
        "backend/scripts/e2e/gateway_stub.py",
    ]
    hits: dict[str, list[str]] = {}
    for rel in files:
        assert (REPO_ROOT / rel).is_file(), f"{rel} is missing"
        for comment in checker.python_comments(_read(rel)):
            for kind, _ in checker.find_references(comment.text, production=True):
                hits.setdefault(rel, []).append(kind)
    assert hits == {}
