"""No module under `services/` builds or runs a SQL statement.

What is pinned: every service module reaches Postgres through the tenant-scoped
repository helpers only, and the one path the `do_orm_execute` guard does NOT
intercept -- a raw `sqlalchemy.text()` statement -- is taken by exactly the five
modules `TEXT_ALLOWLIST` names.

Why the check is static rather than runtime: a query built inside a function
body that no test happens to call is invisible to a runtime guard, and the
`do_orm_execute` listener cannot see a `text()` statement at all. Parsing every
file with `ast` reads the code that exists, not the code that ran. It is the
same shape as `test_policy_purity.py`.

Two synthetic-source cases (`test_a_planted_statement_builder_is_caught`,
`test_a_fifth_text_importer_is_caught`) prove the walkers can fail, since the
tree is clean today; `test_the_service_source_set_is_not_empty` proves they are
not reading an empty file set.
"""

import ast
from pathlib import Path

import pytest


def _repo_root() -> Path:
    """The directory holding the root `pyproject.toml`, found by walking upwards.

    Walked rather than indexed off `parents[3]` so that moving this file one
    directory does not silently point every case at an empty tree; the depth is
    then still checked by `test_the_service_source_set_is_not_empty`.
    """
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "backend" / "pyproject.toml").is_file():
            return candidate
    raise AssertionError("repo root not found: no parent holds backend/pyproject.toml")


REPO_ROOT = _repo_root()
"""Every path in this module is resolved from here and reported relative to it."""

SERVICE_PACKAGES: tuple[str, ...] = (
    "agent_api",
    "mcp_server",
    "scheduler",
    "shared",
)
"""The app subpackages under `backend/helpdesk/` that must not build raw queries.

`shared/` is included because all raw queries live in
`shared/crud/`, which is excluded from the scan (see `_service_files`).
Each package must contribute at least one file, which is what
`test_the_service_source_set_is_not_empty` asserts; without that, a wrong glob
would make every rule below pass over nothing."""

FORBIDDEN_BUILDERS: frozenset[str] = frozenset({"select", "insert", "update", "delete", "text"})
"""The `sqlalchemy` names that CONSTRUCT a statement, and so bypass the
tenant-scoped repository helpers when called in a service.

Deliberately not the whole `sqlalchemy` namespace: `AsyncSession`,
`async_sessionmaker`, `create_async_engine`, `String` and `SQLAlchemyError` are
imported by service modules today and build nothing, which
`test_a_type_only_sqlalchemy_import_is_not_a_finding` pins. Fed to
`test_no_service_module_builds_a_sqlalchemy_statement`."""

READINESS_ALLOWLIST: frozenset[str] = frozenset(
    {
        "backend/helpdesk/shared/core/health.py",
    }
)
"""The one service module sanctioned to call `conn.execute(text("SELECT 1"))`.

Sanctioned because nothing tenant-scoped can leak through it: each probe takes a
RAW engine connection (not an `AsyncSession`), runs the constant liveness probe
`SELECT 1`, reads no table, and interpolates no value. Every other service file
must be clean, and the allowlist is pinned to exactly this one by
`test_only_the_readiness_modules_build_or_run_a_statement`, so a second module
taking this path is a failure rather than a silent extension."""

SETUP_GRANT_ALLOWLIST: frozenset[str] = frozenset(
    {
        "backend/helpdesk/agent_api/agent/checkpointer.py",
    }
)
"""The one service module sanctioned to call `.execute` outside readiness, added 2026-10-12.

`grant_checkpointer_access` runs a constant `GRANT` on LangGraph's own checkpoint
tables, through a psycopg pool, from the migrate target only. It never runs on a
request, reads no row, and touches no tenant data; the role name it interpolates
is the migration's constant, never configuration. It builds no SQLAlchemy
statement, so it is kept apart from `READINESS_ALLOWLIST`, whose members must."""

STORE_ALLOWLIST: frozenset[str] = frozenset()
"""No service module is allowed to build or run its own SQL statements.

The idempotency guard, notifications and rag stores all use `shared/crud/`
helpers, so the allowlist is empty and every service file is expected to be clean.
`test_only_the_readiness_modules_build_or_run_a_statement` pins this as the
exact set."""

TEXT_ALLOWLIST: frozenset[str] = frozenset(
    {
        "backend/helpdesk/shared/core/health.py",
        "backend/helpdesk/shared/crud/audit.py",
        "backend/helpdesk/shared/crud/idempotency.py",
        "backend/helpdesk/shared/crud/numbers.py",
    }
)
"""The four modules in the whole tree that may import `sqlalchemy.text`.

`helpdesk/core/db.py`'s `do_orm_execute` guard covers ORM SELECT, UPDATE and
DELETE and does NOT intercept a `text()` statement, so this is the one path by
which a read can reach Postgres untenanted. The shared health probes run `SELECT 1`
on a raw engine connection; `crud/audit.py` and `crud/idempotency.py` hold the
only two advisory locks in the system (audit chain serialisation and idempotency
key claim); `crud/numbers.py` runs one upsert that takes the bound tenant id.
Asserted as an EXACT set by
`test_sqlalchemy_text_is_imported_by_exactly_the_allowlisted_modules`,
the diff that adds a fifth site extends this list."""

SCANNED_TREES: tuple[str, ...] = ("backend/helpdesk",)
"""The one tree the `text` allowlist is checked over, tests excluded.

Tests are excluded because `conftest.py` legitimately runs `text()` to create
and drop the throwaway database; `backend/migrations/` is outside this tree
because a migration is not tenant-scoped code. Feeds
`test_sqlalchemy_text_is_imported_by_exactly_the_allowlisted_modules`."""


def _service_files() -> tuple[Path, ...]:
    """Every `.py` file in the service subpackages, minus `shared/crud/`, sorted.

    `shared/crud/` is the one exempted tree: every file there is a query owner
    by design and is allowed to call `select`, `text`, etc.  Everything else in
    `shared/` must be clean, the same rule the three app packages
    already followed.

    The suites live under `backend/tests/`, not under these packages, so no test
    file is reachable from here.
    """
    crud_dir = REPO_ROOT / "backend" / "helpdesk" / "shared" / "crud"
    collected: list[Path] = []
    for package in SERVICE_PACKAGES:
        for path in sorted((REPO_ROOT / "backend" / "helpdesk" / package).rglob("*.py")):
            if path.is_relative_to(crud_dir):
                continue
            collected.append(path)
    return tuple(collected)


SERVICE_FILES: tuple[Path, ...] = _service_files()
"""Collected once at import so the parametrized cases build the file set one time."""

SERVICE_IDS: tuple[str, ...] = tuple(str(path.relative_to(REPO_ROOT)) for path in SERVICE_FILES)
"""Repo-relative paths, used both as the parametrize ids and as the allowlist keys."""


def _sqlalchemy_builder_names(tree: ast.Module) -> set[str]:
    """The names a file imported from `sqlalchemy` (or any `sqlalchemy.*` module).

    Only imported names count: a local `update()` helper or a `dict.update()`
    call is not a statement builder, and banning the bare name would redden
    innocent modules.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module is not None
            and (node.module == "sqlalchemy" or node.module.startswith("sqlalchemy."))
        ):
            names.update(alias.asname or alias.name for alias in node.names)
    return names


def _builder_findings(source: str, label: str) -> list[str]:
    """Every call of a `sqlalchemy`-imported name in `FORBIDDEN_BUILDERS`.

    Each finding reads `<label>:<line> calls <name>`, so a failure names the
    file, the symbol and the line without the reader opening anything.
    """
    tree = ast.parse(source)
    imported = _sqlalchemy_builder_names(tree) & FORBIDDEN_BUILDERS
    findings: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in imported
        ):
            findings.append(f"{label}:{node.lineno} calls {node.func.id}")
    return findings


def _execute_findings(source: str, label: str) -> list[str]:
    """Every call to an attribute named `execute`, whatever the receiver is."""
    tree = ast.parse(source)
    findings: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "execute"
        ):
            findings.append(f"{label}:{node.lineno} calls .execute")
    return findings


def _text_importing_paths(sources: list[tuple[str, str]]) -> set[str]:
    """The repo-relative paths, of the `(path, source)` pairs given, that import `text`.

    Takes pairs rather than reading the disk itself so that
    `test_a_fifth_text_importer_is_caught` can feed one synthetic module in
    without a file ever being written to the tree.
    """
    importers: set[str] = set()
    for path, source in sources:
        if "text" in _sqlalchemy_builder_names(ast.parse(source)):
            importers.add(path)
    return importers


def _scanned_sources() -> list[tuple[str, str]]:
    """Every non-test `.py` under `SCANNED_TREES`, as `(repo-relative path, source)`."""
    pairs: list[tuple[str, str]] = []
    for tree in SCANNED_TREES:
        for path in sorted((REPO_ROOT / tree).rglob("*.py")):
            relative = path.relative_to(REPO_ROOT)
            if "tests" in relative.parts:
                continue
            pairs.append((str(relative), path.read_text(encoding="utf-8")))
    return pairs


PLANTED_BUILDER_SOURCE = (
    "from sqlalchemy import select\n"
    "from helpdesk.models import User\n"
    "async def f(s):\n"
    "    return await s.execute(select(User))\n"
)
"""A service module as it must never be written: an untenanted ORM SELECT run on
a session. Feeds `test_a_planted_statement_builder_is_caught`, the kill for both
tree-wide rules; parsed as a string, never written into the tree."""

TYPE_ONLY_SOURCE = (
    "from sqlalchemy import String\n"
    "from sqlalchemy.exc import SQLAlchemyError\n"
    "from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker\n"
    "def f(session: AsyncSession, maker: async_sessionmaker[AsyncSession]) -> str:\n"
    "    try:\n"
    "        return String.__name__\n"
    "    except SQLAlchemyError:\n"
    "        return repr(maker)\n"
)
"""The innocent shape fourteen service modules already have: `sqlalchemy` names
used for annotations and error handling, building nothing. Feeds
`test_a_type_only_sqlalchemy_import_is_not_a_finding`, which stops the rule
being 'fixed' into a ban on importing `sqlalchemy` at all."""


def test_the_service_source_set_is_not_empty() -> None:
    """Catches a wrong glob that makes every rule below pass over an empty file set."""
    assert len(SERVICE_FILES) >= 20, f"only {len(SERVICE_FILES)} service files collected"
    for package in SERVICE_PACKAGES:
        package_dir = REPO_ROOT / "backend" / "helpdesk" / package
        assert package_dir.is_dir(), f"{package_dir} is not a directory"
        contributed = [path for path in SERVICE_FILES if package_dir in path.parents]
        assert contributed, f"{package} contributed no files"


@pytest.mark.parametrize("source_file", SERVICE_FILES, ids=SERVICE_IDS)
def test_no_service_module_builds_a_sqlalchemy_statement(source_file: Path) -> None:
    """Catches a service that constructs its own query instead of calling a
    tenant-scoped repository helper, which is how a read escapes the
    `do_orm_execute` filter."""
    relative = str(source_file.relative_to(REPO_ROOT))
    findings = _builder_findings(source_file.read_text(encoding="utf-8"), relative)

    if relative in READINESS_ALLOWLIST | STORE_ALLOWLIST:
        assert findings, f"{relative} is allowlisted but builds nothing; drop it from the list"
        return
    assert findings == [], f"{relative} builds a SQLAlchemy statement: {findings}"


@pytest.mark.parametrize("source_file", SERVICE_FILES, ids=SERVICE_IDS)
def test_no_service_module_calls_execute(source_file: Path) -> None:
    """Catches a service calling `session.execute(...)` directly -- the rule
    CLAUDE.md states in prose and nothing has enforced -- which runs a statement
    the repository helpers never saw and never tenant-filtered."""
    relative = str(source_file.relative_to(REPO_ROOT))
    findings = _execute_findings(source_file.read_text(encoding="utf-8"), relative)

    if relative in READINESS_ALLOWLIST | SETUP_GRANT_ALLOWLIST | STORE_ALLOWLIST:
        assert findings, f"{relative} is allowlisted but calls no .execute; drop it from the list"
        return
    assert findings == [], f"{relative} executes a statement directly: {findings}"


def test_only_the_readiness_modules_build_or_run_a_statement() -> None:
    """Catches any further service module joining the sanctioned raw-statement
    paths: both allowlists are proven against the tree, not merely declared."""
    offenders: set[str] = set()
    for source_file in SERVICE_FILES:
        relative = str(source_file.relative_to(REPO_ROOT))
        source = source_file.read_text(encoding="utf-8")
        if _builder_findings(source, relative) or _execute_findings(source, relative):
            offenders.add(relative)

    allowed = READINESS_ALLOWLIST | SETUP_GRANT_ALLOWLIST | STORE_ALLOWLIST
    assert offenders == set(allowed), (
        f"raw-statement modules {sorted(offenders)} != allowlist {sorted(allowed)}"
    )


def test_a_planted_statement_builder_is_caught() -> None:
    """Catches a walker that reports nothing: deleting the builder walk fails the
    first assertion and deleting the execute walk fails the second, so neither
    tree-wide rule can be vacuously green."""
    builder = _builder_findings(PLANTED_BUILDER_SOURCE, "planted.py")
    execute = _execute_findings(PLANTED_BUILDER_SOURCE, "planted.py")

    assert [finding for finding in builder if finding.endswith("calls select")] == [
        "planted.py:4 calls select"
    ]
    assert execute == ["planted.py:4 calls .execute"]


def test_a_type_only_sqlalchemy_import_is_not_a_finding() -> None:
    """Catches a rule widened into a ban on importing `sqlalchemy` at all, which
    would redden every service module annotating a parameter `AsyncSession`
    while catching no real query."""
    assert _builder_findings(TYPE_ONLY_SOURCE, "typed.py") == []
    assert _execute_findings(TYPE_ONLY_SOURCE, "typed.py") == []


def test_sqlalchemy_text_is_imported_by_exactly_the_allowlisted_modules() -> None:
    """Catches a sixth module reaching Postgres through `text()`, which
    `do_orm_execute` cannot intercept, and equally an allowlisted module that
    stopped using it and left the list rotting."""
    sources = _scanned_sources()

    assert len(sources) >= 20, f"only {len(sources)} source files scanned"
    assert _text_importing_paths(sources) == set(TEXT_ALLOWLIST)


def test_a_fifth_text_importer_is_caught() -> None:
    """Catches a collector that ignores the file it is given: one extra synthetic
    importer must break the equality the case above relies on."""
    planted = ("services/agent-api/agent_api/planted.py", "from sqlalchemy import text\n")
    importers = _text_importing_paths([*_scanned_sources(), planted])

    assert planted[0] in importers
    assert importers != set(TEXT_ALLOWLIST)


# ---------------------------------------------------------------------------
# All queries go through shared/crud helpers
# ---------------------------------------------------------------------------

_SHARED_ACCESS_PATH = "backend/helpdesk/shared/access/planted.py"
_SHARED_CRUD_PATH = "backend/helpdesk/shared/crud/planted.py"

_BUILDER_SOURCE = (
    "from sqlalchemy import select\n"
    "from helpdesk.shared.models import AccessRequest\n"
    "async def load(s):\n"
    "    return await s.execute(select(AccessRequest))\n"
)
"""A raw select call that must be a finding when the scan covers shared/access/."""


def test_the_scan_covers_shared_except_crud() -> None:
    """Catches an implementation that still only scans the three app packages
    SERVICE_FILES (or a replacement) must
    include at least one file from shared/ outside shared/crud/, and must
    include no file from shared/crud/.

    The builder walker already catches the call itself; this test proves the
    scan is widened to reach it by checking what files the module-level
    collection gathers.
    """
    shared_dir = REPO_ROOT / "backend" / "helpdesk" / "shared"
    crud_dir = shared_dir / "crud"

    shared_non_crud = [
        p for p in SERVICE_FILES if shared_dir in p.parents and crud_dir not in p.parents
    ]
    shared_crud = [p for p in SERVICE_FILES if crud_dir in p.parents]

    assert shared_non_crud, (
        "SERVICE_FILES contains no file from shared/ outside shared/crud/; "
        "the scan must be widened to cover shared/ after plan 70"
    )
    assert not shared_crud, (
        f"SERVICE_FILES contains shared/crud/ files {[str(p) for p in shared_crud]}; "
        "crud/ is exempted from the builder/execute scan"
    )


def test_the_store_allowlist_is_empty() -> None:
    """Catches an implementation that leaves the old STORE_ALLOWLIST entries in
    place instead of moving those queries into shared/crud/ helpers: every store module must be
    clean, so no allowlist entry is needed.
    """
    assert frozenset() == STORE_ALLOWLIST, (
        f"STORE_ALLOWLIST must be empty after plan 70 but still contains: {sorted(STORE_ALLOWLIST)}"
    )


_EXPECTED_TEXT_ALLOWLIST: frozenset[str] = frozenset(
    {
        "backend/helpdesk/shared/core/health.py",
        "backend/helpdesk/shared/crud/audit.py",
        "backend/helpdesk/shared/crud/idempotency.py",
        "backend/helpdesk/shared/crud/numbers.py",
    }
)
"""The exact set TEXT_ALLOWLIST must equal.

The shared health module keeps its entry because its probes run a SELECT 1 liveness
check through a raw engine connection. The two new entries replace the old
guard.py and writer.py paths: the only callers of sqlalchemy.text
outside the health probes are the crud helpers, the number counter included."""


def test_text_is_imported_by_exactly_the_probes_and_crud() -> None:
    """Catches an implementation that updates the code but forgets to update
    TEXT_ALLOWLIST, or that updates the list to the wrong paths: the exact four
    entries are pinned so that neither the old paths nor any new unlisted path
    can silently remain.
    """
    assert TEXT_ALLOWLIST == _EXPECTED_TEXT_ALLOWLIST, (
        f"TEXT_ALLOWLIST must be exactly the shared health module plus "
        f"crud/audit.py, crud/idempotency.py and crud/numbers.py.\n"
        f"  extra entries : {sorted(TEXT_ALLOWLIST - _EXPECTED_TEXT_ALLOWLIST)}\n"
        f"  missing entries: {sorted(_EXPECTED_TEXT_ALLOWLIST - TEXT_ALLOWLIST)}"
    )
