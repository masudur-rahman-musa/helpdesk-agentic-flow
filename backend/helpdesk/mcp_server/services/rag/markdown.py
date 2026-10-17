"""Split a markdown document into sections by heading.

The sections cover the source exactly, end to end, because stored offsets
must point at the right words for a citation. A section starts at its own
heading line, so the heading words get embedded. A `#` inside a code fence
is not a heading.
"""

import re
from dataclasses import dataclass

_HEADING = re.compile(r"(?P<hashes>#{1,6})[ \t]+(?P<text>.*?)[ \t]*#*[ \t]*$")
"""An ATX heading line. Trailing `#`s are dropped; `#hashtag` is prose."""

_FENCE = re.compile(r"(?P<marker>```+|~~~+)")


@dataclass(frozen=True)
class Section:
    """One heading-delimited span of a document.

    `heading_path` is the enclosing headings, outermost first, and empty
    before the first heading. `text` is `source[char_start:char_end]`.
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
    """Split `markdown` by heading, recording the path and offsets.

    A document with no headings is one section, so it is still indexed.
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
                # A same-or-shallower heading pops back to its own level.
                del path[level - 1 :]
                path.append(text)
                starts.append((offset, tuple(path)))
        offset += len(line)

    if not starts or starts[0][0] != 0:
        # Text before the first heading has an empty path.
        starts.insert(0, (0, ()))

    ends = [start for start, _ in starts[1:]] + [len(markdown)]
    return [
        Section(heading_path=heading_path, char_start=start, char_end=end, text=markdown[start:end])
        for (start, heading_path), end in zip(starts, ends, strict=True)
    ]
