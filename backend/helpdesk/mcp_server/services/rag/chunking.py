"""Semantic chunking: merge adjacent sentences while they agree.

Walk a section's sentences and keep adding the next while it is similar
enough to the last one and the chunk stays under the token cap; otherwise
start a new chunk that repeats the last sentence. A chunk never crosses a
heading, so a citation can name the heading it came from. No I/O: the only
model is the embedder passed in.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from helpdesk.mcp_server.services.rag.embedding import Embedder, SparseVector, cosine
from helpdesk.mcp_server.services.rag.markdown import Section
from helpdesk.mcp_server.services.rag.sentences import Sentence, has_content, split_sentences

SIMILARITY_THRESHOLD = 0.75
"""Cosine similarity at or above which two adjacent sentences stay in one
chunk. Exactly 0.75 merges."""

MAX_CHUNK_TOKENS = 400
"""The cap on a merged chunk, counted by the dense model's own tokenizer.
The model cuts input at 512, and 400 leaves room for the overlap sentence."""

OVERLAP_SENTENCES = 1
"""How many sentences a chunk repeats from the one before it, so a hit
near a chunk edge keeps the sentence that gave it its subject."""


@dataclass(frozen=True)
class Chunk:
    """One embedded span of a document, ready to store.

    `char_start` and `char_end` index the whole redacted document, so a
    citation resolves against the stored text. The sparse vector feeds the
    keyword half of hybrid search.
    """

    heading_path: tuple[str, ...]
    char_start: int
    char_end: int
    text: str
    dense: tuple[float, ...]
    sparse: SparseVector


def _groups(
    sentences: Sequence[Sentence],
    dense: Sequence[tuple[float, ...]],
    embedder: Embedder,
) -> list[list[int]]:
    """The sentence indices of each chunk of one section, in order.

    The cap is checked against the merged text before adding. A single
    sentence over the cap becomes its own chunk with no overlap, rather than
    being dropped or cut mid-word.
    """
    groups: list[list[int]] = []
    current: list[int] = []

    for index in range(len(sentences)):
        if embedder.count_tokens(sentences[index].text) > MAX_CHUNK_TOKENS:
            if current:
                groups.append(current)
            groups.append([index])
            current = []
            continue

        if not current:
            current = [index]
            continue

        previous = current[-1]
        merged = "".join(sentences[position].text for position in [*current, index])
        similar = cosine(dense[previous], dense[index]) >= SIMILARITY_THRESHOLD
        if similar and embedder.count_tokens(merged) <= MAX_CHUNK_TOKENS:
            current.append(index)
            continue

        groups.append(current)
        overlap = current[-OVERLAP_SENTENCES:] if OVERLAP_SENTENCES else []
        current = [*overlap, index]

    if current:
        groups.append(current)
    return groups


def chunk_document(sections: Sequence[Section], embedder: Embedder) -> list[Chunk]:
    """Chunk every section, in document order.

    A heading with no body gives no chunk: its vector would sit near every
    query. Embeddings are asked for per section, not per sentence, because
    each model call has a fixed cost.
    """
    chunks: list[Chunk] = []

    for section in sections:
        if not has_content(section.text):
            continue
        sentences = split_sentences(section.text, section.char_start)
        if not sentences:
            continue

        dense_by_sentence = embedder.embed_dense([sentence.text for sentence in sentences])
        groups = _groups(sentences, dense_by_sentence, embedder)

        texts = ["".join(sentences[index].text for index in group) for group in groups]
        dense_by_chunk = embedder.embed_dense(texts)
        sparse_by_chunk = embedder.embed_sparse(texts)

        for group, text, dense, sparse in zip(
            groups, texts, dense_by_chunk, sparse_by_chunk, strict=True
        ):
            chunks.append(
                Chunk(
                    heading_path=section.heading_path,
                    char_start=sentences[group[0]].char_start,
                    char_end=sentences[group[-1]].char_end,
                    text=text,
                    dense=dense,
                    sparse=sparse,
                )
            )

    return chunks
