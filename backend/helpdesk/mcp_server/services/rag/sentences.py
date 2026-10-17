"""Split a section into sentences, with offsets.

A regex, not an NLP package: the corpus is policy prose, lists and tables.
The sentences cover their input exactly, end to end, so stored offsets
point at the right words. Whitespace after a sentence belongs to it. A
heading is carried into the next sentence so its words get embedded with
the body. A list item, table row or quote is its own sentence, since
bullets rarely end in a full stop.
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
"""Words whose full stop does not end a sentence, lowercased. Kept short:
a longer list would start missing real sentence ends."""

_TERMINATOR = re.compile(r"[.!?]['\")\]]*(?=\s|$)")
"""A sentence terminator, allowing a closing quote or bracket after it."""

_HEADING_LINE = re.compile(r"[ \t]*#{1,6}[ \t]+\S")
"""An ATX heading line at the start of a line; `#hashtag` is prose."""

_BLOCK_LINE = re.compile(r"[ \t]*(?:[-*+]|\d+[.)]|\||>)[ \t]")
"""A bullet, numbered item, table row or block quote. Headings are not
here: they join the next sentence instead of standing alone."""


@dataclass(frozen=True)
class Sentence:
    """One sentence, with offsets into the whole document, so a chunk can be
    cut from the source without knowing its section."""

    text: str
    char_start: int
    char_end: int


def has_content(text: str) -> bool:
    """Whether `text` holds anything worth embedding.

    Headings and blank lines alone embed to a vector near every query.
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
    """Cut points that structure forces, before any punctuation rule.

    A blank line ends what came before it, so a paragraph with no full stop
    still closes.
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

    Returns `[]` only for the empty string. Text with no content comes back
    as one sentence, so offsets stay unbroken; `chunking` skips it later.
    """
    if not text:
        return []

    cuts = _structural_cuts(text)
    for match in _TERMINATOR.finditer(text):
        end = match.end()
        if _last_word(text[:end]) in _ABBREVIATIONS:
            continue
        # Whitespace after the terminator belongs to this sentence.
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
            # A heading or blank lines: carried into the next sentence.
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
