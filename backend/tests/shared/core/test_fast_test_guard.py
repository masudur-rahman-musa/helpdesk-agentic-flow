"""No test file under `backend/tests` builds its own backing service.

What is pinned: the SET of test files that build their own engine, Redis client,
Qdrant client, raw connection, or schema (`Base.metadata.create_all` /
`CREATE DATABASE`) is exactly `EXEMPT_BUILDERS`, both directions. A new file that
builds a service without an entry fails, and an entry whose file stopped building
fails too, so the list never rots and never grows silently.

Why the check is static rather than runtime: a test that builds an engine inside
a body no run reaches is invisible at runtime, but its source still carries the
builder call. Scanning the text reads the code that exists, not the code that
ran. It is the same shape as `test_tenant_guard_static.py`.

The scan uses six patterns: four BUILDER patterns and two SCHEMA patterns.
`test_a_planted_engine_builder_off_the_list_is_caught` proves the
scan can fail, and `test_the_scanned_test_set_is_not_empty` proves it is not
reading an empty file set. `test_a_shared_fixture_user_is_not_a_finding` proves
reaching a service through a shared fixture is not itself a finding.
"""

import re
from pathlib import Path


def _repo_root() -> Path:
    """The directory holding `backend/pyproject.toml`, found by walking upwards.

    Walked rather than indexed off a fixed `parents[n]` so that moving this file
    one directory does not silently point every case at an empty tree; the depth
    is then still checked by `test_the_scanned_test_set_is_not_empty`.
    """
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "backend" / "pyproject.toml").is_file():
            return candidate
    raise AssertionError("repo root not found: no parent holds backend/pyproject.toml")


REPO_ROOT = _repo_root()
"""Every path in this module is resolved from here and reported relative to it."""

TEST_TREE = REPO_ROOT / "backend" / "tests"
"""The one tree the builder scan walks.

`rglob("test_*.py")` over this tree naturally skips `conftest.py` and the
`support/` helpers, which is intended: the session fixtures belong in
`conftest.py`, and the helpers are not test files. Feeds
`_scanned_test_sources`."""

_THIS_FILE = Path(__file__).resolve()
"""This guard names every builder pattern and its planted synthetic sources as
literals, so it trivially matches its own scan while building no service. It is
left out of `_scanned_test_sources` so it is not a false finding; dropping it
would make the scanned count 150 and put this file on `EXEMPT_BUILDERS`, both of
which `test_the_scanned_test_set_is_not_empty` and the exact-set case would then
have to carry for no reason."""

BUILDER_PATTERNS: tuple[str, ...] = (
    r"(?<![\w.])create(_async)?_engine\(",
    r"(?<![\w])(?<!Fake)(Async)?Redis(\.from_url)?\(",
    r"(?<![\w])(Async)?QdrantClient\(",
    r"(?<![\w])(asyncpg|psycopg)\.connect\(",
)
"""The four patterns that match a test building its own backing service."""

SCHEMA_PATTERNS: tuple[str, ...] = (
    r"Base\.metadata\.create_all",
    r"CREATE DATABASE",
)
"""The two patterns that match a test building its own schema or database.

A test that creates its own schema or database is building its own backing
service just as much as one that builds an engine. Scanned together with
`BUILDER_PATTERNS`."""

EXEMPT_BUILDERS: frozenset[str] = frozenset(
    {
        # agent-api chat and route suites: each builds its own engine, and some a
        # schema plus CREATE DATABASE; 57a deferred converting them to the shared
        # fixtures.
        "backend/tests/agent_api/test_agent_metrics.py",
        "backend/tests/agent_api/test_audit_verify_route.py",
        "backend/tests/agent_api/test_chat_route.py",
        "backend/tests/agent_api/test_chat_stream.py",
        "backend/tests/agent_api/test_login.py",
        "backend/tests/agent_api/test_rate_limit.py",
        # mcp-server service suites (rag ingest/retrieval, idempotency, rollback):
        # each builds its own engine; the rag and subtest set 57a deferred.
        "backend/tests/mcp_server/services/access_requests/test_access_rollback.py",
        "backend/tests/mcp_server/services/idempotency/test_idempotency_store.py",
        "backend/tests/mcp_server/services/rag/test_rag_chunks.py",
        "backend/tests/mcp_server/services/rag/test_rag_citations.py",
        "backend/tests/mcp_server/services/rag/test_rag_isolation.py",
        "backend/tests/mcp_server/services/rag/test_rag_pipeline.py",
        "backend/tests/mcp_server/services/rag/test_rag_store.py",
        # mcp-server tool suites: each builds its own engine, some their own Redis;
        # the large tool-matrix set 57a left on its own database.
        "backend/tests/mcp_server/test_mcp_access_read_tools.py",
        "backend/tests/mcp_server/test_mcp_approvals.py",
        "backend/tests/mcp_server/test_mcp_create_access_request.py",
        "backend/tests/mcp_server/test_mcp_helpdesk_tools.py",
        "backend/tests/mcp_server/test_mcp_list_kill_switches.py",
        "backend/tests/mcp_server/test_mcp_list_policy_documents.py",
        "backend/tests/mcp_server/test_mcp_list_sod_rules.py",
        "backend/tests/mcp_server/test_mcp_notifications.py",
        "backend/tests/mcp_server/test_mcp_offboard_user.py",
        "backend/tests/mcp_server/test_mcp_onboard_user.py",
        "backend/tests/mcp_server/test_mcp_pipeline.py",
        "backend/tests/mcp_server/test_mcp_policy_isolation.py",
        "backend/tests/mcp_server/test_mcp_rbac_matrix.py",
        "backend/tests/mcp_server/test_mcp_read_tools.py",
        "backend/tests/mcp_server/test_mcp_registry.py",
        "backend/tests/mcp_server/test_mcp_reingest_policy_document.py",
        "backend/tests/mcp_server/test_mcp_reviews.py",
        "backend/tests/mcp_server/test_mcp_revoke_access.py",
        "backend/tests/mcp_server/test_mcp_rollback_request.py",
        "backend/tests/mcp_server/test_mcp_search_audit.py",
        "backend/tests/mcp_server/test_mcp_search_policy.py",
        "backend/tests/mcp_server/test_mcp_set_kill_switch.py",
        "backend/tests/mcp_server/test_mcp_transfer_user.py",
        # scheduler suites: the global-sweep tests 57a reverted to their own
        # isolated database because the tick enumerates every tenant.
        "backend/tests/scheduler/jobs/test_access_gauges.py",
        "backend/tests/scheduler/jobs/test_audit_health.py",
        "backend/tests/scheduler/test_scheduler_runner.py",
        # seed_all corpus test: builds its own engine and schema by design.
        "backend/tests/scripts/test_seed.py",
        # schema-shape / model tests: each runs Base.metadata.create_all on its own
        # engine to check the mapped shape, which the shared fixtures do not give.
        "backend/tests/shared/models/test_audit_model.py",
        "backend/tests/shared/models/test_conversation_models.py",
        "backend/tests/shared/models/test_models.py",
        "backend/tests/shared/models/test_notification_models.py",
        "backend/tests/shared/models/test_policy_models.py",
        # shared access lifecycle suites still on their own engine (57a deferred).
        "backend/tests/shared/access/test_access_approvals.py",
        "backend/tests/shared/access/test_access_lifecycle.py",
        "backend/tests/shared/access/test_access_offboard.py",
        "backend/tests/shared/access/test_access_reviews.py",
        "backend/tests/shared/access/test_access_snapshot.py",
        "backend/tests/shared/access/test_access_transfer.py",
        # audit role test needs its own restricted-role database connection.
        "backend/tests/shared/audit/test_audit_db_role.py",
        # one of the two Redis-client tests: builds its own Redis.
        "backend/tests/shared/services/killswitch/test_killswitch.py",
        # shared access and audit-chain suites still on their own engine (57a deferred).
        "backend/tests/shared/test_access_execute.py",
        "backend/tests/shared/test_access_revoke.py",
        "backend/tests/shared/test_audit_anchor.py",
        "backend/tests/shared/test_audit_anchor_retention.py",
        # asks the server for its own name, so it must not go through the
        # shared fixtures whose server it is checking.
        "backend/tests/support/test_db_server.py",
    }
)
"""The test files sanctioned to build their own backing service, as repo-relative paths.

Read off the tree as it stands today:
these 57 files each match a builder or schema pattern now. Grouped by the reason
each still builds its own rather than taking the shared fixtures. Asserted as an
EXACT set by `test_the_builders_are_exactly_the_exempt_list`: a file that reverts to building its
own and is not here reddens, and a file
here that converts and stops building reddens until it is dropped."""


def _scanned_test_sources() -> list[tuple[str, str]]:
    """Every `test_*.py` under `TEST_TREE`, as `(repo-relative path, source)`.

    Repo-relative so paths read like `backend/tests/...`. Sorted so the order is
    stable across runs. Feeds every case that scans the real tree."""
    pairs: list[tuple[str, str]] = []
    for path in sorted(TEST_TREE.rglob("test_*.py")):
        if path.resolve() == _THIS_FILE:
            continue
        relative = path.relative_to(REPO_ROOT)
        pairs.append((str(relative), path.read_text(encoding="utf-8")))
    return pairs


def _builds_backing_service(source: str) -> bool:
    """True when a non-comment line of `source` matches any of the six patterns.

    Split into lines, skip any line whose `lstrip()` starts with `#`, apply
    `re.search`. A comment mentioning `create_async_engine` is therefore not a
    finding."""
    for line in source.splitlines():
        if line.lstrip().startswith("#"):
            continue
        for pattern in (*BUILDER_PATTERNS, *SCHEMA_PATTERNS):
            if re.search(pattern, line):
                return True
    return False


def _builder_files(sources: list[tuple[str, str]]) -> set[str]:
    """The repo-relative paths, of the `(path, source)` pairs given, that build a service.

    Takes pairs rather than reading the disk itself so that
    `test_a_planted_engine_builder_off_the_list_is_caught` can feed one synthetic
    module in without a file ever being written to the tree."""
    return {path for path, source in sources if _builds_backing_service(source)}


PLANTED_ENGINE_SOURCE = (
    "from sqlalchemy.ext.asyncio import create_async_engine\n"
    "engine = create_async_engine('postgresql+asyncpg://x')\n"
)
"""A test module as it must never be written off the list: it builds its own
engine instead of taking one from a session fixture. Feeds
`test_a_planted_engine_builder_off_the_list_is_caught`, the kill for the scan;
fed as a string, never written into the tree."""

SHARED_FIXTURE_SOURCE = (
    "from tests.conftest import fresh_tenant\n"
    "\n"
    "\n"
    "async def test_x(fresh_tenant):\n"
    "    tenant = fresh_tenant\n"
    "    assert tenant\n"
)
"""The innocent shape of a test that reaches a service through a shared fixture:
it imports the conftest factory and binds a tenant, building nothing. Feeds
`test_a_shared_fixture_user_is_not_a_finding`, which stops the guard being a
blanket ban on reaching a service."""


def test_the_builders_are_exactly_the_exempt_list() -> None:
    """Catches a new test building its own backing service with no entry, and
    equally an entry whose file stopped building and left the list rotting."""
    builders = _builder_files(_scanned_test_sources())
    exempt = set(EXEMPT_BUILDERS)

    unexempt = sorted(builders - exempt)
    stale = sorted(exempt - builders)
    assert builders == exempt, (
        f"builders off the exempt list: {unexempt}; exempt entries that build nothing: {stale}"
    )


def test_a_planted_engine_builder_off_the_list_is_caught() -> None:
    """Catches a scan that reports nothing: one planted engine builder off the
    list must be found and must break the equality the first case relies on, so
    the scan cannot be vacuously green."""
    planted = (
        "backend/tests/shared/test_planted_engine.py",
        PLANTED_ENGINE_SOURCE,
    )
    builders = _builder_files([*_scanned_test_sources(), planted])

    assert planted[0] in builders
    assert builders != set(EXEMPT_BUILDERS)


def test_a_shared_fixture_user_is_not_a_finding() -> None:
    """Catches a guard widened into a blanket ban on reaching a service, which
    would redden every test that takes a tenant from a shared conftest fixture
    while building nothing itself."""
    pair = ("backend/tests/shared/test_fixture_user.py", SHARED_FIXTURE_SOURCE)
    assert _builder_files([pair]) == set()


def test_the_scanned_test_set_is_not_empty() -> None:
    """Catches a wrong glob that makes the exact-set case pass over an empty file
    set, and an exempt list emptied to make it pass trivially."""
    assert len(_scanned_test_sources()) >= 140, (
        f"only {len(_scanned_test_sources())} test files scanned"
    )
    assert len(EXEMPT_BUILDERS) >= 50, f"only {len(EXEMPT_BUILDERS)} exempt builders listed"
