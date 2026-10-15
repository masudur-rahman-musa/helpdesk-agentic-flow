"""Static checks that prove an earlier change's single-app code moves.

This file does not touch Postgres, Redis, Qdrant, any fixture or the network.
It only scans files on disk by their path.

An earlier change moves the code that only one app reads down into that app's tree, and
keeps the code two apps read in `shared/`. The happy paths prove the NEW
locations under `agent_api`, `mcp_server` and `scheduler` are present. The
unhappy paths prove the OLD `shared/` files that got moved are GONE, and that
nothing that stayed in `shared/` was also copied under an app tree.

While the moves are still pending every case is red on purpose.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"


def test_gateway_and_tracing_under_agent_api() -> None:
    """Kills a move that leaves the gateway client and tracing in shared instead of under agent_api."""
    moved = [
        BACKEND / "helpdesk" / "agent_api" / "services" / "model_gateway" / "client.py",
        BACKEND / "helpdesk" / "agent_api" / "services" / "tracing.py",
    ]
    for path in moved:
        assert path.is_file(), f"expected moved file is missing: {path.relative_to(REPO_ROOT)}"

    gone = [
        BACKEND / "helpdesk" / "shared" / "gateway" / "client.py",
        BACKEND / "helpdesk" / "shared" / "obs" / "tracing.py",
    ]
    for path in gone:
        assert not path.is_file(), f"old shared file still present: {path.relative_to(REPO_ROOT)}"

    stays = BACKEND / "helpdesk" / "shared" / "gateway" / "keys.py"
    assert stays.is_file(), f"shared file that must stay is missing: {stays.relative_to(REPO_ROOT)}"


def test_mcp_services_present() -> None:
    """Kills a move that leaves mcp-only rag, idempotency, notifications list and rollback in shared."""
    moved = [
        BACKEND / "helpdesk" / "mcp_server" / "services" / "rag" / "pipeline.py",
        BACKEND / "helpdesk" / "mcp_server" / "services" / "idempotency" / "guard.py",
        BACKEND / "helpdesk" / "mcp_server" / "services" / "notifications" / "list.py",
        BACKEND / "helpdesk" / "mcp_server" / "services" / "access_requests" / "rollback.py",
    ]
    for path in moved:
        assert path.is_file(), f"expected moved file is missing: {path.relative_to(REPO_ROOT)}"

    gone = [
        BACKEND / "helpdesk" / "shared" / "rag" / "pipeline.py",
        BACKEND / "helpdesk" / "shared" / "idempotency" / "guard.py",
        BACKEND / "helpdesk" / "shared" / "notifications" / "list.py",
        BACKEND / "helpdesk" / "shared" / "access" / "rollback.py",
    ]
    for path in gone:
        assert not path.is_file(), f"old shared file still present: {path.relative_to(REPO_ROOT)}"


def test_metrics_sweep_merged() -> None:
    """Kills a move that leaves the two gauge refreshers in shared instead of merged into one scheduler job."""
    sweep = BACKEND / "helpdesk" / "scheduler" / "jobs" / "metrics_sweep.py"
    assert sweep.is_file(), f"merged metrics sweep job is missing: {sweep.relative_to(REPO_ROOT)}"

    text = sweep.read_text(encoding="utf-8")
    assert "def refresh_active_grants_gauge" in text, (
        "metrics_sweep.py is missing refresh_active_grants_gauge"
    )
    assert "def refresh_chain_gauge" in text, "metrics_sweep.py is missing refresh_chain_gauge"

    old_jobs_module = BACKEND / "helpdesk" / "scheduler" / "jobs.py"
    assert not old_jobs_module.is_file(), (
        f"old scheduler jobs.py still present: {old_jobs_module.relative_to(REPO_ROOT)}"
    )

    jobs_init = BACKEND / "helpdesk" / "scheduler" / "jobs" / "__init__.py"
    assert jobs_init.is_file(), (
        f"scheduler jobs package init is missing: {jobs_init.relative_to(REPO_ROOT)}"
    )

    gone = [
        BACKEND / "helpdesk" / "shared" / "access" / "gauges.py",
        BACKEND / "helpdesk" / "shared" / "audit" / "health.py",
    ]
    for path in gone:
        assert not path.is_file(), f"old shared file still present: {path.relative_to(REPO_ROOT)}"


def test_shared_kept_what_two_apps_read() -> None:
    """Kills a move that pulls two-app code out of shared or copies it under an app tree."""
    stays = [
        BACKEND / "helpdesk" / "shared" / "policy" / "engine.py",
        BACKEND / "helpdesk" / "shared" / "policy" / "types.py",
        BACKEND / "helpdesk" / "shared" / "access" / "execute.py",
        BACKEND / "helpdesk" / "shared" / "access" / "requests.py",
        BACKEND / "helpdesk" / "shared" / "access" / "revoke.py",
        BACKEND / "helpdesk" / "shared" / "notifications" / "notify.py",
        BACKEND / "helpdesk" / "shared" / "audit" / "anchor.py",
        BACKEND / "helpdesk" / "shared" / "services" / "policy_docs" / "naming.py",
        BACKEND / "helpdesk" / "shared" / "gateway" / "keys.py",
    ]
    for path in stays:
        assert path.is_file(), (
            f"shared file that must stay is missing: {path.relative_to(REPO_ROOT)}"
        )

    policy_dir = BACKEND / "helpdesk" / "mcp_server" / "services" / "policy"
    assert not policy_dir.exists(), (
        f"shared policy was copied under an app tree: {policy_dir.relative_to(REPO_ROOT)}"
    )

    copies = [
        BACKEND / "helpdesk" / "mcp_server" / "services" / "policy_engine" / "engine.py",
        BACKEND / "helpdesk" / "mcp_server" / "services" / "access_requests" / "execute.py",
        BACKEND / "helpdesk" / "mcp_server" / "services" / "notifications" / "notify.py",
    ]
    for path in copies:
        assert not path.is_file(), (
            f"shared file was copied under an app tree: {path.relative_to(REPO_ROOT)}"
        )
