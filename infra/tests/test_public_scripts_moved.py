"""The public scripts live outside `dev/`, and every reference follows them.

`dev/` is stripped before the repo goes public, so anything CI, the `Makefile`,
Playwright or a test runs must not sit there. This file reads files, runs
`git ls-files` and imports a few modules. It touches no service and no network.

While the move is pending, the "new home" cases are red on purpose.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SELF = Path(__file__).resolve()

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

DEV_SCRIPTS_KEPT = [
    "dev/scripts/auto_lock.py",
    "dev/scripts/chat_probe.py",
    "dev/scripts/chat_probe_85.json",
    "dev/scripts/chat_probe_86.json",
    "dev/scripts/chat_probe_scenarios.json",
    "dev/scripts/env_keys.py",
    "dev/scripts/git-pre-push-guard.sh",
]

# A path under the old folder that names something that moved, or the old
# dotted import. `env_keys.py` and the other kept tools are not matched.
OLD_PATH = re.compile(
    r"dev/scripts/(?:check_root|test_shard|test_durations|build_images"
    r"|e2e_reset|e2e_accounts|e2e)\b|dev\.scripts\."
)


def find_old_paths(text: str) -> list[str]:
    """Every old-path hit in `text`."""
    return [m.group(0) for m in OLD_PATH.finditer(text)]


def _git_files(*args: str) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [line for line in out.splitlines() if line]


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


def test_dev_scripts_keeps_only_build_process_tools() -> None:
    """Catches a move that copies instead of moving, leaving a public tool behind in
    dev/scripts, or one that takes a build-process tool out with it."""
    assert sorted(_git_files("dev/scripts")) == DEV_SCRIPTS_KEPT


def test_no_tracked_file_outside_dev_names_an_old_path() -> None:
    """Catches a reference (Makefile, workflow, hook, doc, import) still pointing at the
    old dev/scripts home of a moved file."""
    hits: dict[str, list[str]] = {}
    for rel in _git_files():
        if rel.startswith("dev/") or (REPO_ROOT / rel).resolve() == SELF:
            continue
        path = REPO_ROOT / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        found = find_old_paths(text)
        if found:
            hits[rel] = found
    assert hits == {}, f"old paths still named: {hits}"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("run dev/scripts/e2e_reset.py now", ["dev/scripts/e2e_reset"]),
        ("from dev.scripts.build_images import x", ["dev.scripts."]),
        ("dev/scripts/e2e/gateway_stub.py", ["dev/scripts/e2e"]),
        ("dev/scripts/check_root.py", ["dev/scripts/check_root"]),
        ("dev/scripts/env_keys.py", []),
        ("dev/scripts/auto_lock.py", []),
        ("backend/scripts/e2e_reset.py", []),
    ],
)
def test_the_old_path_finder_flags_a_planted_reference(text: str, expected: list[str]) -> None:
    """Catches a finder that never matches (so the repo-wide sweep passes for nothing)
    or one so wide it also flags the tools that stay in dev/scripts."""
    assert find_old_paths(text) == expected


def test_every_tool_file_names_the_new_paths() -> None:
    """Catches a tool file (Makefile, CI, pre-commit, hooks, Playwright) that was not
    pointed at the new home, or that names a path that does not exist."""
    # (file, pattern that must match, path that must exist). Patterns refuse a
    # leading `dev/`, so the old path cannot satisfy them.
    checks = [
        ("Makefile", r"(?<!dev/)scripts/e2e_reset\.py", "backend/scripts/e2e_reset.py"),
        ("Makefile", r"(?<!dev/)scripts/e2e_accounts\.py", "backend/scripts/e2e_accounts.py"),
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
    settings = json.loads(_read(".claude/settings.json"))
    commands = [
        hook["command"]
        for group in settings["hooks"]["PreToolUse"]
        for hook in group["hooks"]
        if "check_root.py" in hook.get("command", "")
    ]
    if len(commands) != 2:
        problems.append(f"expected 2 root hook commands, found {len(commands)}")
    problems.extend(
        f"hook does not name .github/scripts/check_root.py: {c}"
        for c in commands
        if ".github/scripts/check_root.py" not in c
    )
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


def test_the_root_hook_blocks_a_stray_root_file_from_its_new_home() -> None:
    """Catches a root hook that was moved but not working (wrong root, or a message
    still naming the old dev/scripts path), so strays reach the repo root unblocked."""
    payload = {"tool_name": "Write", "tool_input": {"file_path": str(REPO_ROOT / "stray.txt")}}
    result = subprocess.run(
        ["python3", ".github/scripts/check_root.py", "--claude-hook"],
        cwd=REPO_ROOT,
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env={**os.environ, "CLAUDE_PROJECT_DIR": str(REPO_ROOT)},
        check=False,
    )
    assert result.returncode == 2, result.stderr
    # A missing script also exits 2 and names its path, so the hook's own words are pinned.
    assert "BLOCKED by .github/scripts/check_root.py" in result.stderr


def test_moved_backend_scripts_carry_no_internal_references() -> None:
    """Catches a move into backend/scripts that leaves comments naming plans, tests or
    dev/ paths, which the backend comment check rejects."""
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
