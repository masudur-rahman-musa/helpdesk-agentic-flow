"""The two embedding models and the seam that lets the merge be tested.

`Embedder` is a Protocol and `chunking.chunk_document` takes one; it never
constructs a model. Real embeddings do not land on 0.75
on demand, so the only way to test "merge while similarity >= 0.75" at its
boundary at all is against a fake whose similarities the case chooses. This
module holds the real implementation and the tests that exercise it live
alone in `test_rag_embedding.py`.

Both vectors are produced here, dense and sparse, though only the dense one
decides a merge (decision I). One module owns model loading, and 14c's Qdrant
upsert needs both vectors for the same chunk; splitting them would mean two
places that know where the cache lives and two chances to load a model twice.

`count_tokens` is on the protocol because the 400-token cap is measured by the
dense model's OWN tokenizer (decision C). The model truncates its input at 512
tokens, so a cap counted in characters or words would not control what the
model actually sees, and the fake used by the merge tests counts by the same
call the real one does.
"""

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

DENSE_MODEL = "BAAI/bge-small-en-v1.5"
"""the design's dense model. Pinned by
`test_rag_embedding.py::test_the_model_names_and_dimension_are_exactly_the_spec_values`."""

SPARSE_MODEL = "Qdrant/bm25"
"""the design's sparse model, for the lexical half of row 15's hybrid search."""

DENSE_DIMENSION = 384
"""`BAAI/bge-small-en-v1.5`'s output width. Pinned against the model itself by
`test_rag_embedding.py::test_the_dense_vector_has_the_models_dimension`, so a
model swap that changes the width fails here rather than in Qdrant."""


@dataclass(frozen=True)
class SparseVector:
    """A sparse embedding as Qdrant takes it: parallel indices and values.

    The two tuples are the same length by construction. 14c writes them into a
    named sparse vector on the point, so a pair of different lengths would be
    rejected by Qdrant on upsert with nothing here to explain it; pinned by
    `test_rag_embedding.py::test_the_sparse_vector_has_paired_indices_and_values`.
    """

    indices: tuple[int, ...]
    values: tuple[float, ...]


class Embedder(Protocol):
    """What `chunk_document` needs from a model, and nothing more.

    Three methods, batched: the merge asks for one embedding per sentence of a
    section at a time, and a per-sentence call would pay the model's fixed
    cost once per sentence of the corpus.
    """

    def embed_dense(self, texts: Sequence[str]) -> list[tuple[float, ...]]: ...

    def embed_sparse(self, texts: Sequence[str]) -> list[SparseVector]: ...

    def count_tokens(self, text: str) -> int: ...


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity of two dense vectors.

    Returns 0.0 when either vector has no magnitude, rather than dividing by
    zero: a zero vector is what an empty string embeds to, and the merge would
    otherwise raise on a document nobody would think of as malformed. Pinned
    by `test_rag_embedding.py::test_cosine_of_a_zero_vector_is_zero`.
    """
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    magnitude = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    if magnitude == 0.0:
        return 0.0
    return dot / magnitude


class FastEmbedEmbedder:
    """The real models, loaded from a cache directory inside this repo.

    `cache_dir` is passed explicitly rather than left to fastembed's default
    of `~/.cache` (decision E): a project rule says this project writes nothing
    outside its own folder. The directory is created if it is missing, because
    fastembed's download does not create its parent.

    Both models load on construction, which is why the suite builds this once
    in a session-scoped fixture: a cold cache downloads
    about 130 MB, and a warm one still costs seconds of ONNX session setup.
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
        """How many tokens the DENSE model will see in `text`.

        The model's own tokenizer, through fastembed's `token_count`, not a
        character or word heuristic (decision C): 400 is a cap on what reaches
        a model that truncates at 512, and a count measured in anything else
        does not bound that.
        """
        texts: Iterable[str] = [text]
        return int(self._dense.token_count(texts))
