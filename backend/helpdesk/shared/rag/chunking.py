"""the design's semantic chunking: merge adjacent sentences while they agree.

Composes 14a's `split_sections` output with `sentences.split_sentences` and an
`Embedder`. Pure of I/O: the only thing that could touch a network is the
embedder handed in, and this module never constructs one.

The rule, in one line: walk a section's sentences in order and keep adding the
next one while its dense embedding is at least `SIMILARITY_THRESHOLD` similar
to the last one added AND the merged text stays within `MAX_CHUNK_TOKENS`;
otherwise start a new chunk, carrying `OVERLAP_SENTENCES` of the previous one
with it.

Two invariants a reader should hold onto:

* A chunk never crosses a section boundary, so `heading_path` is single-valued
  per chunk and a citation can name the heading it came from. Pinned by
  `test_rag_chunking.py::test_no_chunk_overlaps_a_section_boundary`.
* The chunks COVER their section but do not partition it: neighbours share a
  sentence by design, so the lengths sum to more than the source. 14a's
  section-level partition assertion is not the test here; the replacement is
  the coverage invariant, run over all 40 corpus files by
  `test_rag_chunking.py::test_every_character_of_every_corpus_section_lies_in_some_chunk`.

Nothing here writes a `policy_chunks` row. A chunk row never exists without
its Qdrant point (14a decision F); both land in 14c.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from helpdesk.shared.rag.embedding import Embedder, SparseVector, cosine
from helpdesk.shared.rag.markdown import Section
from helpdesk.shared.rag.sentences import Sentence, has_content, split_sentences

SIMILARITY_THRESHOLD = 0.75
"""Cosine similarity at or above which two adjacent sentences stay in one
chunk. INCLUSIVE: exactly 0.75 merges. Pinned by
`test_rag_chunking.py::test_two_sentences_at_exactly_the_threshold_merge` and
its 0.7499 twin, and the value itself by
`test_rag_chunking.py::test_the_chunking_constants_are_exactly_the_planned_values`."""

MAX_CHUNK_TOKENS = 400
"""The cap on a merged chunk, counted by the dense model's own tokenizer
(decision C). The model truncates at 512, and 400 leaves room for the overlap
sentence the next chunk begins with."""

OVERLAP_SENTENCES = 1
"""How many sentences a chunk repeats from its predecessor in the same
section. Retrieval that lands on a chunk boundary otherwise loses the sentence
that gave the next one its subject."""


@dataclass(frozen=True)
class Chunk:
    """One embedded span of a document, ready for 14c's upsert.

    `char_start` and `char_end` index the whole (redacted) document and `text`
    is exactly that slice, so a citation resolves against the stored document
    without the chunk carrying its own copy of the section. Both vectors are
    carried because the point 14c writes needs the dense one for similarity
    and the sparse one for the lexical half of row 15's hybrid search.
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

    A group is closed for one of two reasons, and the two are not the same:
    similarity fell below the threshold, or the token cap was reached. The cap
    is checked against the text the merge WOULD produce, so a chunk is never
    emitted over the cap by a sentence that was already counted.

    A single sentence over the cap becomes its own group, alone and with no
    overlap on either side, rather than being dropped or cut mid-word: there
    is nothing to split it on, every alternative loses bytes the coverage
    invariant could not see, and pairing it with an overlap sentence would put
    a chunk over the cap for a reason that is not its own length. Pinned by
    `test_rag_chunking.py::test_a_single_sentence_over_the_cap_becomes_its_own_oversized_chunk`.
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
        # The overlap: the new chunk opens with the tail of the closed one, so
        # the sentence that gave this one its subject is still readable.
        overlap = current[-OVERLAP_SENTENCES:] if OVERLAP_SENTENCES else []
        current = [*overlap, index]

    if current:
        groups.append(current)
    return groups


def chunk_document(sections: Sequence[Section], embedder: Embedder) -> list[Chunk]:
    """Chunk every section, in document order.

    Sections are chunked one at a time and never merged across, which is what
    keeps `heading_path` single-valued. A section with no content of its own —
    a heading with no body under it — contributes nothing at all rather than a
    chunk whose text is a heading, which would embed to a vector that sits
    near every query and pollute retrieval; pinned by
    `test_rag_chunking.py::test_a_heading_with_no_body_yields_no_chunk`.

    Embeddings are requested once per section for the sentences and once per
    section for the chunks, rather than per sentence: the models cost a fixed
    amount per call and the corpus has thousands of sentences.
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
