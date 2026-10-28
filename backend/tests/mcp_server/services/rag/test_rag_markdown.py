"""The structural split in `helpdesk/rag/markdown.py`.

Expected implementation surface (the implementer must provide exactly this):

``helpdesk.rag.markdown``
    ``@dataclass(frozen=True) class Section`` with exactly these fields:
        ``heading_path: tuple[str, ...]`` — the enclosing heading TEXTS,
            outermost first, the innermost being this section's own heading;
            empty for a section that precedes the document's first heading.
        ``char_start: int`` / ``char_end: int`` — offsets into the source
            string; the section starts at the first character of its own
            heading line and ends where the next heading line begins.
        ``text: str`` — exactly ``source[char_start:char_end]``.
    ``def split_sections(markdown: str) -> list[Section]`` — pure: no I/O, no
        DB, no network. ATX headings only (`#`…`######` at the start of a
        line); a `#` inside a fenced code block is not a heading. The
        sections partition the source: the first starts at 0, each starts
        where the previous ended, and the last ends at ``len(markdown)``.

The corpus round-trip is asserted PER SECTION over all 40 files under
`backend/data/policies/*/*/*.md` (risk: bytes lost or duplicated at a heading
boundary), plus the partition property, which is the half a per-section
equality alone cannot see.

Pure filesystem and pure function: no DB fixtures, no compose stack.
"""

from itertools import pairwise
from pathlib import Path

import pytest

from helpdesk.mcp_server.services.rag.markdown import Section, split_sections

REPO_ROOT = Path(__file__).resolve().parents[5]
CORPUS_FILES: tuple[Path, ...] = tuple(sorted(REPO_ROOT.glob("backend/data/policies/*/*/*.md")))

NESTED = """# Access Control Policy

Front matter line.

## 1. Purpose

Purpose body.

### 1.1 Detail

Detail body.

## 2. Scope

Scope body.
"""

FENCED = """# Title

Intro paragraph.

```text
# not a heading
## also not a heading
```

## Real Section

Body.
"""

HEADINGLESS = "Just a paragraph of policy prose.\n\nAnd a second one, with a # in the middle.\n"

PREAMBLE = """Body text before any heading at all.

# The First Heading

Its body.
"""


def _paths(sections: list[Section]) -> list[tuple[str, ...]]:
    return [section.heading_path for section in sections]


def test_the_corpus_has_the_forty_files_this_suite_claims_to_cover() -> None:
    """Catches a glob that silently matches nothing (a moved corpus, a wrong
    repo root), which would make every round-trip case below pass by
    iterating over an empty list."""
    assert len(CORPUS_FILES) == 40


@pytest.mark.parametrize(
    "path", CORPUS_FILES, ids=lambda p: f"{p.parent.parent.name}-{p.parent.name}-{p.stem}"
)
def test_every_section_of_every_corpus_file_round_trips(path: Path) -> None:
    """Catches a splitter whose offsets do not address the text it returns —
    an off-by-one at the heading line, an offset measured in bytes while the
    slice is in characters, or a `text` built by re-joining lines rather than
    slicing the source."""
    source = path.read_text(encoding="utf-8")
    sections = split_sections(source)

    assert sections, path
    for index, section in enumerate(sections):
        assert source[section.char_start : section.char_end] == section.text, (
            f"{path}: section {index} {section.heading_path} does not round-trip"
        )


@pytest.mark.parametrize(
    "path", CORPUS_FILES, ids=lambda p: f"{p.parent.parent.name}-{p.parent.name}-{p.stem}"
)
def test_the_sections_of_every_corpus_file_partition_the_source(path: Path) -> None:
    """Catches a splitter that drops or duplicates bytes at a heading
    boundary: dropping a heading line, starting a
    section after its heading, or overlapping two sections all leave every
    per-section round-trip above green."""
    source = path.read_text(encoding="utf-8")
    sections = split_sections(source)

    assert sections[0].char_start == 0, path
    assert sections[-1].char_end == len(source), path
    for previous, current in pairwise(sections):
        assert current.char_start == previous.char_end, path


def test_nested_headings_give_the_enclosing_path_outermost_first() -> None:
    """Catches a splitter that records only the section's own heading, or
    that builds the path from the wrong end — a `### 1.1 Detail` under
    `## 1. Purpose` under `# Access Control Policy` must read as all three,
    outermost first, and the following `## 2. Scope` must pop the `###`
    level back off the stack."""
    sections = split_sections(NESTED)

    assert _paths(sections) == [
        ("Access Control Policy",),
        ("Access Control Policy", "1. Purpose"),
        ("Access Control Policy", "1. Purpose", "1.1 Detail"),
        ("Access Control Policy", "2. Scope"),
    ]


def test_a_section_spans_from_its_own_heading_to_the_next_heading() -> None:
    """Catches a splitter that starts a section after its heading line (the
    heading text then belongs to no section's `text`, so the chunk that gets
    embedded loses the words a reader searches for)."""
    sections = split_sections(NESTED)

    assert sections[1].text.startswith("## 1. Purpose")
    assert sections[1].text.endswith("Purpose body.\n\n")
    assert "1.1 Detail" not in sections[1].text


def test_a_document_with_no_headings_is_one_section_spanning_the_whole_source() -> None:
    """Catches a splitter that returns nothing for a headingless document —
    the whole document would then be dropped from the index — and one that
    invents a heading path for text that has no heading above it."""
    sections = split_sections(HEADINGLESS)

    assert len(sections) == 1
    assert sections[0].heading_path == ()
    assert sections[0].char_start == 0
    assert sections[0].char_end == len(HEADINGLESS)
    assert sections[0].text == HEADINGLESS


def test_body_text_before_the_first_heading_is_its_own_pathless_section() -> None:
    """Catches a splitter that discards everything before the first heading,
    or that folds the preamble into the first heading's section and so
    labels it with a heading that does not govern it."""
    sections = split_sections(PREAMBLE)

    assert _paths(sections) == [(), ("The First Heading",)]
    assert sections[0].char_start == 0
    assert sections[0].text == "Body text before any heading at all.\n\n"
    assert sections[1].text.startswith("# The First Heading")


def test_a_hash_inside_a_fenced_code_block_is_not_a_heading() -> None:
    """Catches a splitter that scans for `^#` line by line without tracking
    fences: the code block would split into two phantom sections and the
    heading path of everything after it would be wrong."""
    sections = split_sections(FENCED)

    assert _paths(sections) == [("Title",), ("Title", "Real Section")]
    assert "# not a heading" in sections[0].text
    assert "## also not a heading" in sections[0].text
