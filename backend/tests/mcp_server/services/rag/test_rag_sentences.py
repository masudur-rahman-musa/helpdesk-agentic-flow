"""The sentence splitter in `helpdesk/rag/sentences.py`.

Expected implementation surface (the implementer must provide exactly this):

``helpdesk.rag.sentences``
    ``@dataclass(frozen=True) class Sentence`` with exactly these fields:
        ``text: str`` — the sentence INCLUDING the whitespace that follows it,
            because the sentences must partition their input.
        ``char_start: int`` / ``char_end: int`` — offsets into the WHOLE
            document, i.e. `offset` plus the position inside `text`.
    ``def split_sentences(text: str, offset: int = 0) -> list[Sentence]`` —
        pure: no I/O, no NLP dependency. Breaks on `.`, `!`, `?`
        followed by whitespace, guards a short abbreviation list, and treats a
        markdown list item, a table row and a blank-line-delimited block as
        their own sentence.

The invariant every chunk offset rests on is the partition:
``"".join(s.text for s in split_sentences(t)) == t``, the first sentence
starting at `offset`, each starting where the previous ended, and the last
ending at ``offset + len(t)``. A byte dropped between two sentences is a
citation that points at the wrong words once the store keeps the offsets.

Pure filesystem and pure function: no DB fixtures, no compose stack.
"""

from itertools import pairwise
from pathlib import Path

import pytest

from helpdesk.mcp_server.services.rag.markdown import split_sections
from helpdesk.mcp_server.services.rag.sentences import Sentence, split_sentences

REPO_ROOT = Path(__file__).resolve().parents[5]
CORPUS_FILES: tuple[Path, ...] = tuple(sorted(REPO_ROOT.glob("backend/data/policies/*/*/*.md")))

PROSE = (
    "Access is granted by the system owner. "
    "Requests are logged in the helpdesk queue. "
    "Approvals expire after ninety days.\n"
)

BULLETS = (
    "The following systems are in scope:\n"
    "\n"
    "- erp, the finance system\n"
    "- crm, the sales system\n"
    "- mes, the production system\n"
)

TABLE = (
    "| class | example |\n"
    "| --- | --- |\n"
    "| internal | the wiki |\n"
    "| confidential | contract terms |\n"
)

BLOCKS = "First block with no full stop\n\nSecond block with no full stop\n"

UNTERMINATED = "Access is granted by the owner. Requests are logged without a full stop"

PREFIX = "# Access Control Policy\n\nSome front matter.\n\n"

TEXTS: tuple[tuple[str, str], ...] = (
    ("prose", PROSE),
    ("bullets", BULLETS),
    ("table", TABLE),
    ("blocks", BLOCKS),
    ("unterminated", UNTERMINATED),
)


def _stripped(sentences: list[Sentence]) -> list[str]:
    return [sentence.text.strip() for sentence in sentences if sentence.text.strip()]


# --- the partition invariant ---------------------------------------------------


@pytest.mark.parametrize("text", [text for _, text in TEXTS], ids=[name for name, _ in TEXTS])
def test_the_sentences_of_a_text_partition_it_exactly(text: str) -> None:
    """Catches a splitter that returns stripped sentences and so silently
    drops the whitespace between them: the concatenation then differs from
    the source, and every chunk offset built on it is short by the number of
    separators before it."""
    sentences = split_sentences(text)

    assert sentences
    assert "".join(sentence.text for sentence in sentences) == text


@pytest.mark.parametrize("text", [text for _, text in TEXTS], ids=[name for name, _ in TEXTS])
def test_the_offsets_are_contiguous_and_span_the_text(text: str) -> None:
    """Catches offsets computed independently of the returned text — a
    splitter that reports the position of the MATCH (the full stop) rather
    than of the sentence start, or one that restarts counting per sentence.
    The partition case above cannot see that: it only reads `text`."""
    sentences = split_sentences(text)

    assert sentences[0].char_start == 0
    assert sentences[-1].char_end == len(text)
    for previous, current in pairwise(sentences):
        assert current.char_start == previous.char_end
    for sentence in sentences:
        assert sentence.char_end - sentence.char_start == len(sentence.text)


def test_the_offsets_index_the_whole_document_not_the_section() -> None:
    """Catches a splitter that ignores its `offset` argument (or adds it only
    to `char_start`): every sentence of every section after the first would
    then address text from the top of the file, which is exactly how a
    citation ends up quoting the wrong policy clause."""
    document = PREFIX + PROSE
    sentences = split_sentences(PROSE, len(PREFIX))

    assert sentences[0].char_start == len(PREFIX)
    assert sentences[-1].char_end == len(document)
    for sentence in sentences:
        assert document[sentence.char_start : sentence.char_end] == sentence.text


# --- what counts as a break ----------------------------------------------------


def test_prose_breaks_at_every_full_stop() -> None:
    """Catches a splitter that never breaks at all (returning the paragraph
    as one sentence), which would make the whole merge step in `chunking.py`
    a no-op while every partition assertion above stays green."""
    sentences = split_sentences(PROSE)

    assert _stripped(sentences) == [
        "Access is granted by the system owner.",
        "Requests are logged in the helpdesk queue.",
        "Approvals expire after ninety days.",
    ]


_ABBREVIATIONS: tuple[tuple[str, str], ...] = (
    ("e.g.", "Access is granted by the owner, e.g. the head of IT. Requests are logged."),
    ("i.e.", "The badge covers all doors, i.e. the external ones, too. Requests are logged."),
    ("etc.", "Laptops, phones, etc. are covered by this policy. Requests are logged."),
    ("Mr.", "Approvals are countersigned by Mr. Weber in finance. Requests are logged."),
    ("No.", "The change is tracked under No. forty-two in the queue. Requests are logged."),
    ("vs.", "Owner vs. approver is decided per system here. Requests are logged."),
)


@pytest.mark.parametrize(
    ("abbreviation", "text"), _ABBREVIATIONS, ids=[name for name, _ in _ABBREVIATIONS]
)
def test_an_abbreviation_does_not_end_a_sentence(abbreviation: str, text: str) -> None:
    """Catches a naive `.\\s` split: each of these would break mid-clause, so
    a chunk would be cut after "e.g." and the retrieved snippet would end on
    a dangling abbreviation. The second, genuine full stop in each row proves
    the splitter still breaks where it should, so a splitter that simply
    never breaks does not pass this row either."""
    sentences = split_sentences(text)

    assert len(sentences) == 2
    assert abbreviation in sentences[0].text
    assert sentences[1].text.strip() == "Requests are logged."


def test_each_list_item_is_its_own_sentence() -> None:
    """Catches a splitter that only breaks on terminal punctuation: a bullet
    list carries none, so the whole list would be one sentence and a long
    list of in-scope systems could never be split at the token cap."""
    sentences = split_sentences(BULLETS)

    assert _stripped(sentences) == [
        "The following systems are in scope:",
        "- erp, the finance system",
        "- crm, the sales system",
        "- mes, the production system",
    ]


def test_each_table_row_is_its_own_sentence() -> None:
    """Catches a splitter that treats a markdown table as one blob: a
    classification table would then be a single indivisible sentence, and one
    oversized chunk would swallow rows that answer different questions."""
    sentences = split_sentences(TABLE)

    assert _stripped(sentences) == [
        "| class | example |",
        "| --- | --- |",
        "| internal | the wiki |",
        "| confidential | contract terms |",
    ]


def test_a_blank_line_ends_a_block_even_without_punctuation() -> None:
    """Catches a splitter that requires punctuation to break: two unpunctuated
    paragraphs (a heading line and its following block, in corpus terms) would
    merge into one sentence, so a heading could never be the start of a
    chunk."""
    sentences = split_sentences(BLOCKS)

    assert _stripped(sentences) == [
        "First block with no full stop",
        "Second block with no full stop",
    ]


def test_a_text_ending_without_punctuation_still_yields_its_last_sentence() -> None:
    """Catches a splitter that emits a sentence only when it finds a
    terminator: the trailing clause would be dropped entirely, which the
    partition assertion catches only because that assertion exists — this row
    names the loss, and a section whose final line has no full stop is the
    common corpus shape."""
    sentences = split_sentences(UNTERMINATED)

    assert len(sentences) == 2
    assert sentences[-1].text.strip() == "Requests are logged without a full stop"
    assert sentences[-1].char_end == len(UNTERMINATED)


def test_empty_text_yields_no_sentences() -> None:
    """Catches a splitter that returns one empty-text sentence for an empty
    section: that sentence would embed to a zero vector and pollute
    retrieval (plan the named risk)."""
    assert split_sentences("") == []


def test_whitespace_only_text_yields_no_sentence_carrying_content() -> None:
    """Catches a splitter that emits a content-bearing sentence for a blank
    section. The exact expectation asserted: every returned sentence has
    `text.strip() == ""`, and the partition still holds — the whitespace is
    NOT asserted away, because dropping it would break the offset chain for
    the section that follows it."""
    blank = "   \n\n  "
    sentences = split_sentences(blank)

    assert all(sentence.text.strip() == "" for sentence in sentences)
    assert "".join(sentence.text for sentence in sentences) == blank


# --- the corpus ----------------------------------------------------------------


def test_the_corpus_has_the_forty_files_this_suite_claims_to_cover() -> None:
    """Catches a glob that silently matches nothing (a moved corpus, a wrong
    repo root), which would make the sweep below pass over an empty list."""
    assert len(CORPUS_FILES) == 40


@pytest.mark.parametrize(
    "path", CORPUS_FILES, ids=lambda p: f"{p.parent.parent.name}-{p.parent.name}-{p.stem}"
)
def test_every_section_of_every_corpus_file_partitions_into_sentences(path: Path) -> None:
    """Catches a splitter that loses or duplicates characters on shapes the
    handwritten cases above do not contain — a fenced block, a numbered
    heading, a table inside a list — on real policy text rather than on
    invented text. The per-sentence document slice is the half a
    concatenation check cannot see."""
    source = path.read_text(encoding="utf-8")

    for section in split_sections(source):
        sentences = split_sentences(section.text, section.char_start)
        assert "".join(sentence.text for sentence in sentences) == section.text, (
            f"{path}: section {section.heading_path} does not partition"
        )
        if not sentences:
            continue
        assert sentences[0].char_start == section.char_start, path
        assert sentences[-1].char_end == section.char_end, path
        for previous, current in pairwise(sentences):
            assert current.char_start == previous.char_end, path
        for sentence in sentences:
            assert source[sentence.char_start : sentence.char_end] == sentence.text, path
