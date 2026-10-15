"""Static checks that prove an earlier change's last folder-restructure move.

This file does not touch Postgres, Redis, Qdrant, any fixture or the network.
It only scans files on disk by their path and reads a few config files' text.

An earlier change (restructure An earlier change) finishes the move: `k8s/` goes to `infra/k8s/`;
`docker/postgres/` goes to `infra/local/postgres/`; `scripts/load_smoke.py`
goes to `infra/load/`; the dev-only scripts (`build_images.py`,
`select_tests.py`, `test_shard.py`, `test_durations.json`, `e2e_reset.py`,
`e2e_accounts.py`, `git-pre-push-guard.sh`, and the e2e stub plus its four
fixtures) go to `dev/scripts/`; the ADRs `docs/adr/*` go to the project notes;
`docs/runbook.md`, `telemetry.md`, `threat-model.md` and `deploy.md` go to
the project notes; `docs/architecture.md` becomes root `ARCHITECTURE.md`;
`CUSTOMIZING.md` becomes `docs/customizing.md`; new root `AGENTS.md` and
`SECURITY.md` are added. Last, the TEMPORARY block of `.claude/root-allowlist`
is deleted and `dev/scripts/check_root.py` is wired into
`.pre-commit-config.yaml` and `.github/workflows/ci.yml`.

The happy paths prove the NEW locations are present; the unhappy paths prove
the OLD `k8s/`, `docker/`, `scripts/`, root `CUSTOMIZING.md` and the old
`docs/` files are GONE (no shim left behind), the allowlist's TEMPORARY block
is removed, and compose and the CI config follow the move.

While the move is still pending every "present" case is red on purpose.
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
    """Kills a move that drops a dev script or an e2e stub/fixture from dev/scripts."""
    present = [
        REPO_ROOT / "dev" / "scripts" / "build_images.py",
        REPO_ROOT / "dev" / "scripts" / "test_shard.py",
        REPO_ROOT / "dev" / "scripts" / "e2e" / "gateway_stub.py",
        REPO_ROOT / "dev" / "scripts" / "e2e" / "fixtures" / "01-wiki-viewer.json",
    ]
    for path in present:
        assert path.is_file(), f"expected dev script is missing: {path.relative_to(REPO_ROOT)}"


def test_docs_relocated() -> None:
    """Kills a move that leaves an ADR, an ops doc or architecture at its old path."""
    present = [
        REPO_ROOT / "dev" / "adr" / "0001-langgraph-orchestration-only.md",
        REPO_ROOT / "dev" / "ops" / "runbook.md",
        REPO_ROOT / "dev" / "ops" / "telemetry.md",
        REPO_ROOT / "dev" / "ops" / "threat-model.md",
        REPO_ROOT / "dev" / "ops" / "deploy.md",
        REPO_ROOT / "docs" / "customizing.md",
        REPO_ROOT / "ARCHITECTURE.md",
    ]
    for path in present:
        assert path.is_file(), f"expected relocated doc is missing: {path.relative_to(REPO_ROOT)}"


def test_root_placeholders_added() -> None:
    """Kills a move that forgets the new root AGENTS.md and SECURITY.md."""
    present = [
        REPO_ROOT / "AGENTS.md",
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


def test_temporary_allowlist_removed() -> None:
    """Kills an allowlist that still carries the TEMPORARY block or the old roots."""
    text = (REPO_ROOT / ".claude" / "root-allowlist").read_text(encoding="utf-8")
    assert "TEMPORARY" not in text, "root-allowlist still has a TEMPORARY marker"
    # Match whole allowlist ENTRIES, not substrings: the header comment names
    # `dev/scripts/check_root.py`, which must not read as a stray `scripts/`.
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
