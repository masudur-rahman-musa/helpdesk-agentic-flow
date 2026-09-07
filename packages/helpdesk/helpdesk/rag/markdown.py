"""The structural half of the design's semantic chunking: split by heading.

Pure: no I/O, no DB, no network, no models. 14b splits each section further
by sentence and merges on embedding similarity; this module decides only
where a section begins and ends, and what headings enclose it.

The invariant everything downstream rests on is that the sections PARTITION
the source: the first starts at 0, each starts where the previous ended, and
the last ends at `len(markdown)`. Char offsets are stored on `policy_chunks`
and become a Qdrant payload, so a byte dropped or duplicated at a heading
boundary is a citation that points at the wrong words. Pinned per file over
the whole corpus by
`test_rag_markdown.py::test_the_sections_of_every_corpus_file_partition_the_source`,
with `test_every_section_of_every_corpus_file_round_trips` pinning the other
half — that `text` is the slice its own offsets address.

A section starts at the FIRST character of its own heading line, not after
it: the heading words are the ones a reader searches for, so they belong in
the text that gets embedded. Pinned by
`test_a_section_spans_from_its_own_heading_to_the_next_heading`.

ATX headings only (`#` … `######` at the start of a line), which is what the
corpus uses. A `#` inside a fenced code block is not a heading — pinned by
`test_a_hash_inside_a_fenced_code_block_is_not_a_heading` — because a fence
is the one place the corpus can contain a line that merely looks like one.
"""

import re
from dataclasses import dataclass

_HEADING = re.compile(r"(?P<hashes>#{1,6})[ \t]+(?P<text>.*?)[ \t]*#*[ \t]*$")
"""An ATX heading line, already known to start at a line boundary.

Trailing `#`s are the closed-ATX form and are not part of the heading text.
A bare `#` with no text is not a heading: `[ \\t]+` requires the space, and
`#hashtag` is prose.
"""

_FENCE = re.compile(r"(?P<marker>```+|~~~+)")


@dataclass(frozen=True)
class Section:
    """One heading-delimited span of a document.

    `heading_path` is the enclosing heading texts, outermost first, the last
    being this section's own heading; it is empty for a section that precedes
    the document's first heading (and for a document that has none at all).
    `text` is exactly `source[char_start:char_end]`.
    """

    heading_path: tuple[str, ...]
    char_start: int
    char_end: int
    text: str


def _heading_at(line: str) -> tuple[int, str] | None:
    """The (level, text) of an ATX heading line, or None."""
    stripped = line.rstrip("\n")
    if not stripped.startswith("#"):
        return None
    match = _HEADING.fullmatch(stripped)
    if match is None or not match.group("text"):
        return None
    return len(match.group("hashes")), match.group("text")


def split_sections(markdown: str) -> list[Section]:
    """Split `markdown` by heading hierarchy, recording the path and offsets.

    Never returns an empty list for a non-empty document: a headingless one
    is a single pathless section spanning the whole source, so a document
    with no headings is still indexed rather than silently dropped. Pinned by
    `test_a_document_with_no_headings_is_one_section_spanning_the_whole_source`.
    """
    starts: list[tuple[int, tuple[str, ...]]] = []
    path: list[str] = []
    in_fence = False
    fence_marker = ""
    offset = 0

    for line in markdown.splitlines(keepends=True):
        fence = _FENCE.match(line)
        if fence is not None:
            marker = fence.group("marker")
            if not in_fence:
                in_fence, fence_marker = True, marker[:3]
            elif marker.startswith(fence_marker):
                in_fence = False
        elif not in_fence:
            heading = _heading_at(line)
            if heading is not None:
                level, text = heading
                # A deeper heading nests; a same-or-shallower one pops back
                # to its own level first, so `## 2` after `### 1.1` is a
                # sibling of `## 1` rather than its child.
                del path[level - 1 :]
                path.append(text)
                starts.append((offset, tuple(path)))
        offset += len(line)

    if not starts or starts[0][0] != 0:
        # The preamble: body text before the first heading, governed by no
        # heading, so its path is empty rather than the first heading's.
        starts.insert(0, (0, ()))

    ends = [start for start, _ in starts[1:]] + [len(markdown)]
    return [
        Section(heading_path=heading_path, char_start=start, char_end=end, text=markdown[start:end])
        for (start, heading_path), end in zip(starts, ends, strict=True)
    ]
