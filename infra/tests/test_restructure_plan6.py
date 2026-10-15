"""Static checks that prove an earlier change's moves inside shared/.

This file does not touch Postgres, Redis, Qdrant, any fixture or the network.
It only scans files on disk by their path, and reads a few split files' text
to confirm the right names landed in them.

An earlier change (restructure An earlier change) tidies the inside of `shared/`:
`core/repository.py` splits into a `crud/` package (one module per table);
`core/settings.py`→`core/config.py` and `errors.py`→`core/errors.py`;
`obs/` moves under `core/observability/`; the killswitch, audit and access
services split into packages, and `rag/naming.py`→`services/policy_docs/
naming.py`; the kept `audit/` and `access/` modules keep a fresh `__init__.py`;
the redis helper moves to `core/redis.py` and the two per-app copies go away.
The happy paths prove the NEW locations are present; the unhappy paths prove
the OLD files are GONE and that dropped targets were never created.

Readiness is deliberately NOT touched here: an earlier change already moved readiness
into each app's `api/routes/health.py`, and the two differ (mcp-server checks
Qdrant, scheduler does not), so there is no shared `core/readiness.py` and
an earlier change does not create one.

While the moves are still pending every "present" case is red on purpose.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"
SHARED = BACKEND / "helpdesk" / "shared"


def test_crud_split() -> None:
    """Kills a split that leaves repository.py whole or drops a table's crud module."""
    present = [SHARED / "crud" / "__init__.py"]
    for m in [
        "tenants",
        "users",
        "grants",
        "idempotency",
        "messages",
        "sessions",
        "access_requests",
    ]:
        present.append(SHARED / "crud" / f"{m}.py")
    for path in present:
        assert path.is_file(), f"expected split file is missing: {path.relative_to(REPO_ROOT)}"

    init_text = (SHARED / "crud" / "__init__.py").read_text(encoding="utf-8")
    assert "class Repository" in init_text, (
        f"class Repository not found in {(SHARED / 'crud' / '__init__.py').relative_to(REPO_ROOT)}"
    )

    gone = SHARED / "core" / "repository.py"
    assert not gone.is_file(), f"old file still present: {gone.relative_to(REPO_ROOT)}"


def test_dropped_crud_targets_absent() -> None:
    """Kills a split that invents crud modules for tables that have no repository helpers."""
    gone = [
        SHARED / "crud" / "audit_events.py",
        SHARED / "crud" / "notifications.py",
        SHARED / "crud" / "reviews.py",
    ]
    for path in gone:
        assert not path.is_file(), f"crud module should not exist: {path.relative_to(REPO_ROOT)}"


def test_config_and_errors_renamed() -> None:
    """Kills a rename that keeps settings.py or leaves errors.py at the shared top level."""
    present = [
        SHARED / "core" / "config.py",
        SHARED / "core" / "errors.py",
    ]
    for path in present:
        assert path.is_file(), f"expected moved file is missing: {path.relative_to(REPO_ROOT)}"

    gone = [
        SHARED / "core" / "settings.py",
        SHARED / "errors.py",
    ]
    for path in gone:
        assert not path.is_file(), f"old file still present: {path.relative_to(REPO_ROOT)}"


def test_observability_moved() -> None:
    """Kills a move that leaves the obs/ package in place instead of under core/observability/."""
    present = []
    for m in ["context", "logging", "metrics"]:
        present.append(SHARED / "core" / "observability" / f"{m}.py")
    for path in present:
        assert path.is_file(), f"expected moved file is missing: {path.relative_to(REPO_ROOT)}"

    assert not (SHARED / "obs").is_dir(), (
        f"old package still present: {(SHARED / 'obs').relative_to(REPO_ROOT)}"
    )


def test_services_split() -> None:
    """Kills a split that keeps the flat service files or the old rag/naming.py location."""
    present = []
    for m in ["switch", "change"]:
        present.append(SHARED / "services" / "killswitch" / f"{m}.py")
    for m in ["chain", "search", "verify", "writer"]:
        present.append(SHARED / "services" / "audit" / f"{m}.py")
    for m in ["approvals", "directory", "lifecycle", "reviews", "snapshot"]:
        present.append(SHARED / "services" / "access" / f"{m}.py")
    present.append(SHARED / "services" / "policy_docs" / "naming.py")
    for path in present:
        assert path.is_file(), f"expected split file is missing: {path.relative_to(REPO_ROOT)}"

    assert not (SHARED / "killswitch").is_dir(), (
        f"old package still present: {(SHARED / 'killswitch').relative_to(REPO_ROOT)}"
    )
    gone_file = SHARED / "rag" / "naming.py"
    assert not gone_file.is_file(), f"old file still present: {gone_file.relative_to(REPO_ROOT)}"


def test_kept_modules_get_fresh_init() -> None:
    """Kills a split that moves anchor/retention or execute/revoke away, or drops their __init__."""
    present = [
        SHARED / "audit" / "anchor.py",
        SHARED / "audit" / "retention.py",
        SHARED / "audit" / "__init__.py",
        SHARED / "access" / "execute.py",
        SHARED / "access" / "revoke.py",
        SHARED / "access" / "__init__.py",
    ]
    for path in present:
        assert path.is_file(), f"expected kept file is missing: {path.relative_to(REPO_ROOT)}"

    gone = [
        SHARED / "audit" / "chain.py",
        SHARED / "access" / "approvals.py",
    ]
    for path in gone:
        assert not path.is_file(), f"moved file still present: {path.relative_to(REPO_ROOT)}"


def test_redis_shared() -> None:
    """Kills a move that keeps the two per-app redis helpers instead of one shared get_redis."""
    shared_redis = SHARED / "core" / "redis.py"
    assert shared_redis.is_file(), (
        f"expected moved file is missing: {shared_redis.relative_to(REPO_ROOT)}"
    )
    redis_text = shared_redis.read_text(encoding="utf-8")
    assert "def get_redis" in redis_text, (
        f"def get_redis not found in {shared_redis.relative_to(REPO_ROOT)}"
    )

    gone = [
        BACKEND / "helpdesk" / "mcp_server" / "redis_client.py",
        BACKEND / "helpdesk" / "scheduler" / "redis_client.py",
    ]
    for path in gone:
        assert not path.is_file(), f"old file still present: {path.relative_to(REPO_ROOT)}"
