"""Static checks that prove an earlier change's moves inside agent_api.

This file does not touch Postgres, Redis, Qdrant, any fixture or the network.
It only scans files on disk by their path.

An earlier change reshapes the inside of `backend/helpdesk/agent_api/`: routes move under
`api/`, schemas and services split into their own packages, the graph and
tools folders become one `agent/` package, and the checkpointer setup script
becomes a CLI with its deps under `core/`. The happy paths prove the NEW
locations are present. The unhappy paths prove the OLD files and folders that
got moved are GONE.

While the moves are still pending every case is red on purpose.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"
AGENT_API = BACKEND / "helpdesk" / "agent_api"


def test_routes_under_api() -> None:
    """Kills a move that leaves routes as top-level *_routes.py files instead of under api/routes/."""
    present = [
        AGENT_API / "api" / "router.py",
        AGENT_API / "api" / "sse.py",
        AGENT_API / "api" / "routes" / "chat.py",
        AGENT_API / "api" / "routes" / "auth.py",
        AGENT_API / "api" / "routes" / "me.py",
        AGENT_API / "api" / "routes" / "notifications.py",
        AGENT_API / "api" / "routes" / "approvals.py",
        AGENT_API / "api" / "routes" / "reviews.py",
        AGENT_API / "api" / "routes" / "health.py",
        AGENT_API / "api" / "routes" / "admin" / "audit.py",
        AGENT_API / "api" / "routes" / "admin" / "killswitch.py",
        AGENT_API / "api" / "routes" / "admin" / "policies.py",
        AGENT_API / "api" / "routes" / "admin" / "rules.py",
    ]
    for path in present:
        assert path.is_file(), f"expected moved file is missing: {path.relative_to(REPO_ROOT)}"

    gone = [
        AGENT_API / "main.py",
        AGENT_API / "sse.py",
        AGENT_API / "admin_routes.py",
        AGENT_API / "approval_routes.py",
        AGENT_API / "audit_routes.py",
        AGENT_API / "auth_routes.py",
        AGENT_API / "chat_routes.py",
        AGENT_API / "killswitch_routes.py",
        AGENT_API / "me_routes.py",
        AGENT_API / "notification_routes.py",
        AGENT_API / "policies_routes.py",
        AGENT_API / "review_routes.py",
        AGENT_API / "rules_routes.py",
    ]
    for path in gone:
        assert not path.is_file(), f"old file still present: {path.relative_to(REPO_ROOT)}"


def test_schemas_and_services_split_out() -> None:
    """Kills a move that keeps schemas and services inline instead of split into schemas/ and services/."""
    present = [
        AGENT_API / "schemas" / "chat.py",
        AGENT_API / "schemas" / "auth.py",
        AGENT_API / "schemas" / "me.py",
        AGENT_API / "schemas" / "audit.py",
        AGENT_API / "schemas" / "killswitch.py",
        AGENT_API / "schemas" / "policies.py",
        AGENT_API / "schemas" / "rules.py",
        AGENT_API / "schemas" / "approvals.py",
        AGENT_API / "schemas" / "reviews.py",
        AGENT_API / "schemas" / "notifications.py",
        AGENT_API / "services" / "chat.py",
        AGENT_API / "services" / "auth.py",
        AGENT_API / "services" / "tool_proxy.py",
        AGENT_API / "services" / "decision_card.py",
        AGENT_API / "services" / "mcp_client.py",
        AGENT_API / "services" / "ratelimit.py",
    ]
    for path in present:
        assert path.is_file(), f"expected moved file is missing: {path.relative_to(REPO_ROOT)}"


def test_agent_package_present() -> None:
    """Kills a move that keeps the graph and tools folders instead of one agent/ package."""
    present = [
        AGENT_API / "agent" / "graph.py",
        AGENT_API / "agent" / "errors.py",
        AGENT_API / "agent" / "state.py",
        AGENT_API / "agent" / "gates.py",
        AGENT_API / "agent" / "keys.py",
        AGENT_API / "agent" / "matching.py",
        AGENT_API / "agent" / "postcheck.py",
        AGENT_API / "agent" / "replay.py",
        AGENT_API / "agent" / "resume.py",
        AGENT_API / "agent" / "turn_deps.py",
        AGENT_API / "agent" / "turn_metrics.py",
        AGENT_API / "agent" / "checkpointer.py",
        AGENT_API / "agent" / "prompts.py",
        AGENT_API / "agent" / "tool_args.py",
        AGENT_API / "agent" / "nodes" / "extract_intent.py",
        AGENT_API / "agent" / "prompts" / "system.md",
    ]
    for path in present:
        assert path.is_file(), f"expected moved file is missing: {path.relative_to(REPO_ROOT)}"

    assert not (AGENT_API / "graph").is_dir(), (
        f"old graph dir still present: {(AGENT_API / 'graph').relative_to(REPO_ROOT)}"
    )
    assert not (AGENT_API / "tools").is_dir(), (
        f"old tools dir still present: {(AGENT_API / 'tools').relative_to(REPO_ROOT)}"
    )


def test_cli_moved_and_core_deps() -> None:
    """Kills a move that leaves the checkpointer setup as a root script and deps outside core/."""
    present = [
        AGENT_API / "cli.py",
        AGENT_API / "core" / "deps.py",
    ]
    for path in present:
        assert path.is_file(), f"expected moved file is missing: {path.relative_to(REPO_ROOT)}"

    gone = [
        REPO_ROOT / "scripts" / "checkpointer_setup.py",
        AGENT_API / "deps.py",
        AGENT_API / "graph" / "build.py",
        AGENT_API / "graph" / "plain_errors.py",
    ]
    for path in gone:
        assert not path.is_file(), f"old file still present: {path.relative_to(REPO_ROOT)}"
