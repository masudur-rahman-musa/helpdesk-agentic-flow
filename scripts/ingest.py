"""Ingest one tenant's policy corpus version into both stores.

    uv run python scripts/ingest.py --tenant nordlicht --version v1

Argument parsing, the corpus root, one line of output per document and the
exit code — nothing else. Every effect lives in `helpdesk.rag.pipeline`: the design calls ingestion "also an admin API", and a
later row's admin route can only call the same function this script does if
the script holds no logic of its own. That is also why almost nothing here is
tested directly - there is no behaviour to reach that `test_rag_pipeline.py`
does not already pin. The exception is the corpus root: `_corpus_root` and
`--corpus-root`'s default are the whole of what `test_ingest_script.py`
covers.

The root is `POLICY_DIR` (SPEC section 12.1, an earlier change) so the repository works
as a template: an adopter points that variable at their own folder and runs
`make ingest`. An explicit `--corpus-root` still wins. A walk that silently
found nothing would be worse than one that refuses, and `ingest_version`
refuses a missing directory rather than reporting zero.

This is the ONE place the real redaction client and the real embedding models
are constructed (decisions E and F), so every test runs against fakes with no
gateway reachable and no model loaded.

Exits 1 on the first typed refusal, printing its code and message. The
document it refused is not half-written: `ingest_version` opens one
transaction per document, so the files already ingested stay and the refused
one landed nothing.
"""

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

from helpdesk.shared.core.db import create_engine, create_session_factory
from helpdesk.shared.core.repository import find_tenant_by_slug
from helpdesk.shared.core.settings import load_settings
from helpdesk.shared.errors import ToolError
from helpdesk.mcp_server.services.rag.embedding import FastEmbedEmbedder
from helpdesk.mcp_server.services.rag.redaction_client import RedactionClient
from helpdesk.mcp_server.services.rag.pipeline import ingest_version
from helpdesk.mcp_server.services.rag.vectors import create_client


def _corpus_root(flag: Path | None, configured: Path) -> Path:
    """The corpus root to walk: the explicit flag, else `POLICY_DIR`.

    A function rather than an argparse default so the fallback is reachable
    without a loaded `Settings`, and so `--help` keeps
    working in an environment with no `DATABASE_URL`: the flag's default stays
    `None` and settings are read inside `_run`. Pinned by
    `test_ingest_script.py::test_the_corpus_root_falls_back_to_the_settings_value`,
    `::test_an_explicit_corpus_root_flag_wins_over_the_settings_value` and
    `::test_the_corpus_root_flag_defaults_to_none`.
    """
    return configured if flag is None else flag


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tenant", required=True, help="tenant slug, e.g. nordlicht")
    parser.add_argument("--version", required=True, help="version directory, e.g. v1")
    parser.add_argument(
        "--corpus-root",
        type=Path,
        default=None,
        help="corpus root directory (default: the POLICY_DIR setting)",
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    settings = load_settings()
    corpus_root = _corpus_root(args.corpus_root, settings.policy_dir)
    engine = create_engine(settings.database_url)
    client = create_client(settings)
    redactor = RedactionClient.from_settings(settings)
    embedder = FastEmbedEmbedder(settings.embedding_cache_dir)

    try:
        factory = create_session_factory(engine)
        async with factory() as session:
            tenant = await find_tenant_by_slug(session, args.tenant)
        if tenant is None:
            print(f"error VALIDATION: no tenant with slug {args.tenant!r}", file=sys.stderr)
            return 1

        results = await ingest_version(
            factory,
            client,
            tenant_id=tenant.id,
            corpus_root=corpus_root,
            tenant_slug=args.tenant,
            version=args.version,
            redactor=redactor,
            embedder=embedder,
            # One instant for the whole walk, so a run is one timestamp and a
            # skipped file keeps the instant it was actually last read.
            ingested_at=datetime.now(UTC),
        )
    except ToolError as exc:
        print(f"error {exc.code.value}: {exc}", file=sys.stderr)
        return 1
    finally:
        await redactor.aclose()
        await client.close()
        await engine.dispose()

    for result in results:
        state = "ingested" if result.created else "unchanged"
        print(f"{state} {result.document.slug} ({result.chunk_count} chunks)")
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_run(_parse(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
