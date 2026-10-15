"""Static checks that prove an earlier change's web move into `src/`.

This file does not touch Postgres, Redis, Qdrant, any fixture or the network.
It only scans files on disk by their path and reads a few config files' text.

An earlier change (restructure An earlier change) gives `web/` a `src/` tree: `app/` holds routes
only under `src/app/`; `components/ui/*` stays as `src/components/ui/`; every
top-level shell component moves to `src/components/layout/`; each feature folder
gets `src/features/<name>/` holding its `components/`, `schema.ts`, `labels.ts`,
`format.ts` and the like; the kept `lib/` modules stay in `src/lib/`;
`middleware.ts` moves to `src/middleware.ts`. The `@/` alias repoints to
`./src`, and the tailwind content globs scan `./src`.

The happy paths prove the NEW locations are present; the unhappy paths prove
the OLD `web/app`, `web/components`, `web/lib` and `web/middleware.ts` are GONE
(no shim left behind) and that the alias and tailwind globs follow the move.

While the move is still pending every "present" case is red on purpose.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WEB = REPO_ROOT / "web"
SRC = WEB / "src"


def test_src_tree_exists() -> None:
    """Kills a move that never creates the src app, ui, layout, lib or a feature tree."""
    present = [
        SRC / "app",
        SRC / "components" / "ui",
        SRC / "components" / "layout",
        SRC / "lib",
        SRC / "features" / "chat" / "components",
    ]
    for path in present:
        assert path.is_dir(), f"expected src directory is missing: {path.relative_to(REPO_ROOT)}"


def test_features_laid_out() -> None:
    """Kills a move that drops a feature's schema, csv, format or labels file."""
    present = [
        SRC / "features" / "access" / "schema.ts",
        SRC / "features" / "admin" / "audit" / "csv.ts",
        SRC / "features" / "admin" / "killswitch" / "format.ts",
        SRC / "features" / "chat" / "decision-labels.ts",
    ]
    for path in present:
        assert path.is_file(), f"expected feature file is missing: {path.relative_to(REPO_ROOT)}"


def test_middleware_moved() -> None:
    """Kills a move that leaves middleware.ts out of src."""
    path = SRC / "middleware.ts"
    assert path.is_file(), f"expected file is missing: {path.relative_to(REPO_ROOT)}"


def test_old_tree_gone() -> None:
    """Kills a move that leaves the old app, components, lib or middleware behind."""
    gone = [
        WEB / "app",
        WEB / "components",
        WEB / "lib",
        WEB / "middleware.ts",
    ]
    for path in gone:
        assert not path.exists(), f"old path still present: {path.relative_to(REPO_ROOT)}"


def test_alias_points_at_src() -> None:
    """Kills an alias left at ./* instead of repointed to ./src/*."""
    text = (WEB / "tsconfig.json").read_text(encoding="utf-8")
    assert '"@/*": ["./src/*"]' in text, "tsconfig @/* is not mapped to ./src/*"
    assert '"@/*": ["./*"]' not in text, "old @/* -> ./* mapping is still present"


def test_tailwind_scans_src() -> None:
    """Kills content globs still naming the old app, components or lib roots."""
    text = (WEB / "tailwind.config.ts").read_text(encoding="utf-8")
    assert "./src/" in text, "tailwind content does not scan ./src/"
    for stale in ['"./app/', '"./components/', '"./lib/']:
        assert stale not in text, f"tailwind content still names a root path: {stale}"
