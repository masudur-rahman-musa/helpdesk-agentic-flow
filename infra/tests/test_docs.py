"""The shipped documents say what the code does.

What is pinned here: the twelve fixed README sections and their order, the
seven guarantees and the tests each one links as its proof, the failure-modes
and numbers tables, the demo gap, the four Mermaid diagram sources and the
README links that reach them, and that every path a reference file names
exists.

Why the check is static rather than runtime: none of these files is read by
the application, so nothing except a test that opens them stops them drifting
from the code they describe. The same mechanism is used by
`test_template_packaging.py`, whose repo-root walk and `_read`,
`_section_headings` and `_section_body` helpers this module reuses rather than
inventing a second one.

The guarantee links are the load-bearing part. A guarantee is a claim, and a
claim without a named proving test is marketing, so the link target is opened
and the link text's `test_...` identifier is looked up in it: a renamed or
deleted test breaks the README here instead of hollowing it quietly.

Pure filesystem: no Postgres, no Redis, no Qdrant, no fixture, no network, no
import of application code.
"""

import re
import subprocess
from pathlib import Path

import pytest


def _repo_root() -> Path:
    """Walk up from this file to the directory that holds the git checkout."""
    for candidate in Path(__file__).resolve().parents:
        if (candidate / ".git").exists():
            return candidate
    raise RuntimeError("could not locate the repo root (.git) above this test file")


REPO_ROOT = _repo_root()
"""Every path below is resolved from here."""

README = REPO_ROOT / "README.md"
CUSTOMIZING = REPO_ROOT / "docs" / "use-it-for-your-company.md"
DOCS = REPO_ROOT / "docs"
DIAGRAM_DIR = DOCS / "diagrams"

README_SECTIONS: tuple[str, ...] = (
    "Problem statement",
    "Demo",
    "Architecture diagram",
    "Guarantees",
    "What broke and how I fixed it",
    "Design decisions",
    "Failure modes",
    "Numbers",
    "At 10x and 100x",
    "Works council note",
    "Quickstart",
    "Integration path to a real directory",
)
"""The twelve level-two headings, in their fixed order.

Exact heading texts, because every section-body case below locates its section
by this string. Asserted as increasing offsets rather than as presence, so a
reworded heading is red instead of leaving eleven sections passing."""

GUARANTEE_KEYWORDS: tuple[tuple[str, ...], ...] = (
    ("mutation", "mutate"),
    ("tenant",),
    ("expir",),
    ("sod", "duties"),
    ("citation", "cite", "refus"),
    ("audit",),
    ("idempot",),
)
"""One lowercase keyword set per guarantee, in the product summary's order.

Alternatives inside a row only where the product prose has two names for one
thing (`SoD` or `segregation of duties`); the ROW ORDER is the assertion."""

FAILURE_MODE_KEYWORDS: tuple[str, ...] = (
    "gateway",
    "qdrant",
    "redis",
    "garbage",
    "timeout",
    "approvers",
)
"""The six failure-mode rows, in order, one keyword each."""

NUMBERS_LABELS: tuple[str, ...] = (
    "decision accuracy",
    "refusal precision and recall",
    "citation accuracy",
    "cost per turn",
    "p95 latency",
)
"""The five measures the numbers section must account for: each one is either a
table row with a number in it, or named on the section's "Not yet measured" line."""

NOT_MEASURED = "Not yet measured:"
"""How the numbers section's line of unmeasured measures starts."""

NOT_RECORDED = "not yet recorded"
"""The exact text the demo section reads while no video exists."""

VIDEO_MARKERS: tuple[str, ...] = (".mp4", ".webm", "youtu", "vimeo", "loom")
"""What a recorded demo would look like in the demo section's links.

Present so the demo case pins the placeholder rather than the absence of a video:
once a video lands, the literal is allowed to go."""

DIAGRAM_STEMS: tuple[str, ...] = (
    "approval-lifecycle",
    "architecture",
    "grant-lifecycle",
    "request-lifecycle",
)
"""The four Mermaid sources, sorted, as an EXACT set (no exports).

Sorted because the directory listing is compared to it directly."""

MERMAID_FENCE = "```mermaid"


LINK_PATTERN = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
"""A markdown inline link: text in brackets, target in parentheses."""

TEST_NAME_PATTERN = re.compile(r"\btest_[a-zA-Z0-9_]+")
"""A `test_...` identifier as it appears inside a guarantee's link text."""

GUARANTEE_LINE_PATTERN = re.compile(r"^\s*(\d+)\.\s+(.*)$")
"""A numbered guarantee line: `1. ...` through `7. ...`."""


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    return path.read_text(encoding="utf-8")


def _section_headings(markdown: str) -> list[str]:
    """The `##` headings of a document, in document order, level two only.

    Level two exactly: `## ` with the trailing space excludes `###`, so a
    sub-heading inside a section counts as that section's prose.
    """
    return [
        line.removeprefix("## ").strip() for line in markdown.splitlines() if line.startswith("## ")
    ]


def _section_body(markdown: str, heading: str) -> str:
    """Everything under `## {heading}` up to the next `##` heading."""
    lines = markdown.splitlines()
    start: int | None = None
    for index, line in enumerate(lines):
        if line.startswith("## ") and line.removeprefix("## ").strip() == heading:
            start = index + 1
            break
    assert start is not None, f"no '## {heading}' section in the document"
    body: list[str] = []
    for line in lines[start:]:
        if line.startswith("## "):
            break
        body.append(line)
    return "\n".join(body)


def _heading_offset(markdown: str, heading: str) -> int:
    """The character offset of the line `## {heading}`, matched whole."""
    needle = f"## {heading}"
    offset = 0
    for line in markdown.splitlines(keepends=True):
        if line.rstrip() == needle:
            return offset
        offset += len(line)
    raise AssertionError(f"no '## {heading}' heading in the document")


def _section_matching(markdown: str, keyword: str) -> tuple[str, str]:
    """The first `##` section whose heading contains `keyword`, and its body.

    Used where a section is named by subject rather than by exact text.
    """
    for heading in _section_headings(markdown):
        if keyword.lower() in heading.lower():
            return heading, _section_body(markdown, heading)
    raise AssertionError(f"no '##' section whose heading mentions {keyword!r}")


def _table_rows(body: str) -> list[str]:
    """The DATA rows of the first markdown table in `body`.

    The header row and the `|---|` separator are dropped, so a count is a
    count of scenarios rather than of pipe characters.
    """
    pipe_lines = [line.strip() for line in body.splitlines() if line.strip().startswith("|")]
    rows = [line for line in pipe_lines if not set(line) <= set("|-: ")]
    return rows[1:] if rows else []


def _cells(row: str) -> list[str]:
    """The trimmed cells of a markdown table row, outer pipes discarded."""
    return [cell.strip() for cell in row.strip().strip("|").split("|")]


def _guarantee_lines() -> list[tuple[int, str]]:
    """Every numbered line under the README's guarantees heading."""
    body = _section_body(_read(README), "Guarantees")
    found: list[tuple[int, str]] = []
    for line in body.splitlines():
        match = GUARANTEE_LINE_PATTERN.match(line)
        if match is not None:
            found.append((int(match.group(1)), match.group(2)))
    return found


# --- README structure ---------------------------------------------------------


def test_the_readme_carries_the_twelve_sections_in_order() -> None:
    """Catches a README missing one of the twelve fixed sections, and one that
    carries all twelve with two of them swapped.

    Every other README case locates its section by heading and would pass on a
    document whose sections run in any order at all, because none of them looks
    at where a section sits. The order is the product of the page: a reader who
    meets the quickstart before the guarantees has already decided what this
    repository is. Offsets are asserted to increase strictly, so a reworded
    heading fails here rather than passing silently with eleven sections.
    """
    readme = _read(README)

    offsets = [_heading_offset(readme, heading) for heading in README_SECTIONS]
    ascending = sorted(offsets)
    assert offsets == ascending, (
        "README sections are out of order: "
        f"{[name for _, name in sorted(zip(offsets, README_SECTIONS, strict=True))]}"
    )


def test_the_readme_lists_exactly_seven_guarantees() -> None:
    """Catches a guarantees section that grew an eighth claim or lost one, and
    one whose claims drifted out of the product summary's order.

    The ordered-headings case above is green on a guarantees section holding
    nothing at all, because it never opens a body, and both link cases below
    are green on a section holding four guarantees: they check the links of
    whatever they find. The count is asserted as equality, never as a floor, so
    an eighth unproved guarantee is a failure rather than a bonus.
    """
    guarantees = _guarantee_lines()

    assert len(guarantees) == 7, f"expected 7 numbered guarantees, found {len(guarantees)}"
    assert [number for number, _ in guarantees] == [1, 2, 3, 4, 5, 6, 7]

    for index, (_, text) in enumerate(guarantees):
        alternatives = GUARANTEE_KEYWORDS[index]
        lowered = text.lower()
        assert any(word in lowered for word in alternatives), (
            f"guarantee {index + 1} names none of {alternatives}: {text!r}"
        )


def test_every_guarantee_links_a_test_file_that_exists() -> None:
    """Catches a guarantee whose proof link points at a test file that was
    renamed, moved or never written.

    The count case above passes on seven guarantees carrying no link at all,
    and on seven links to files that do not exist, because it reads only the
    numbering and one keyword. This is an unhappy path: a
    guarantee is a claim, and a claim whose proof is a dead relative path is
    worse than an unlinked one, because the link says someone checked. Targets
    are resolved from the repository root, the way a reader on the web page
    resolves them.
    """
    guarantees = _guarantee_lines()
    assert guarantees, "no numbered guarantees to check; see the count case"

    missing: list[str] = []
    for number, text in guarantees:
        links = LINK_PATTERN.findall(text)
        assert links, f"guarantee {number} carries no markdown link: {text!r}"
        for _, target in links:
            if not (REPO_ROOT / target).is_file():
                missing.append(f"guarantee {number} -> {target}")
    assert missing == [], f"guarantee links with no file behind them: {missing}"


def test_every_guarantee_names_a_test_that_its_file_defines() -> None:
    """Catches a guarantee linking a real test file whose link text names a
    test that file does not define.

    The target-exists case above is green on exactly that README, since the
    file it opens is there; only reading the link text catches a test renamed
    while the file kept its name. That is the failure this
    case makes loud: a line anchor rots invisibly, a named function
    does not. The identifier is required as `def <name>(`, so a test merely
    MENTIONED in a comment in that file does not pass for one it defines.
    """
    guarantees = _guarantee_lines()
    assert guarantees, "no numbered guarantees to check; see the count case"

    unproved: list[str] = []
    for number, text in guarantees:
        for link_text, target in LINK_PATTERN.findall(text):
            names = TEST_NAME_PATTERN.findall(link_text)
            assert names, f"guarantee {number} link text names no test: {link_text!r}"
            path = REPO_ROOT / target
            if not path.is_file():
                continue  # the target-exists case owns this failure
            source = path.read_text(encoding="utf-8")
            for name in names:
                if f"def {name}(" not in source:
                    unproved.append(f"guarantee {number}: {target} defines no {name}")
    assert unproved == [], f"guarantees naming a test their file does not define: {unproved}"


def test_the_failure_modes_table_has_exactly_six_rows() -> None:
    """Catches a failure-modes section written as reassuring prose, and a table
    that quietly dropped the dependency a reader most wants to ask about.

    The ordered-headings case passes on an empty failure-modes section. Six is
    asserted as equality and each row is matched to ITS scenario by position,
    so a table that answers the gateway twice and never mentions Redis is red
    even though it holds six rows. Keywords rather than sentences, because the
    prose around them is the author's and the scenario is the specification's.
    """
    _, body = _section_matching(_read(README), "Failure modes")
    rows = _table_rows(body)

    assert len(rows) == 6, f"expected 6 failure-mode rows, found {len(rows)}: {rows}"
    for index, keyword in enumerate(FAILURE_MODE_KEYWORDS):
        assert keyword in rows[index].lower(), (
            f"failure-mode row {index + 1} does not name {keyword!r}: {rows[index]!r}"
        )


def test_the_numbers_section_accounts_for_every_measure() -> None:
    """Catches a numbers section that reports the measures that look good and
    quietly drops the ones that are not measured yet.

    Every one of the five measures must appear, either as a table row whose
    result holds a number, or on the section's "Not yet measured" line, so a
    reader can tell a measured result from a gap. A table cell with no number
    (`TBD`, `n/a`, `coming soon`, empty) is red, and so is a measure that is in
    neither place.
    """
    _, body = _section_matching(_read(README), "Numbers")
    rows = _table_rows(body)
    labels = [_cells(row)[0].lower() for row in rows]
    unmeasured = [line for line in body.splitlines() if line.startswith(NOT_MEASURED)]

    assert rows, "the numbers section has no table of measured results"
    for row in rows:
        for cell in _cells(row)[1:]:
            assert any(character.isdigit() for character in cell), (
                f"numbers cell {cell!r} holds no number; an unmeasured measure belongs "
                f"on the {NOT_MEASURED!r} line, not in the table"
            )
    assert len(unmeasured) <= 1, f"more than one {NOT_MEASURED!r} line: {unmeasured}"
    named = unmeasured[0].lower() if unmeasured else ""
    for label in NUMBERS_LABELS:
        in_table = any(label in row_label for row_label in labels)
        assert in_table or label in named, (
            f"{label!r} is neither a table row with a number nor on the {NOT_MEASURED!r} line"
        )


def test_the_what_broke_section_has_at_least_five_entries() -> None:
    """Catches a README section that names one embarrassment and calls the
    subject covered, and one that lists breakages without saying what fixed
    them.

    No other case opens this section, and the ordered-headings case is green on
    an empty one. Each entry must carry the word `fix`, because an incident
    list with no resolution reads as a risk register rather than as engineering
    history. Entries are counted as `###` headings or top-level list items, so
    either shape is accepted.
    """
    body = _section_body(_read(README), "What broke and how I fixed it")

    entries: list[str] = []
    current: list[str] = []
    for line in body.splitlines():
        starts_entry = line.startswith("### ") or line.startswith("- ")
        if starts_entry:
            if current:
                entries.append("\n".join(current))
            current = [line]
        elif current:
            current.append(line)
    if current:
        entries.append("\n".join(current))

    assert len(entries) >= 5, f"expected at least 5 what broke entries, found {len(entries)}"
    without_fix = [entry.splitlines()[0] for entry in entries if "fix" not in entry.lower()]
    assert without_fix == [], f"what broke entries with no fix sentence: {without_fix}"


def test_the_demo_section_declares_the_video_missing() -> None:
    """Catches a demo section left blank while the video is unrecorded, which
    reads as an oversight rather than as a known gap.

    The ordered-headings case passes on a heading with nothing under it, and
    that is precisely the state this case rejects. The literal is required
    only while no video link is present, so the case does not have to be edited
    on the day the recording lands: it simply starts passing on the link
    instead.
    """
    body = _section_body(_read(README), "Demo")

    assert body.strip(), "the demo section is empty"
    has_video = any(marker in body.lower() for marker in VIDEO_MARKERS)
    assert has_video or NOT_RECORDED in body, (
        f"the demo section links no video and does not say {NOT_RECORDED!r}"
    )


def test_the_readme_embeds_the_architecture_diagram() -> None:
    """Catches an architecture section that links the diagram source instead of
    rendering it on the front page.

    The link case below is green on exactly that README, because it asks only
    that the four paths appear somewhere in the file. Decision C keeps Mermaid
    sources with no exported images, which works only if GitHub renders the
    fence in the README itself; a reader who has to click through to see the
    architecture does not see the architecture. Read from this section's body,
    so a fence under the quickstart cannot satisfy it.
    """
    body = _section_body(_read(README), "Architecture diagram")

    assert MERMAID_FENCE in body, "no fenced mermaid block under the architecture heading"


def test_the_readme_links_every_diagram_source() -> None:
    """Catches a diagram source that exists and that nothing reaches.

    This is an unhappy path, and the exact-set case below
    stays green on it: that case reads the directory and never opens the
    README. The expected list is READ OFF `docs/diagrams/` rather than retyped,
    so a fifth diagram added later is red here until the README links
    it, which is the drift nothing else in the repository would notice. Matched
    with the closing bracket and parenthesis, so a filename in prose does not
    pass for a link.
    """
    assert DIAGRAM_DIR.is_dir(), f"missing diagram directory: {DIAGRAM_DIR}"
    stems = sorted(path.stem for path in DIAGRAM_DIR.glob("*.md"))
    # Premise: the four fixed sources. If this is red, the diagram
    # set moved, not the README.
    assert stems == list(DIAGRAM_STEMS)

    readme = _read(README)
    unlinked = [stem for stem in stems if f"](docs/diagrams/{stem}.md)" not in readme]
    assert unlinked == [], f"diagram sources the README does not link: {unlinked}"


# --- diagrams -----------------------------------------------------------------


def test_the_four_diagram_sources_exist() -> None:
    """Catches a missing diagram source, and a fifth source added without a
    README link.

    The per-source Mermaid case below opens exactly the four names it is
    parametrised with, so a fifth file is invisible to it; the README link case
    reads the directory too, but stays green if the directory is missing only
    because this case is the one that opens it first. The set is asserted as
    equality, which keeps sources only, so a committed
    `architecture.png` beside its source (the drift where the image and the
    source disagree and only the image is read) is red here.
    """
    assert DIAGRAM_DIR.is_dir(), f"missing diagram directory: {DIAGRAM_DIR}"
    names = sorted(path.name for path in DIAGRAM_DIR.iterdir() if path.is_file())

    assert names == [f"{stem}.md" for stem in DIAGRAM_STEMS]


@pytest.mark.parametrize("stem", DIAGRAM_STEMS)
def test_every_diagram_source_holds_a_mermaid_block(stem: str) -> None:
    """Catches a diagram source that is a prose description of a diagram.

    The exact-set case above is green on four empty files, because it reads
    only the directory listing. Decision C ships no rendered exports, so the
    fence IS the diagram: a source with no ```mermaid block renders as nothing
    on the page that embeds it, and the reader sees a gap rather than an
    architecture.
    """
    assert MERMAID_FENCE in _read(DIAGRAM_DIR / f"{stem}.md"), (
        f"{stem}.md holds no fenced mermaid block"
    )


# --- every named path exists ------------------------------------------------

MAKEFILE = REPO_ROOT / "Makefile"
COMPOSE = REPO_ROOT / "docker-compose.yml"
COMPOSE_OVERRIDE = REPO_ROOT / "docker-compose.override.example.yml"
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
K8S_DIR = REPO_ROOT / "infra" / "k8s"

PATH_IN_TEXT = re.compile(r"(?:\./)?((?:backend|web|docs|infra|k8s)/[A-Za-z0-9_.\-/]+)")
"""A repo-relative path starting with a real top-level folder. The character
class stops at `:`, so a compose mount `./backend/data/policies:/srv/...` yields
just the host side. A bare `dev` folder name is left out: it would match device paths like
`/dev/null` in the workflows. `docker/` is left out
too: it collides with GitHub Action refs like `docker/build-push-action`, and
there is no `docker/` tree now."""

FORBIDDEN_OLD_PATHS: tuple[str, ...] = (
    "packages/helpdesk",
    "services/agent-api",
    "services/mcp-server",
    "services/scheduler",
    "project-evals/",
)
"""Old-layout path fragments. None may survive in a committed reference file."""

# The local env files: gitignored, loaded by compose with `required: false`,
# and so absent from a fresh CI checkout. The path check skips them; that the
# skip hides no committed file is pinned by
# `test_every_skipped_env_path_is_gitignored` and
# `test_the_skip_list_holds_only_env_files`.
LOCAL_ENV_PATHS: tuple[str, ...] = ("backend/.env", "backend/.env.demo")

# Runtime artifacts and the real (gitignored) env file are named in workflows
# and the Makefile but never exist in a checkout; they are not static paths.
PATH_NOISE: tuple[str, ...] = (
    ".coverage",
    "shard",
    "backend/.env\b",
    "node_modules",
    ".next",
)


def _reference_files() -> list[Path]:
    files = [README, MAKEFILE, COMPOSE, COMPOSE_OVERRIDE]
    files += sorted(WORKFLOW_DIR.glob("*.yml")) + sorted(WORKFLOW_DIR.glob("*.yaml"))
    files += sorted(K8S_DIR.glob("*.yaml")) + sorted(K8S_DIR.glob("*.yml"))
    return [path for path in files if path.is_file()]


def test_every_named_path_exists() -> None:
    """Catches a reference file that still points at an old location, or at a path
    that was never created.

    Every path a committed reference file names under a real top-level folder is
    resolved from the repository root and must exist; and no file may still name
    an old `packages/` or `services/` path. A Dockerfile, mount, build context
    or command that names a path that no longer exists fails here.
    """
    missing: list[str] = []
    stale: list[str] = []
    for path in _reference_files():
        text = path.read_text(encoding="utf-8")
        where = path.relative_to(REPO_ROOT)

        for fragment in FORBIDDEN_OLD_PATHS:
            if fragment in text:
                stale.append(f"{where}: {fragment}")

        for raw in PATH_IN_TEXT.findall(text):
            candidate = raw.rstrip("./,:;)\"'`").removeprefix("./")
            if not candidate or any(noise in candidate for noise in (".coverage", "shard")):
                continue
            if candidate in LOCAL_ENV_PATHS or candidate.endswith("/node_modules"):
                continue
            # A local env file (LOCAL_ENV_PATHS) is skipped above; everything
            # else must be on disk.
            if not (REPO_ROOT / candidate).exists():
                missing.append(f"{where}: {candidate}")

    assert stale == [], f"old-layout paths still named: {stale}"
    assert missing == [], f"named paths that do not exist: {missing}"


def test_every_skipped_env_path_is_gitignored() -> None:
    """Catches a skip list that holds a committed file, so the path check would
    quietly stop reporting it missing.

    Each name on `LOCAL_ENV_PATHS` is asked of `git check-ignore -q`, run from
    the repository root; exit 0 means git ignores it, so no checkout owes it.
    A tracked or un-ignored name exits 1 and fails here.
    """
    assert len(LOCAL_ENV_PATHS) > 0, "the skip list is empty; nothing is checked"
    not_ignored: list[str] = []
    for name in LOCAL_ENV_PATHS:
        result = subprocess.run(
            ["git", "check-ignore", "-q", name],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if result.returncode != 0:
            not_ignored.append(f"{name} (exit {result.returncode}) {result.stderr.strip()}")
    assert not_ignored == [], f"skipped env paths git does not ignore: {not_ignored}"


def test_the_skip_list_holds_only_env_files() -> None:
    """Catches a skip list widened to a source file (say `backend/Dockerfile`),
    which the gitignore check alone would miss if that file were also ignored.

    Every name's last part must be exactly `.env` or `.env.demo`. The tripwire
    pins both names on the list, so dropping `backend/.env.demo`
    (the CI break) or `backend/.env` fails here by name.
    """
    assert "backend/.env" in LOCAL_ENV_PATHS
    assert "backend/.env.demo" in LOCAL_ENV_PATHS
    not_env = [name for name in LOCAL_ENV_PATHS if Path(name).name not in {".env", ".env.demo"}]
    assert not_env == [], f"skip list names that are not env files: {not_env}"
