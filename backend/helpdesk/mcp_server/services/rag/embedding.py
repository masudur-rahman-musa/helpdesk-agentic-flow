"""The two embedding models, behind a protocol so the merge can be tested.

Real embeddings never land on 0.75 on demand, so the chunker takes an
`Embedder` and tests pass a fake. One module loads both models, so the
cache path and model loading live in one place.
"""

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

DENSE_MODEL = "BAAI/bge-small-en-v1.5"
"""The dense model."""

SPARSE_MODEL = "Qdrant/bm25"
"""The sparse model, for the keyword half of hybrid search."""

DENSE_DIMENSION = 384
"""The dense model's output width. A model swap that changes it should
fail here, not in the vector store."""


@dataclass(frozen=True)
class SparseVector:
    """A sparse embedding as Qdrant takes it: parallel indices and values of
    the same length."""

    indices: tuple[int, ...]
    values: tuple[float, ...]


class Embedder(Protocol):
    """What `chunk_document` needs from a model. Batched, since each model
    call has a fixed cost."""

    def embed_dense(self, texts: Sequence[str]) -> list[tuple[float, ...]]: ...

    def embed_sparse(self, texts: Sequence[str]) -> list[SparseVector]: ...

    def count_tokens(self, text: str) -> int: ...


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity of two dense vectors; 0.0 when either is all zeros.

    An empty string embeds to zeros, and that must not divide by zero.
    """
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    magnitude = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    if magnitude == 0.0:
        return 0.0
    return dot / magnitude


class FastEmbedEmbedder:
    """The real models, loaded from a cache directory inside this repo.

    The cache is passed in so nothing is written outside the project.
    Loading is slow (a download, then ONNX setup), so build this once.
    """

    def __init__(self, cache_dir: Path | str) -> None:
        from fastembed import SparseTextEmbedding, TextEmbedding

        directory = Path(cache_dir)
        directory.mkdir(parents=True, exist_ok=True)
        self._dense = TextEmbedding(model_name=DENSE_MODEL, cache_dir=str(directory))
        self._sparse = SparseTextEmbedding(model_name=SPARSE_MODEL, cache_dir=str(directory))

    def embed_dense(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        """One dense vector per text, in the order given."""
        return [
            tuple(float(value) for value in vector) for vector in self._dense.embed(list(texts))
        ]

    def embed_sparse(self, texts: Sequence[str]) -> list[SparseVector]:
        """One sparse vector per text, in the order given."""
        return [
            SparseVector(
                indices=tuple(int(index) for index in vector.indices),
                values=tuple(float(value) for value in vector.values),
            )
            for vector in self._sparse.embed(list(texts))
        ]

    def count_tokens(self, text: str) -> int:
        """How many tokens the dense model will see in `text`.

        Counted by the model's own tokenizer, since the cap must bound what the
        model reads.
        """
        texts: Iterable[str] = [text]
        return int(self._dense.token_count(texts))
