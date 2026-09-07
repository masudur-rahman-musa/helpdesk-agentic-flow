"""An earlier change §6: the real embedder in `helpdesk/rag/embedding.py`.

Expected implementation surface (the implementer must provide exactly this):

``helpdesk.rag.embedding``
    ``DENSE_MODEL = "BAAI/bge-small-en-v1.5"``
    ``SPARSE_MODEL = "Qdrant/bm25"``
    ``DENSE_DIMENSION = 384``
    ``@dataclass(frozen=True) class SparseVector`` — ``indices: tuple[int, ...]``,
        ``values: tuple[float, ...]``.
    ``class Embedder(Protocol)`` — ``embed_dense(texts: Sequence[str]) ->
        list[tuple[float, ...]]``, ``embed_sparse(texts: Sequence[str]) ->
        list[SparseVector]``, ``count_tokens(text: str) -> int``.
    ``class FastEmbedEmbedder`` — ``__init__(self, cache_dir: Path | str) -> None``,
        implements ``Embedder``, loads BOTH models from `cache_dir`.
    ``def cosine(a: Sequence[float], b: Sequence[float]) -> float``

This is the only file in an earlier change that loads a model. Both fastembed models
are built in ONE session-scoped fixture: rebuilding them
per test would re-read ~130 MB of weights for every case. Per-case isolation
is not needed at all here, because an embedder holds no mutable state the
cases can see.

The cache directory comes from `Settings.embedding_cache_dir`, resolved
against the repo root, so fastembed never writes to `~/.cache` (decision E,
a project rule).
"""

from pathlib import Path

import pytest
from helpdesk.core.settings import load_settings
from helpdesk.rag.embedding import (
    DENSE_DIMENSION,
    DENSE_MODEL,
    SPARSE_MODEL,
    FastEmbedEmbedder,
    SparseVector,
    cosine,
)
from pydantic import ValidationError

REPO_ROOT = Path(__file__).resolve().parents[3]

# Two sentences about genuinely different things, both in the corpus register.
BADGE_SENTENCE = "A lost badge is reported to reception and blocked the same day."
BACKUP_SENTENCE = "Database backups are encrypted and retained for ninety days."

KNOWN_STRING = "Access to the finance system is granted by the system owner."


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


# --- constants -----------------------------------------------


def test_the_model_names_and_dimension_are_exactly_the_spec_values() -> None:
    """Catches a swap to a different model or dimension: the Qdrant
    collection 14c creates is sized from DENSE_DIMENSION, so a mismatch
    between the constant and the model would surface as a rejected upsert
    against a collection that is already live."""
    assert DENSE_MODEL == "BAAI/bge-small-en-v1.5"
    assert SPARSE_MODEL == "Qdrant/bm25"
    assert DENSE_DIMENSION == 384


# --- cosine, which needs no model ----------------------------------------------


def test_cosine_normalises_both_vectors() -> None:
    """Catches a `cosine` implemented as a bare dot product: (3,0)·(6,0) is
    18, not 1.0, so an unnormalised version would compare every pair against
    the 0.75 threshold on a scale that depends on vector length, and the
    merge rule would become "merge whenever the vectors are long"."""
    assert cosine((3.0, 0.0), (6.0, 0.0)) == 1.0
    assert cosine((3.0, 0.0), (0.0, 4.0)) == 0.0
    assert cosine((3.0, 4.0), (0.0, 5.0)) == pytest.approx(0.8)


def test_cosine_is_symmetric_and_signed() -> None:
    """Catches a `cosine` that returns an absolute value or a distance
    (1 - similarity): both invert the merge rule, the first silently merging
    opposite sentences and the second merging nothing at all."""
    assert cosine((1.0, 0.0), (-1.0, 0.0)) == -1.0
    assert cosine((3.0, 4.0), (0.0, 5.0)) == cosine((0.0, 5.0), (3.0, 4.0))


# --- the real models ------------------------------------------------------------


def test_the_dense_vector_has_the_models_dimension(embedder: FastEmbedEmbedder) -> None:
    """Catches an embedder wired to a different dense model (a 768-dimension
    one is the easy mistake) and one that returns the raw fastembed generator
    rather than a list of fixed-width vectors."""
    vectors = embedder.embed_dense([BADGE_SENTENCE, BACKUP_SENTENCE])

    assert len(vectors) == 2
    for vector in vectors:
        assert len(vector) == DENSE_DIMENSION


def test_the_sparse_vector_has_paired_indices_and_values(
    embedder: FastEmbedEmbedder,
) -> None:
    """Catches an embedder that builds `SparseVector` from the wrong halves of
    fastembed's output (indices and values swapped or truncated): Qdrant
    accepts only equal-length pairs, so 14c's upsert would fail for reasons
    invisible at this layer."""
    sparse = embedder.embed_sparse([BADGE_SENTENCE])

    assert len(sparse) == 1
    vector = sparse[0]
    assert isinstance(vector, SparseVector)
    assert len(vector.indices) == len(vector.values)
    assert vector.indices, "a policy sentence must produce at least one bm25 term"


def test_a_text_is_maximally_similar_to_itself(embedder: FastEmbedEmbedder) -> None:
    """Catches an embedder that returns a fresh random or zero vector per
    call (a stubbed model, a shuffled batch): identical text must score 1.0,
    and nothing else in this file would notice non-determinism."""
    first, second = embedder.embed_dense([BADGE_SENTENCE, BADGE_SENTENCE])

    assert cosine(first, second) == pytest.approx(1.0, abs=1e-6)


def test_two_unrelated_policy_sentences_score_below_the_merge_threshold(
    embedder: FastEmbedEmbedder,
) -> None:
    """Catches an embedder that returns the same vector for every input (the
    shape a mis-wired batch call produces): everything would then score 1.0,
    every document would merge into one chunk, and the 0.75 threshold in
    `chunking.py` would never fire on real text."""
    badge, backup = embedder.embed_dense([BADGE_SENTENCE, BACKUP_SENTENCE])

    assert cosine(badge, backup) < 0.75


def test_the_batch_order_follows_the_input_order(embedder: FastEmbedEmbedder) -> None:
    """Catches an embedder that reorders a batch (fastembed's parallel path
    can): sentence i would get sentence j's vector, so the merge decisions in
    `chunking.py` would be made against the wrong neighbour while every
    length assertion above stays green."""
    batch = embedder.embed_dense([BADGE_SENTENCE, BACKUP_SENTENCE])
    single_badge = embedder.embed_dense([BADGE_SENTENCE])[0]
    single_backup = embedder.embed_dense([BACKUP_SENTENCE])[0]

    assert cosine(batch[0], single_badge) == pytest.approx(1.0, abs=1e-6)
    assert cosine(batch[1], single_backup) == pytest.approx(1.0, abs=1e-6)


def test_count_tokens_uses_a_real_tokenizer(embedder: FastEmbedEmbedder) -> None:
    """The exact count is not pinned here: it is a property of the
    `BAAI/bge-small-en-v1.5` vocabulary and cannot be known without running
    the model, so writing a number would be inventing one. The property this
    asserts instead — strictly more tokens than whitespace-separated words,
    strictly fewer than characters — still catches the two implementations
    decision C rejects: `len(text.split())` (a word count, which under-counts
    sub-word pieces and would let a 400-token chunk exceed the model's 512
    limit) and a character heuristic."""
    words = len(KNOWN_STRING.split())
    count = embedder.count_tokens(KNOWN_STRING)

    assert words < count < len(KNOWN_STRING)


def test_count_tokens_grows_with_the_text(embedder: FastEmbedEmbedder) -> None:
    """Catches a `count_tokens` returning a constant (the model's max length,
    or 0), which the band above would not catch on its own and which would
    make the 400-token cap in `chunking.py` either always or never bite."""
    short = embedder.count_tokens(BADGE_SENTENCE)
    long = embedder.count_tokens(BADGE_SENTENCE + " " + BACKUP_SENTENCE)

    assert long > short


def test_the_models_are_cached_inside_the_repository() -> None:
    """Catches a cache directory resolved against the process working
    directory or defaulted to `~/.cache/fastembed`: a project rule forbids this
    project writing ~130 MB outside its own folder, and nothing else in the
    suite would notice where the weights landed."""
    settings = load_settings()
    cache_dir = Path(settings.embedding_cache_dir)
    resolved = cache_dir if cache_dir.is_absolute() else REPO_ROOT / cache_dir

    assert resolved.resolve().is_relative_to(REPO_ROOT.resolve())
