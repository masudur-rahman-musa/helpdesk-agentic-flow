"""Fixtures shared by the files in this folder.

The real embedder is built here, once per session: rebuilding both fastembed
models per test would re-read ~130 MB of weights for every case. Per-case
isolation is not needed, because an embedder holds no mutable state the cases
can see.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from helpdesk.mcp_server.services.rag.embedding import FastEmbedEmbedder
from helpdesk.shared.core.config import load_settings

REPO_ROOT = Path(__file__).resolve().parents[5]


@pytest.fixture(scope="session")
def embedder() -> FastEmbedEmbedder:
    """Both fastembed models, built once for the whole session."""
    try:
        settings = load_settings()
    except ValidationError as exc:
        pytest.fail(
            f"Settings are not configured (env or .env). Copy .env.example to .env.\n{exc}",
            pytrace=False,
        )
    cache_dir = Path(settings.embedding_cache_dir)
    if not cache_dir.is_absolute():
        cache_dir = REPO_ROOT / cache_dir
    return FastEmbedEmbedder(cache_dir)
