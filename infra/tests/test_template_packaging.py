"""The repository works as a template.

What is pinned here: the shape of `docs/customizing.md`, the shape of
`docs/directory-adapter.md`, the two README entry points, the `.env.example`
line for `POLICY_DIR`, the `make ingest` target, and the
absence of any invented seed name from Python application source.

Why the check is static rather than runtime: nothing in this list is read by
the application at all. `docs/customizing.md` and `docs/directory-adapter.md` are
read by a human adopting the template, and a document nobody checks drifts
from the code it describes within a few changes. A green suite proves the code
works; only parsing the document proves the document still describes it.

The adapter guide's method list is READ OFF the Protocols in
`helpdesk.access.directory` rather than retyped, so adding an eighth port
method fails this file instead of passing unnoticed.

Pure filesystem plus two imports: no Postgres, no Redis, no Qdrant, no
fixture. `scripts.seed` is importable because
`packages/helpdesk/tests/conftest.py` puts the repository root on `sys.path`,
the same mechanism `test_seed.py` uses.
"""

from pathlib import Path
from typing import Any

import pytest

from helpdesk.shared.access.directory import DirectoryProvider, DirectoryReader
from scripts.seed import TENANTS


def _repo_root() -> Path:
    """Walk up from this file to the directory that holds the git checkout."""
    for candidate in Path(__file__).resolve().parents:
        if (candidate / ".git").exists():
            return candidate
    raise RuntimeError("could not locate the repo root (.git) above this test file")


REPO_ROOT = _repo_root()
"""Every path below is resolved from here."""

CUSTOMIZING = REPO_ROOT / "docs" / "customizing.md"
ADAPTER_GUIDE = REPO_ROOT / "docs" / "directory-adapter.md"
README = REPO_ROOT / "README.md"
ENV_EXAMPLE = REPO_ROOT / "backend" / ".env.example"
MAKEFILE = REPO_ROOT / "Makefile"

EXPECTED_SECTIONS: tuple[str, ...] = (
    "Policies",
    "Directory adapter",
    "Role templates",
    "SoD rules",
    "Branding",
)
"""The five things an adopter changes first, in the order the guide fixes.

Asserted as an exact ordered list, not as a subset: a sixth section makes the
file a manual rather than a checklist, and a reordering buries the one change
(`POLICY_DIR`) that every adopter makes."""

ENV_EXAMPLE_LINE = "POLICY_DIR=policies"
"""The exact line `.env.example` must carry, value included.

The default is a value, so it is pinned as a value, in both
this file and `test_settings.py`'s `POLICY_DIR_DEFAULT`. An `.env.example`
whose placeholder disagrees with the field's default teaches the adopter the
wrong default."""

PORT_METHODS_AT_HEAD: frozenset[str] = frozenset(
    {
        "lookup_user",
        "list_membership_role_ids",
        "list_account_role_ids",
        "list_department_role_ids",
        "create_user",
        "apply_grant",
        "revoke_grant",
    }
)
"""The seven port methods at HEAD, as a tripwire only.

This literal is never the source the guide is checked against: that is
`_port_method_names()`, read off the Protocols. The literal exists so a
changed port names itself in the failure message instead of leaving the reader
to work out which of the two sets moved (the premise is
asserted, and it is the property the case depends on)."""


def _port_method_names() -> frozenset[str]:
    """Every public callable declared on the two directory Protocols.

    `vars()` per class, so only names each Protocol DECLARES are counted and
    nothing inherited from `typing.Protocol` leaks in. `DirectoryProvider`
    extends `DirectoryReader`, so both are read and the results unioned.
    """
    names: set[str] = set()
    for protocol in (DirectoryReader, DirectoryProvider):
        members: dict[str, Any] = dict(vars(protocol))
        for name, member in members.items():
            if not name.startswith("_") and callable(member):
                names.add(name)
    return frozenset(names)


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    return path.read_text(encoding="utf-8")


def _section_headings(markdown: str) -> list[str]:
    """The `##` headings of a document, in document order, level two only.

    Level two exactly: `## ` with the trailing space excludes `###`, so a
    sub-heading inside a section counts as that section's prose and the
    exact-list assertion below does not depend on how deeply the author nested
    their notes.
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


# --- docs/customizing.md -----------------------------------------------------------


def test_customizing_lists_exactly_the_five_things_an_adopter_changes_first() -> None:
    """Catches a `docs/customizing.md` that is missing, that is a wall of prose with
    no headings, or that grew a sixth section.

    The file's whole value is that it is scannable in a minute, which is also
    why the adapter walkthrough lives in its own file. An adopter
    who cannot see the five things in one screen reads the source instead, and
    the template stops being a template. Asserted as an ordered list rather
    than a set, so a file that names all five in a different order (burying
    `Policies`, the change every adopter makes) is red too.
    """
    assert _section_headings(_read(CUSTOMIZING)) == list(EXPECTED_SECTIONS)


def test_the_customizing_policies_section_names_the_variable_and_the_command() -> None:
    """Catches a `Policies` section that describes bringing your own corpus in
    prose without naming either thing the adopter has to type.

    The heading-list case above stays green on exactly that file: it reads
    headings and never opens a body. Two literals are required, because each
    answers a different half and one without the other leaves the adopter
    stuck: `POLICY_DIR` says where to point the repository, and `make ingest`
    says how to load what is now there. Read from this section's
    body rather than the whole file, so a `POLICY_DIR` mentioned only under
    `Branding` cannot satisfy it.
    """
    body = _section_body(_read(CUSTOMIZING), "Policies")

    assert "POLICY_DIR" in body
    assert "make ingest" in body


# --- docs/directory-adapter.md ------------------------------------------------


def test_the_adapter_guide_names_every_directory_provider_method() -> None:
    """Catches a guide that documents some of the port and leaves the rest for
    the reader to find, and a guide that goes stale when the port grows.

    An adapter written from a guide missing `revoke_grant` compiles as a
    Protocol implementation right up to the first offboarding, where the
    missing method is a runtime failure in the one path nobody demos. The
    expected set is read off `DirectoryProvider` and `DirectoryReader` at test
    time, so an eighth method added later is red here until the guide
    names it - the failure this case exists for, since nothing else in the
    repository connects the two files.
    """
    port_methods = _port_method_names()
    # Premise: the port is the seven-method shape the guide lists. If this is
    # red, the port moved and the test needs rereading, not the guide.
    assert port_methods == PORT_METHODS_AT_HEAD

    guide = _read(ADAPTER_GUIDE)
    missing = sorted(name for name in port_methods if name not in guide)
    assert missing == [], f"the adapter guide does not name: {missing}"


def test_the_adapter_guide_names_the_simulated_implementation() -> None:
    """Catches a guide that describes the interface in the abstract and never
    points at the working example.

    The method-name case above passes on such a guide: seven names in a bullet
    list satisfy it. What an adapter author actually needs is the one file that
    already implements all seven, so both the class and its path are required.
    Naming the path as well as the class matters because `SimulatedDirectory`
    is referenced from several modules; only the path says where to read it.
    """
    guide = _read(ADAPTER_GUIDE)

    assert "SimulatedDirectory" in guide
    assert "backend/helpdesk/shared/access/directory.py" in guide


# --- README.md ----------------------------------------------------------------


def test_the_readme_links_the_customizing_and_adapter_guides() -> None:
    """Catches two documents that exist but that nothing reaches.

    Every case above opens its file by path, so both stay green when the README
    mentions neither. A reader arrives at the README and nowhere else; an
    unlinked customizing guide is a file only the author knows about. The link
    target is asserted with the closing bracket and parenthesis
    (`](docs/customizing.md)`) so a bare mention of the filename in prose does
    not pass for a link.
    """
    readme = _read(README)

    assert "](docs/customizing.md)" in readme
    assert "](docs/directory-adapter.md)" in readme


def test_the_readme_shows_the_bring_your_own_policies_command() -> None:
    """Catches a README that links `docs/customizing.md` and says nothing itself.

    The link case above is green on exactly that README. Section 1 asks for the
    command in the README too, because the "can I use this with my policies?"
    question is answered in the ten seconds a reader spends on the front page,
    not in a file they have not opened. All three literals are required: the
    block's own heading text, the variable to set, and the command to run.
    """
    readme = _read(README)

    assert "Use it with your own policies" in readme
    assert "POLICY_DIR" in readme
    assert "make ingest" in readme


# --- .env.example and the Makefile --------------------------------------------


def test_the_env_example_declares_policy_dir_with_the_default_value() -> None:
    """Catches an `.env.example` that never mentions `POLICY_DIR`, and one that
    mentions it with no explanation of what it points at.

    `.env.example` is the only inventory of what this system can be configured
    with, so a setting absent from it is a setting nobody discovers. The line
    is matched exactly, value included, so a placeholder like
    `POLICY_DIR=/path/to/your/policies` is red: copying `.env.example` to
    `.env` must leave a working checkout working. The comment line above it is
    required because the variable name alone does not say it is a directory of
    `{tenant}/{version}` corpora.
    """
    lines = _read(ENV_EXAMPLE).splitlines()

    assert ENV_EXAMPLE_LINE in lines, f"no exact line {ENV_EXAMPLE_LINE!r} in .env.example"
    index = lines.index(ENV_EXAMPLE_LINE)
    assert index > 0, "POLICY_DIR is the first line, so it carries no comment above it"
    assert lines[index - 1].startswith("#"), (
        f"the line above {ENV_EXAMPLE_LINE!r} is not a comment: {lines[index - 1]!r}"
    )


def test_the_makefile_declares_a_phony_ingest_target() -> None:
    """Catches the ingest target being documented but not implemented.

    Both README cases and the `docs/customizing.md` body case assert the string
    `make ingest` in prose; none of them proves the target exists, so a
    documented command that errors with "No rule to make target" passes every
    one of them. The `.PHONY` half matters on its own: without it a file or
    directory named `ingest` in the repository root makes the target a silent
    no-op, which reports success while ingesting nothing.
    """
    lines = _read(MAKEFILE).splitlines()

    assert any(line.startswith("ingest:") for line in lines), "no `ingest:` target in the Makefile"

    phony_tokens: set[str] = set()
    for line in lines:
        if line.startswith(".PHONY:"):
            phony_tokens.update(line.removeprefix(".PHONY:").split())
    assert "ingest" in phony_tokens, f"`ingest` is not in .PHONY: {sorted(phony_tokens)}"


# --- no invented name in application source ------------------------------

SOURCE_ROOTS: tuple[Path, ...] = (
    REPO_ROOT / "backend" / "helpdesk" / "shared",
    REPO_ROOT / "backend" / "helpdesk" / "agent_api",
    REPO_ROOT / "backend" / "helpdesk" / "mcp_server",
    REPO_ROOT / "backend" / "helpdesk" / "scheduler",
)
"""The Python application packages this check is scoped to.

Not `scripts/` (it holds `seed.py`, which owns the values), not any `tests/`
directory (a test naming a seeded tenant is naming its own fixture), and not
`web/`, whose one hit is prose in a comment about a regex."""


def _application_source_files() -> list[Path]:
    files: list[Path] = []
    for root in SOURCE_ROOTS:
        assert root.is_dir(), f"missing application source root: {root}"
        files.extend(
            path
            for path in sorted(root.rglob("*.py"))
            if "tests" not in path.relative_to(REPO_ROOT).parts
        )
    return files


@pytest.mark.parametrize(
    ("slug", "name"),
    [(spec.slug, spec.name) for spec in TENANTS],
    ids=[spec.key for spec in TENANTS],
)
def test_no_invented_name_appears_in_python_application_source(slug: str, name: str) -> None:
    """Catches a later change hardcoding a seeded tenant into shipped code.

    This is the template criterion with the longest reach: an adopter who
    deletes the seed data must get a system that still runs, and a slug or a
    company name compiled into a policy default, a prompt, a metric label or a
    conditional makes the demo data load-bearing. The values are read from
    `scripts.seed.TENANTS` rather than retyped, so renaming a
    seeded tenant moves this check with it instead of leaving it guarding a
    string nothing uses.

    Passes at HEAD by construction, which is the point: nothing else in the
    suite would notice the first hardcoded occurrence, because a hardcoded slug
    matches the seeded database and every other test stays green.
    """
    needles = (slug.lower(), name.lower())
    offenders: list[str] = []
    for path in _application_source_files():
        text = path.read_text(encoding="utf-8").lower()
        if any(needle in text for needle in needles):
            offenders.append(str(path.relative_to(REPO_ROOT)))

    assert offenders == [], f"invented seed name {slug!r}/{name!r} appears in: {offenders}"
