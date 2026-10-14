"""An earlier change §6: the semantic merge in `helpdesk/rag/chunking.py`.

Expected implementation surface (the implementer must provide exactly this):

``helpdesk.rag.chunking``
    ``SIMILARITY_THRESHOLD = 0.75`` — merge while cosine is >= this (INCLUSIVE).
    ``MAX_CHUNK_TOKENS = 400`` — measured with ``embedder.count_tokens``.
    ``OVERLAP_SENTENCES = 1``
    ``@dataclass(frozen=True) class Chunk`` — ``heading_path: tuple[str, ...]``,
        ``char_start: int``, ``char_end: int``, ``text: str``,
        ``dense: tuple[float, ...]``, ``sparse: SparseVector``.
    ``def chunk_document(sections: Sequence[Section], embedder: Embedder) -> list[Chunk]``
        — never constructs an embedder (decision D), never merges across a
        section boundary, and indexes `char_start` / `char_end` into the WHOLE
        document, so ``text`` is the slice those offsets address.

Every case here runs against a deterministic FAKE embedder (decision D): real
embeddings never land on 0.75 on demand, so the inclusive boundary could not
be tested at all with the real model. The fake counts tokens as
whitespace-separated words in EVERY case in this file, so a token expectation
in one case reads the same way as in the next.

The threshold vectors are constructed to hit 0.75 EXACTLY in float
arithmetic: `(1,0,0,0,0)` has norm 1.0 and `(3,2,1,1,1)` has norm
sqrt(16) = 4.0, both exact, so their cosine is 3.0/4.0 with no rounding at
all. `test_the_fake_vectors_sit_on_and_just_below_the_threshold` pins that
premise before any merge case relies on it.

Chunks OVERLAP by one sentence, so they do not partition the source; the
corpus assertion here is the coverage invariant (every character in at least
one chunk), not 14a's partition.

Pure: no DB, no network, no model.
"""

import hashlib
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest

from helpdesk.mcp_server.services.rag.chunking import (
    MAX_CHUNK_TOKENS,
    OVERLAP_SENTENCES,
    SIMILARITY_THRESHOLD,
    Chunk,
    chunk_document,
)
from helpdesk.mcp_server.services.rag.embedding import SparseVector, cosine
from helpdesk.mcp_server.services.rag.markdown import Section, split_sections

REPO_ROOT = Path(__file__).resolve().parents[5]
CORPUS_FILES: tuple[Path, ...] = tuple(sorted(REPO_ROOT.glob("policies/*/*/*.md")))

# (1,0,0,0,0) has norm exactly 1.0; (3,2,1,1,1) has norm exactly sqrt(16) = 4.0.
# cosine = 3.0 / 4.0 = 0.75, with no floating-point slack at all.
AT_THRESHOLD = (3.0, 2.0, 1.0, 1.0, 1.0)
# The same vector with the shared component shaved: cosine lands just under.
JUST_BELOW = (2.9996, 2.0, 1.0, 1.0, 1.0)

TAG_VECTORS: Mapping[str, tuple[float, ...]] = {
    "alpha": (1.0, 0.0, 0.0, 0.0, 0.0),
    "beta": AT_THRESHOLD,
    "gamma": JUST_BELOW,
    "delta": (0.0, 1.0, 0.0, 0.0, 0.0),
}


class FakeEmbedder:
    """A deterministic `Embedder` whose similarities the case chooses.

    Token counting is whitespace words in every case of this file (decision
    D's fake side of decision C), so a 150-word sentence is 150 tokens
    wherever it appears.
    """

    def __init__(self, vector_for: Callable[[str], tuple[float, ...]]) -> None:
        self._vector_for = vector_for

    def embed_dense(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        return [self._vector_for(text) for text in texts]

    def embed_sparse(self, texts: Sequence[str]) -> list[SparseVector]:
        return [SparseVector(indices=(0, 1), values=(float(len(text)), 1.0)) for text in texts]

    def count_tokens(self, text: str) -> int:
        return len(text.split())


def _tag_vector(text: str) -> tuple[float, ...]:
    """The vector of the FIRST tag word occurring in `text`.

    Merged text carries several tags; taking the first makes the merged
    chunk's own vector deterministic.
    """
    positions = [(text.find(tag), tag) for tag in TAG_VECTORS if tag in text]
    if not positions:
        pytest.fail(f"no tag word in {text!r}; the case must tag every sentence")
    _, tag = min(positions)
    return TAG_VECTORS[tag]


def _hash_vector(text: str) -> tuple[float, ...]:
    """A deterministic pseudo-vector for corpus text, which carries no tags."""
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return tuple(byte / 255.0 + 0.001 for byte in digest[:8])


def _tagged() -> FakeEmbedder:
    return FakeEmbedder(_tag_vector)


def _hashed() -> FakeEmbedder:
    return FakeEmbedder(_hash_vector)


def _document(*bodies: tuple[tuple[str, ...], str]) -> tuple[str, list[Section]]:
    """Build a synthetic document and the sections that partition it."""
    source = ""
    sections: list[Section] = []
    for heading_path, text in bodies:
        start = len(source)
        source += text
        sections.append(
            Section(
                heading_path=heading_path,
                char_start=start,
                char_end=len(source),
                text=text,
            )
        )
    return source, sections


def _long_sentence(tag: str, words: int) -> str:
    """A sentence of exactly `words` whitespace tokens, ending in `. `."""
    filler = " ".join(["policy"] * (words - 2))
    return f"{tag} {filler} end. "


SHORT_MERGING = "Sentence about alpha. Sentence about beta.\n"
SHORT_SPLITTING = "Sentence about alpha. Sentence about gamma.\n"

S1 = _long_sentence("alpha", 150)
S2 = _long_sentence("alpha", 150)
S3 = _long_sentence("alpha", 150)
CAPPED_SECTION = S1 + S2 + S3

OVERSIZED = _long_sentence("alpha", 500).rstrip()
HEADING_ONLY = "## 2 The four classes\n\n"


# --- premises and constants -----------------------------------------------------


def test_the_chunking_constants_are_exactly_the_planned_values() -> None:
    """Catches a threshold, cap or overlap set to any other number — a cap
    above the dense model's 512-token input limit in particular, which would
    silently truncate what the model actually sees while every merge case
    below still passed."""
    assert SIMILARITY_THRESHOLD == 0.75
    assert MAX_CHUNK_TOKENS == 400
    assert OVERLAP_SENTENCES == 1


def test_the_fake_vectors_sit_on_and_just_below_the_threshold() -> None:
    """Not a test of the implementation: it pins the premise the two boundary
    cases below rest on. Without it, a `beta` pair that scored 0.7499999 by
    rounding would make the inclusive-boundary case fail against a CORRECT
    implementation, and the natural response to that red is to edit the
    expectation."""
    assert cosine(TAG_VECTORS["alpha"], TAG_VECTORS["beta"]) == 0.75
    assert 0.74 < cosine(TAG_VECTORS["alpha"], TAG_VECTORS["gamma"]) < 0.75


# --- the merge boundary ----------------------------------------------------------


def test_two_sentences_at_exactly_the_threshold_merge() -> None:
    """Catches a strict comparison (`> SIMILARITY_THRESHOLD`): the pair scores
    exactly 0.75, so an exclusive test splits it and every borderline pair in
    the corpus becomes two chunks. This is the only row in the file where
    the difference between `>` and `>=` decides the outcome."""
    source, sections = _document(((), SHORT_MERGING))

    chunks = chunk_document(sections, _tagged())

    assert len(chunks) == 1
    assert chunks[0].text == SHORT_MERGING
    assert chunks[0].char_start == 0
    assert chunks[0].char_end == len(source)


def test_two_sentences_just_below_the_threshold_do_not_merge() -> None:
    """Catches an implementation that merges unconditionally (or compares
    against a lower constant such as 0.7): the pair scores about 0.74996, so
    only a threshold at 0.75 keeps them apart. Asserted as "the first chunk
    does not contain the second sentence", which holds whether or not the
    implementation also opens the next chunk with an overlap sentence."""
    _, sections = _document(((), SHORT_SPLITTING))

    chunks = chunk_document(sections, _tagged())

    assert len(chunks) == 2
    assert chunks[0].text == "Sentence about alpha. "
    assert "gamma" not in chunks[0].text


def test_the_token_cap_ends_a_merge_that_similarity_would_have_continued() -> None:
    """Catches a merge loop that checks similarity only: all three sentences
    carry the same tag, so their cosine is exactly 1.0 and nothing but the
    400-token cap can stop the merge. Deleting the cap check yields one
    450-token chunk, which the dense model would truncate at 512 tokens
    without saying so."""
    embedder = _tagged()
    _, sections = _document(((), CAPPED_SECTION))

    assert cosine(_tag_vector(S1), _tag_vector(S2)) == 1.0
    assert embedder.count_tokens(S1) == 150

    chunks = chunk_document(sections, embedder)

    assert len(chunks) == 2
    assert chunks[0].text == S1 + S2
    assert embedder.count_tokens(chunks[0].text) == 300
    for chunk in chunks:
        assert embedder.count_tokens(chunk.text) <= MAX_CHUNK_TOKENS


def test_neighbouring_chunks_of_one_section_overlap_by_exactly_one_sentence() -> None:
    """Catches an implementation that drops the overlap: a question whose
    answer straddles the cut between two chunks would then match neither
    chunk well. The overlap must be exactly one sentence, so this also
    catches a two-sentence or half-sentence overlap. The cut here is made by
    the token cap, so the overlap is asserted at a boundary every reading of
    the rule agrees is a boundary."""
    source, sections = _document(((), CAPPED_SECTION))

    chunks = chunk_document(sections, _tagged())

    assert len(chunks) == 2
    shared = source[chunks[1].char_start : chunks[0].char_end]
    assert shared == S2
    assert chunks[1].text.startswith(S2)
    assert S3 in chunks[1].text


def test_no_chunk_overlaps_a_section_boundary() -> None:
    """Catches an overlap applied blindly between consecutive chunks: the
    first chunk of section two would then start inside section one, and its
    `heading_path` would label text governed by a different heading. The two
    sections' sentences share a tag, so similarity alone would happily merge
    them — only the section boundary can keep them apart."""
    source, sections = _document(
        (("One",), "# One\n\nSentence about alpha. "),
        (("Two",), "# Two\n\nSentence about alpha too. "),
    )

    chunks = chunk_document(sections, _tagged())

    boundary = sections[1].char_start
    second_section_chunks = [c for c in chunks if c.heading_path == ("Two",)]
    assert second_section_chunks
    assert second_section_chunks[0].char_start == boundary
    for chunk in chunks:
        assert source[chunk.char_start : chunk.char_end] == chunk.text
    for chunk in chunks:
        if chunk.heading_path == ("One",):
            assert chunk.char_end <= boundary


def test_every_chunk_carries_exactly_one_sections_heading_path() -> None:
    """Catches a chunk whose span crosses a section boundary while its
    `heading_path` still reads as a single path — the shape that makes a
    citation attribute one heading's words to another heading."""
    _, sections = _document(
        (("One",), "# One\n\nSentence about alpha. "),
        (("Two",), "# Two\n\nSentence about delta. "),
        (("Three",), "# Three\n\nSentence about beta. "),
    )
    spans = {s.heading_path: (s.char_start, s.char_end) for s in sections}

    chunks = chunk_document(sections, _tagged())

    assert chunks
    for chunk in chunks:
        assert chunk.heading_path in spans
        start, end = spans[chunk.heading_path]
        assert start <= chunk.char_start < chunk.char_end <= end


# --- the unhappy paths ------------------------------------------------------------


def test_a_single_sentence_over_the_cap_becomes_its_own_oversized_chunk() -> None:
    """Catches the three wrong answers to a sentence that cannot fit: dropping
    it (bytes vanish from the index), truncating it at 400 tokens, and cutting
    it mid-word. The exact text is asserted, so any of the three fails."""
    embedder = _tagged()
    _, sections = _document(((), OVERSIZED))

    chunks = chunk_document(sections, embedder)

    assert embedder.count_tokens(OVERSIZED) > MAX_CHUNK_TOKENS
    assert len(chunks) == 1
    assert chunks[0].text == OVERSIZED
    assert chunks[0].char_start == 0
    assert chunks[0].char_end == len(OVERSIZED)


def test_a_heading_with_no_body_yields_no_chunk() -> None:
    """Catches an implementation that emits a chunk per section regardless of
    content: the heading-only section would embed to a near-empty vector that
    matches everything weakly, which is plan §5's named retrieval-pollution
    risk. The neighbouring section proves the run produced chunks at all, so
    an implementation that returns nothing does not pass this row."""
    _, sections = _document(
        (("Two",), HEADING_ONLY),
        (("Two", "internal"), "### internal\n\nSentence about alpha. "),
    )

    chunks = chunk_document(sections, _tagged())

    assert [c.heading_path for c in chunks] == [("Two", "internal")]
    for chunk in chunks:
        assert chunk.text.strip()


def test_a_chunk_carries_both_its_dense_and_its_sparse_vector() -> None:
    """Catches a `Chunk` built with placeholder empty vectors (or with the
    dense vector only): 14c upserts both named vectors for one point, so an
    empty sparse half would make hybrid retrieval fall back to dense with
    nothing in the suite noticing."""
    _, sections = _document(((), SHORT_MERGING))

    chunks = chunk_document(sections, _tagged())

    chunk = chunks[0]
    assert chunk.dense == TAG_VECTORS["alpha"]
    assert isinstance(chunk.sparse, SparseVector)
    assert len(chunk.sparse.indices) == len(chunk.sparse.values)
    assert chunk.sparse.indices


# --- the corpus coverage invariant -------------------------------------------------


def test_the_corpus_has_the_forty_files_this_suite_claims_to_cover() -> None:
    """Catches a glob that silently matches nothing (a moved corpus, a wrong
    repo root), which would make the coverage sweep below pass over an empty
    list."""
    assert len(CORPUS_FILES) == 40


def _body_after_heading(section: Section) -> str:
    """A section's text with its own heading line removed."""
    if not section.heading_path:
        return section.text
    _, _, rest = section.text.partition("\n")
    return rest


@pytest.mark.parametrize(
    "path", CORPUS_FILES, ids=lambda p: f"{p.parent.parent.name}-{p.parent.name}-{p.stem}"
)
def test_every_character_of_every_corpus_section_lies_in_some_chunk(path: Path) -> None:
    """Catches an off-by-one at a chunk boundary and a merge loop that drops
    the tail of a section (the sentences left over after the last emitted
    chunk) — both invisible to the crafted cases, which are short enough to
    fit one or two chunks. Chunks overlap by design, so this is coverage, not
    14a's partition. The complement half is asserted too: a section that
    produced NO chunk must have no body, which stops the coverage half from
    being satisfied by an implementation that quietly skips whole sections."""
    source = path.read_text(encoding="utf-8")
    sections = split_sections(source)

    chunks: list[Chunk] = chunk_document(sections, _hashed())

    covered: set[int] = set()
    for chunk in chunks:
        assert source[chunk.char_start : chunk.char_end] == chunk.text, path
        covered.update(range(chunk.char_start, chunk.char_end))

    for section in sections:
        section_chunks = [
            chunk
            for chunk in chunks
            if chunk.char_start >= section.char_start and chunk.char_end <= section.char_end
        ]
        if not section_chunks:
            assert not _body_after_heading(section).strip(), (
                f"{path}: section {section.heading_path} has a body but produced no chunk"
            )
            continue
        uncovered = [
            index for index in range(section.char_start, section.char_end) if index not in covered
        ]
        assert not uncovered, (
            f"{path}: section {section.heading_path} leaves {len(uncovered)} characters in no chunk"
        )
