"""Static checks on where infra, scripts and docs live.

This file does not touch Postgres, Redis, Qdrant, any fixture or the network.
It only scans files on disk by their path and reads a few config files' text.

What is pinned: `k8s/` lives in `infra/k8s/`; `docker/postgres/` in
`infra/local/postgres/`; `scripts/load_smoke.py` in `infra/load/`;
`build_images.py` in `infra/scripts/`, `test_shard.py` and its timings in
`.github/scripts/`, and the e2e scripts and stub in `backend/scripts/`;
`docs/architecture.md` is root `ARCHITECTURE.md`; `CUSTOMIZING.md` is
`docs/customizing.md`; a root `SECURITY.md` exists. The root allowlist (now
`.github/root-allowlist`) names no old roots and the root check (now
`.github/scripts/check_root.py`) is wired into `.pre-commit-config.yaml` and
`.github/workflows/ci.yml`.

The happy paths prove the locations are present; the unhappy paths prove
the old `k8s/`, `docker/`, `scripts/`, root `CUSTOMIZING.md` and the old
`docs/` files are GONE (no shim left behind), the allowlist names none of the old
roots, and compose and the CI config follow the layout.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_infra_tree() -> None:
    """Kills a move that leaves k8s, the postgres init or the load smoke out of infra."""
    present = [
        REPO_ROOT / "infra" / "k8s" / "helpdesk.yaml",
        REPO_ROOT / "infra" / "k8s" / "kustomization.yaml",
        REPO_ROOT / "infra" / "local" / "postgres" / "init-app-role.sh",
        REPO_ROOT / "infra" / "load" / "load_smoke.py",
    ]
    for path in present:
        assert path.is_file(), f"expected infra file is missing: {path.relative_to(REPO_ROOT)}"


def test_dev_scripts_moved() -> None:
    """Kills a move that drops a script or an e2e stub/fixture from its home."""
    present = [
        REPO_ROOT / "infra" / "scripts" / "build_images.py",
        REPO_ROOT / ".github" / "scripts" / "test_shard.py",
        REPO_ROOT / "backend" / "scripts" / "e2e" / "gateway_stub.py",
        REPO_ROOT / "backend" / "scripts" / "e2e" / "fixtures" / "01-wiki-viewer.json",
    ]
    for path in present:
        assert path.is_file(), f"expected dev script is missing: {path.relative_to(REPO_ROOT)}"


def test_docs_relocated() -> None:
    """Kills a move that leaves the customizing guide or architecture at its old path."""
    present = [
        REPO_ROOT / "docs" / "customizing.md",
        REPO_ROOT / "ARCHITECTURE.md",
    ]
    for path in present:
        assert path.is_file(), f"expected relocated doc is missing: {path.relative_to(REPO_ROOT)}"


def test_root_placeholders_added() -> None:
    """Kills a move that forgets the new root SECURITY.md."""
    present = [
        REPO_ROOT / "SECURITY.md",
    ]
    for path in present:
        assert path.is_file(), (
            f"expected root placeholder is missing: {path.relative_to(REPO_ROOT)}"
        )


def test_old_locations_gone() -> None:
    """Kills a move that leaves a shim at k8s, docker, scripts or an old doc path."""
    gone = [
        REPO_ROOT / "k8s",
        REPO_ROOT / "docker",
        REPO_ROOT / "scripts",
        REPO_ROOT / "CUSTOMIZING.md",
        REPO_ROOT / "docs" / "architecture.md",
        REPO_ROOT / "docs" / "deploy.md",
        REPO_ROOT / "docs" / "runbook.md",
        REPO_ROOT / "docs" / "adr",
    ]
    for path in gone:
        assert not path.exists(), f"old path still present: {path.relative_to(REPO_ROOT)}"


def test_allowlist_has_no_old_roots() -> None:
    """Kills an allowlist that still names an old root."""
    text = (REPO_ROOT / ".github" / "root-allowlist").read_text(encoding="utf-8")
    # Match whole allowlist ENTRIES, not substrings: the header comment names
    # `.github/scripts/check_root.py`, which must not read as a stray `scripts/`.
    entries = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    for old in ["services/", "packages/", "scripts/", "k8s/", "docker/", "project-evals/"]:
        assert old not in entries, f"root-allowlist still names an old root: {old}"


def test_check_root_wired() -> None:
    """Kills a move that forgets to wire check_root.py into pre-commit or CI."""
    pre_commit = (REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    assert "check_root.py" in pre_commit, "check_root.py is not wired into .pre-commit-config.yaml"
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "check_root.py" in ci, "check_root.py is not wired into .github/workflows/ci.yml"


def test_compose_mounts_new_postgres_path() -> None:
    """Kills a compose still mounting docker/postgres instead of infra/local/postgres."""
    text = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "infra/local/postgres/" in text, (
        "docker-compose.yml does not mount infra/local/postgres/"
    )
    assert "docker/postgres/" not in text, (
        "docker-compose.yml still mounts the old docker/postgres/ path"
    )
