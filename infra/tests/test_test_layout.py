"""Static checks that prove an earlier change's target test layout.

This file does not touch Postgres, Redis, Qdrant, any fixture or the network.
It only looks at files on disk and imports a few modules by their package path.

The happy paths prove the NEW names and locations are present (the moved
helpers import, the conftest sits at the tests root, seed lives under backend,
the repo-level tests live in infra). The unhappy paths prove the OLD names and
locations are GONE (no `sys.path.insert` left in backend tests, the old helper
files deleted, the root `scripts/seed.py` removed, no repo-level tests left in
`backend/tests/shared/`).

While the moves are still pending every case is red on purpose.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"


def test_no_sys_path_insert_remains() -> None:
    """Kills a move that leaves `sys.path.insert` hacks in the backend tests."""
    offenders = [
        path
        for path in (BACKEND / "tests").rglob("*.py")
        if "sys.path.insert" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, "sys.path.insert still present in: " + ", ".join(
        str(path.relative_to(REPO_ROOT)) for path in offenders
    )


def test_support_helpers_import_by_package_path() -> None:
    """Kills a move that forgets the new `tests.support.*` package or leaves the old helper files."""
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))

    for name in ("tests.support.mcp", "tests.support.policy", "tests.support.seed"):
        module = importlib.import_module(name)
        assert module.__name__ == name

    old_files = [
        BACKEND / "tests" / "shared" / "_seed.py",
        BACKEND / "tests" / "shared" / "_policy.py",
        BACKEND / "tests" / "mcp_server" / "mcp_fixtures.py",
    ]
    still_present = [path for path in old_files if path.exists()]
    assert not still_present, "old helper files still present: " + ", ".join(
        str(path.relative_to(REPO_ROOT)) for path in still_present
    )


def test_conftest_sits_at_tests_root() -> None:
    """Kills a move that leaves conftest under tests/shared instead of at the tests root."""
    assert (BACKEND / "tests" / "conftest.py").exists()
    assert not (BACKEND / "tests" / "shared" / "conftest.py").exists()


def test_seed_moved_under_backend() -> None:
    """Kills a move that leaves seed.py at the repo root instead of under backend/scripts."""
    assert not (REPO_ROOT / "scripts" / "seed.py").exists()
    assert (BACKEND / "scripts" / "seed.py").exists()

    for root in (str(REPO_ROOT), str(BACKEND)):
        if root not in sys.path:
            sys.path.insert(0, root)

    seed = importlib.import_module("scripts.seed")
    seed_file = seed.__file__
    assert seed_file is not None
    assert Path(seed_file).resolve().is_relative_to(BACKEND / "scripts")


def test_repo_level_tests_live_in_infra() -> None:
    """Kills a move that leaves the repo-level tests under backend/tests/shared instead of infra/tests."""
    repo_level_tests = [
        "test_build_images_args.py",
        "test_build_workflow.py",
        "test_container_build.py",
        "test_docs.py",
        "test_e2e_reset_guard.py",
        "test_security_config.py",
        "test_smoke.py",
        "test_template_packaging.py",
        "test_test_shard.py",
    ]

    missing_in_infra = [
        name
        for name in repo_level_tests
        if not (REPO_ROOT / "infra" / "tests" / name).exists()
    ]
    assert not missing_in_infra, (
        "repo-level tests missing from infra/tests: " + ", ".join(missing_in_infra)
    )

    still_in_backend = [
        name
        for name in repo_level_tests
        if (BACKEND / "tests" / "shared" / name).exists()
    ]
    assert not still_in_backend, (
        "repo-level tests still under backend/tests/shared: "
        + ", ".join(still_in_backend)
    )
