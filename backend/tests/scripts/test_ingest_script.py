"""The ingest script resolves its corpus root as designed.

Two pure functions of `scripts/ingest.py` are under test and nothing else:

    helpdesk.mcp_server.cli._corpus_root(flag: Path | None, configured: Path) -> Path
    helpdesk.mcp_server.cli._parse(argv: list[str] | None) -> argparse.Namespace

`_corpus_root` exists so the "flag, or settings default?" answer is reachable
without building `Settings` from a live environment. The
script's own docstring says nothing in it is tested directly; this narrows
that sentence narrower rather than false, and these three cases are the whole
of what it now excludes.

No environment, no engine, no client, no backing service: every case is a
function call on two `Path` values. `scripts` is importable because
`backend/pyproject.toml`'s pytest `pythonpath` puts the repository root on
`sys.path`, which is how `test_seed.py` and `test_access_snapshot.py` already
reach `scripts.seed`.
"""

from pathlib import Path

from helpdesk.mcp_server.cli import _corpus_root, _parse

# Neither value is a real directory and neither has to be: `_corpus_root` picks
# between two paths and touches the filesystem for neither. The two are
# deliberately different strings so no case can pass by returning the wrong one.
SETTINGS_ROOT = Path("/srv/policies")
"""Stands in for `settings.policy_dir` as an adopter would set it: absolute,
and outside this repository."""

FLAG_ROOT = Path("/tmp/mine")
"""Stands in for an explicit `--corpus-root` on the command line."""


def test_the_corpus_root_falls_back_to_the_settings_value() -> None:
    """Catches an implementation that ignores `POLICY_DIR` entirely.

    Without the fallback the script keeps walking its own `CORPUS_ROOT`
    constant (`Path("policies")`), so an adopter who set `POLICY_DIR` to their
    own corpus gets this repository's seeded policies ingested under their
    tenant, or a `CorpusDirectoryNotFoundError` naming a directory they never
    configured. The configured value here is absolute and shares no component
    with `policies`, so a returned `Path("policies")` cannot compare equal by
    accident.
    """
    assert _corpus_root(None, SETTINGS_ROOT) == SETTINGS_ROOT


def test_an_explicit_corpus_root_flag_wins_over_the_settings_value() -> None:
    """Catches the opposite over-correction: a resolver that always answers
    with the settings value and drops the flag.

    That implementation passes the fallback case above, so only this row can
    see it. It would make `--corpus-root` silently inert, which is worse than
    removing the flag, because a one-off ingest from a scratch directory would
    quietly re-read the configured corpus and report success over the wrong
    documents.
    """
    assert _corpus_root(FLAG_ROOT, SETTINGS_ROOT) == FLAG_ROOT


def test_the_corpus_root_flag_defaults_to_none() -> None:
    """Catches leaving `--corpus-root`'s argparse default as the `CORPUS_ROOT`
    constant, which makes the fallback above unreachable in the real script.

    Both cases above would stay green with that default in place: they call
    `_corpus_root` directly and never go through the parser, so `None` never
    arrives and the configured value is never consulted at runtime. This is the
    only case that pins the seam between the two. It also pins that `_parse`
    still needs no environment, which risk 4 requires for `--help`: an argparse
    default of `load_settings().policy_dir` would raise here, with no
    `DATABASE_URL` set, long before the assertion is reached.
    """
    args = _parse(["--tenant", "example-tenant", "--version", "v1"])

    assert args.corpus_root is None
