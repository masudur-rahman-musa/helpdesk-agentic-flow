"""Static checks that prove an earlier change's moves inside mcp_server and scheduler.

This file does not touch Postgres, Redis, Qdrant, any fixture or the network.
It only scans files on disk by their path, and reads a few split files' text
to confirm the right functions landed in them.

An earlier change gives the mcp-server and the scheduler the app layout shape:
`identity.py` and `rag_clients.py` move under `core/`; `readiness.py` becomes
`api/routes/health.py`; `pipeline.py`/`registry.py` move under `tools/` as
`dispatch.py`/`registry.py`; `helpdesk.py`→`self_service.py`,
`policy.py`→`policy_search.py`; `admin.py` splits four ways into
`killswitch.py`, `policy_docs.py`, `rollback.py`, `rules.py`; the scheduler's
`readiness.py` becomes `api/routes/health.py`; `scripts/ingest.py` becomes
`mcp_server/cli.py`. The redis helper and a shared readiness helper do NOT
move here (that is an earlier change). The happy paths prove the NEW locations are
present; the unhappy paths prove the OLD files are GONE and that an earlier change's
moves have not happened yet.

While the moves are still pending every "present" case is red on purpose.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"
MCP = BACKEND / "helpdesk" / "mcp_server"
SCHEDULER = BACKEND / "helpdesk" / "scheduler"
SHARED = BACKEND / "helpdesk" / "shared"


def test_mcp_core_and_api_moves() -> None:
    """Kills a move that leaves identity/rag_clients at the top level or readiness out of api/."""
    present = [
        MCP / "core" / "identity.py",
        MCP / "core" / "rag_clients.py",
        MCP / "api" / "routes" / "health.py",
    ]
    for path in present:
        assert path.is_file(), f"expected moved file is missing: {path.relative_to(REPO_ROOT)}"

    gone = [
        MCP / "identity.py",
        MCP / "rag_clients.py",
        MCP / "readiness.py",
    ]
    for path in gone:
        assert not path.is_file(), f"old file still present: {path.relative_to(REPO_ROOT)}"


def test_mcp_tools_renamed_and_split() -> None:
    """Kills a move that keeps old tool names or splits admin's functions into the wrong files."""
    present = [
        MCP / "tools" / "dispatch.py",
        MCP / "tools" / "registry.py",
        MCP / "tools" / "self_service.py",
        MCP / "tools" / "policy_search.py",
        MCP / "tools" / "killswitch.py",
        MCP / "tools" / "policy_docs.py",
        MCP / "tools" / "rollback.py",
        MCP / "tools" / "rules.py",
    ]
    for path in present:
        assert path.is_file(), f"expected moved file is missing: {path.relative_to(REPO_ROOT)}"

    gone = [
        MCP / "tools" / "pipeline.py",
        MCP / "tools" / "admin.py",
        MCP / "tools" / "helpdesk.py",
        MCP / "tools" / "policy.py",
    ]
    for path in gone:
        assert not path.is_file(), f"old file still present: {path.relative_to(REPO_ROOT)}"

    split_funcs = {
        MCP / "tools" / "killswitch.py": ["set_kill_switch", "list_kill_switches"],
        MCP / "tools" / "policy_docs.py": ["list_policy_documents", "reingest_policy_document"],
        MCP / "tools" / "rollback.py": ["rollback_request"],
        MCP / "tools" / "rules.py": ["list_sod_rules"],
    }
    for path, names in split_funcs.items():
        assert path.is_file(), f"expected split file is missing: {path.relative_to(REPO_ROOT)}"
        text = path.read_text(encoding="utf-8")
        for name in names:
            assert f"def {name}" in text, (
                f"function {name} not found in {path.relative_to(REPO_ROOT)}"
            )


def test_scheduler_health_and_cli() -> None:
    """Kills a move that leaves scheduler readiness unmoved or ingest at the repo root."""
    present = [
        SCHEDULER / "api" / "routes" / "health.py",
        MCP / "cli.py",
    ]
    for path in present:
        assert path.is_file(), f"expected moved file is missing: {path.relative_to(REPO_ROOT)}"

    gone = [
        SCHEDULER / "readiness.py",
        REPO_ROOT / "scripts" / "ingest.py",
    ]
    for path in gone:
        assert not path.is_file(), f"old file still present: {path.relative_to(REPO_ROOT)}"


def test_redis_not_moved_yet() -> None:
    """Kills a move that pulls an earlier change's redis/readiness shift early into an earlier change."""
    still_here = [
        MCP / "redis_client.py",
        SCHEDULER / "redis_client.py",
    ]
    for path in still_here:
        assert path.is_file(), (
            f"redis helper moved early (that is plan 50): {path.relative_to(REPO_ROOT)}"
        )

    not_yet = [
        SHARED / "core" / "redis.py",
        SHARED / "core" / "readiness.py",
    ]
    for path in not_yet:
        assert not path.is_file(), (
            f"plan 50 target exists already: {path.relative_to(REPO_ROOT)}"
        )
