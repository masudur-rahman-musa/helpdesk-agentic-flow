"""The sentence half of the design's semantic chunking, with offsets.

Pure: `re` only. No I/O, no models, no network.

A regex splitter rather than an NLP dependency. The
corpus is policy prose, headings, bullets and tables, not free text, and a
sentence model would be a package and a download for one regex's worth of
value.

The invariant everything downstream rests on is the same one
`markdown.split_sections` holds one level up: the sentences PARTITION their
input, whatever that input contains. The first starts at `offset`, each starts
where the previous ended, the last ends at `offset + len(text)`, and joining
their texts reproduces the input exactly — including a section that is nothing
but whitespace, which yields one sentence carrying that whitespace rather than
nothing at all. Chunk offsets become a Qdrant payload in 14c and a citation
the user reads, so a character dropped at a boundary is a citation pointing at
the wrong words. Pinned by
`test_rag_sentences.py::test_the_sentences_of_a_text_partition_it_exactly` and,
for the degenerate input,
`test_rag_sentences.py::test_whitespace_only_text_yields_no_sentence_carrying_content`.

Deciding what is worth EMBEDDING is `chunking`'s job, not this module's: a
splitter that dropped the empty cases would have to break the partition to do
it.

Whitespace between sentences belongs to the sentence it follows, which is what
makes the partition hold without a separate "gap" concept: a split point is
placed after the run of whitespace that follows a terminator, never before it.

A heading line is NOT a sentence of its own. It is carried forward into the
sentence that follows it, so the heading words — the ones a reader searches
for, which is why 14a puts them inside the section in the first place — are
embedded together with the body they head, and a heading with no body under it
produces nothing worth embedding at all. Pinned one level up by
`test_rag_chunking.py::test_a_heading_with_no_body_yields_no_chunk`.

A line that is a markdown list item, a table row or a block quote IS its own
sentence whatever punctuation it carries
(`test_rag_sentences.py::test_each_list_item_is_its_own_sentence`,
`test_rag_sentences.py::test_each_table_row_is_its_own_sentence`). A bullet
rarely ends in a full stop, so the prose rule alone would glue a whole list
into one sentence and hand the merge step a block it cannot subdivide.
"""

import re
from dataclasses import dataclass

_ABBREVIATIONS = frozenset(
    {
        "e.g.",
        "i.e.",
        "etc.",
        "vs.",
        "approx.",
        "no.",
        "mr.",
        "mrs.",
        "ms.",
        "dr.",
        "prof.",
        "inc.",
        "ltd.",
        "dept.",
        "fig.",
        "sec.",
    }
)
"""The short guard list decision H names, lowercased for comparison.

Short on purpose: every entry is a case where a full stop is followed by a
space and a capital in ordinary policy prose ("e.g. Finance"). A longer list
would start refusing real sentence ends. Pinned by
`test_rag_sentences.py::test_an_abbreviation_does_not_end_a_sentence`.
"""

_TERMINATOR = re.compile(r"[.!?]['\")\]]*(?=\s|$)")
"""A sentence terminator, allowing a closing quote or bracket after it."""

_HEADING_LINE = re.compile(r"[ \t]*#{1,6}[ \t]+\S")
"""An ATX heading line. Matched at the start of a line only, and matched the
way `markdown._HEADING` matches: the space after the hashes is required, so
`#hashtag` is prose."""

_BLOCK_LINE = re.compile(r"[ \t]*(?:[-*+]|\d+[.)]|\||>)[ \t]")
"""A line that is structure rather than prose: a bullet, a numbered item, a
table row or a block quote. Headings are deliberately absent — they are
carried into the following sentence instead of standing alone."""


@dataclass(frozen=True)
class Sentence:
    """One sentence, with offsets into the WHOLE document.

    `char_start` and `char_end` are absolute — `split_sentences` is called per
    section with that section's `char_start` as `offset` — so a chunk built
    from these can be sliced out of the source document without knowing which
    section it came from. `text` is exactly the slice they address.
    """

    text: str
    char_start: int
    char_end: int


def has_content(text: str) -> bool:
    """Whether `text` holds anything worth embedding.

    A heading line and a blank line are both structure: a span made only of
    those says nothing a retrieval query can match, and embedding it produces
    a vector that sits near everything. `chunking` reads this to decide
    whether a section contributes a chunk at all; the splitter reads it to
    decide what to carry forward. Pinned through its callers by
    `test_rag_chunking.py::test_a_heading_with_no_body_yields_no_chunk`.
    """
    return any(line.strip() and _HEADING_LINE.match(line) is None for line in text.splitlines())


def _last_word(text: str) -> str:
    """The final whitespace-delimited word of `text`, lowercased."""
    stripped = text.rstrip()
    if not stripped:
        return ""
    return stripped.rsplit(maxsplit=1)[-1].lower()


def _line_bounds(text: str) -> list[tuple[int, int]]:
    """Every line's (start, end) with its line break, in order."""
    bounds: list[tuple[int, int]] = []
    position = 0
    for line in text.splitlines(keepends=True):
        bounds.append((position, position + len(line)))
        position += len(line)
    return bounds


def _structural_cuts(text: str) -> set[int]:
    """Cut points that structure forces, before any content rule applies.

    A block line (bullet, table row, quote) is a sentence by itself, so both
    its start and its end are cut points. A heading line is cut the same way,
    not because it is a sentence but so it can be carried into the next one as
    a whole. A blank line ends whatever came before it, which is what makes a
    paragraph with no terminal punctuation still close (
    `test_rag_sentences.py::test_a_blank_line_ends_a_block_even_without_punctuation`).
    """
    cuts: set[int] = set()
    for start, end in _line_bounds(text):
        line = text[start:end]
        if not line.strip():
            cuts.add(end)
            continue
        if _BLOCK_LINE.match(line) is not None or _HEADING_LINE.match(line) is not None:
            cuts.add(start)
            cuts.add(end)
    return cuts


def split_sentences(text: str, offset: int = 0) -> list[Sentence]:
    """Split `text` into sentences carrying absolute offsets.

    Returns `[]` only for the empty string
    (`test_rag_sentences.py::test_empty_text_yields_no_sentences`). Anything
    else partitions: a text with no content of its own — whitespace, or a
    heading with nothing under it — comes back as ONE sentence spanning all of
    it, so the offset chain into the next section is unbroken and `chunking`
    is the layer that decides not to embed it.

    Trailing whitespace after the last terminator stays attached to the last
    sentence: dropping it would break the partition, and it is invisible in a
    citation either way.
    """
    if not text:
        return []

    cuts = _structural_cuts(text)
    for match in _TERMINATOR.finditer(text):
        end = match.end()
        if _last_word(text[:end]) in _ABBREVIATIONS:
            continue
        # The whitespace run after the terminator belongs to this sentence,
        # so the next one starts on its first real character.
        while end < len(text) and text[end].isspace():
            end += 1
        cuts.add(end)

    boundaries = sorted(cut for cut in cuts if 0 < cut < len(text))
    starts = [0, *boundaries]
    ends = [*boundaries, len(text)]

    sentences: list[Sentence] = []
    pending_start: int | None = None
    for start, end in zip(starts, ends, strict=True):
        if pending_start is None:
            pending_start = start
        if not has_content(text[pending_start:end]):
            # A heading, or a run of blank lines: carried into the sentence
            # that follows, which keeps the partition and keeps the heading
            # words in the embedded text.
            continue
        sentences.append(
            Sentence(
                text=text[pending_start:end],
                char_start=offset + pending_start,
                char_end=offset + end,
            )
        )
        pending_start = None

    if pending_start is not None:
        if sentences:
            # Trailing structure with no sentence after it joins the last one.
            last = sentences[-1]
            sentences[-1] = Sentence(
                text=text[last.char_start - offset : len(text)],
                char_start=last.char_start,
                char_end=offset + len(text),
            )
        else:
            sentences.append(
                Sentence(
                    text=text[pending_start:],
                    char_start=offset + pending_start,
                    char_end=offset + len(text),
                )
            )
    return sentences
