"""Ingest one tenant's policy corpus version into both stores.

    uv run python -m helpdesk.mcp_server.cli --tenant <slug> --version v1

All the work lives in the ingest pipeline, so an admin route can do the same.
The corpus root is the `POLICY_DIR` setting unless `--corpus-root` is given.
This is the one place the real redaction client and embedding model are built,
so tests use fakes. Exits 1 on the first typed refusal; each document has its
own transaction, so the refused one writes nothing.
"""

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

from helpdesk.mcp_server.services.rag.embedding import FastEmbedEmbedder
from helpdesk.mcp_server.services.rag.pipeline import ingest_version
from helpdesk.mcp_server.services.rag.redaction_client import RedactionClient
from helpdesk.mcp_server.services.rag.vectors import create_client
from helpdesk.shared.core.config import load_settings
from helpdesk.shared.core.db import create_engine, create_session_factory
from helpdesk.shared.core.errors import ToolError
from helpdesk.shared.crud.tenants import find_tenant_by_slug


def _corpus_root(flag: Path | None, configured: Path) -> Path:
    """The explicit flag, else `POLICY_DIR`.

    Not an argparse default, so `--help` works without database settings.
    """
    return configured if flag is None else flag


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tenant", required=True, help="tenant slug, as seeded")
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
            # One time for the whole run; a skipped file keeps its old one.
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
