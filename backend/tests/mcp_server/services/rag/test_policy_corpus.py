"""Corpus-shape tests for the seed policy corpus.

Guards the seed policy corpus under the repo-root ``policies/`` directory:
    policies/{tenant}/{version}/{slug}.md
for tenants ``nordlicht``/``cuan`` and versions ``v1``/``v2``.

Checks:
- all 10 documents exist for every tenant x version (40 files),
- every file is 800-2000 words,
- v2 of documents 1/3/5 (access-control-policy, joiner-mover-leaver,
  access-review) is a material change vs v1 (difflib ratio below 0.9),
- no email-like string outside ``@nordlicht.example`` / ``@cuan.example``.

Pure filesystem: no DB fixtures, no imports from ``helpdesk``.
"""

import re
from difflib import SequenceMatcher
from pathlib import Path

import pytest

TENANTS: tuple[str, str] = ("nordlicht", "cuan")
VERSIONS: tuple[str, str] = ("v1", "v2")

# The 10 documents from the corpus spec, kebab-cased. Document 1's slug is the
# one the spec uses verbatim (``access-control-policy``); documents 3 and 5 use
# the short forms the requirements name (``joiner-mover-leaver``, ``access-review``).
SLUGS: tuple[str, ...] = (
    "access-control-policy",  # 1. Access Control Policy
    "segregation-of-duties",  # 2. Segregation of Duties Standard
    "joiner-mover-leaver",  # 3. Joiner, Mover, Leaver Procedure
    "privileged-access",  # 4. Privileged Access Standard
    "access-review",  # 5. Access Review Procedure
    "acceptable-use",  # 6. Acceptable Use Policy
    "password-account-recovery",  # 7. Password and Account Recovery Procedure
    "physical-access-badge",  # 8. Physical Access (badge) Policy
    "third-party-access",  # 9. Third-party and Contractor Access Policy
    "data-classification",  # 10. Data Classification Standard
)

# Documents 1, 3 and 5: v2 must carry a material change.
MATERIAL_CHANGE_SLUGS: tuple[str, str, str] = (
    "access-control-policy",
    "joiner-mover-leaver",
    "access-review",
)

WORD_MIN = 800
WORD_MAX = 2000
# Similarity above this means the v1 -> v2 edit was trivial, not material.
MATERIAL_CHANGE_MAX_RATIO = 0.9

ALLOWED_EMAIL_DOMAINS: frozenset[str] = frozenset({"nordlicht.example", "cuan.example"})
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")


def _repo_root() -> Path:
    """Walk up from this file to the directory that holds the git checkout."""
    for candidate in Path(__file__).resolve().parents:
        if (candidate / ".git").exists():
            return candidate
    raise RuntimeError("could not locate the repo root (.git) above this test file")


def _policies_dir() -> Path:
    return _repo_root() / "backend" / "data" / "policies"


def _doc_path(tenant: str, version: str, slug: str) -> Path:
    return _policies_dir() / tenant / version / f"{slug}.md"


def _read_doc(tenant: str, version: str, slug: str) -> str:
    path = _doc_path(tenant, version, slug)
    assert path.is_file(), f"missing corpus document: {path}"
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize("tenant", TENANTS)
@pytest.mark.parametrize("version", VERSIONS)
def test_all_documents_present(tenant: str, version: str) -> None:
    """Each tenant x version directory holds all 10 documents."""
    missing = [
        f"{tenant}/{version}/{slug}.md"
        for slug in SLUGS
        if not _doc_path(tenant, version, slug).is_file()
    ]
    assert not missing, (
        f"missing {len(missing)} corpus document(s) under {_policies_dir()}: {missing}"
    )


@pytest.mark.parametrize("slug", SLUGS)
@pytest.mark.parametrize("tenant", TENANTS)
@pytest.mark.parametrize("version", VERSIONS)
def test_word_range(tenant: str, version: str, slug: str) -> None:
    """Every corpus file is 800-2000 words (whitespace-separated tokens)."""
    text = _read_doc(tenant, version, slug)
    words = len(text.split())
    assert WORD_MIN <= words <= WORD_MAX, (
        f"{tenant}/{version}/{slug}.md has {words} words, expected {WORD_MIN}-{WORD_MAX}"
    )


@pytest.mark.parametrize("slug", MATERIAL_CHANGE_SLUGS)
@pytest.mark.parametrize("tenant", TENANTS)
def test_material_v2_change(tenant: str, slug: str) -> None:
    """Documents 1/3/5 differ materially between v1 and v2, not by a trivial edit."""
    v1 = _read_doc(tenant, "v1", slug)
    v2 = _read_doc(tenant, "v2", slug)
    # autojunk junks frequent characters on texts this long, collapsing the
    # ratio to near zero even for a one-line edit; the guard needs it off.
    ratio = SequenceMatcher(a=v1, b=v2, autojunk=False).ratio()
    assert ratio < MATERIAL_CHANGE_MAX_RATIO, (
        f"{tenant}/{slug}: v1 vs v2 similarity {ratio:.3f} >= "
        f"{MATERIAL_CHANGE_MAX_RATIO}; v2 must carry a material change"
    )


def test_no_stray_emails() -> None:
    """No email-like string outside the two fake .example tenant domains."""
    corpus_files = sorted(_policies_dir().glob("*/*/*.md"))
    assert corpus_files, f"no corpus files found under {_policies_dir()}"

    stray: list[str] = []
    for path in corpus_files:
        text = path.read_text(encoding="utf-8")
        for match in EMAIL_RE.finditer(text):
            domain = match.group(1).lower()
            if domain not in ALLOWED_EMAIL_DOMAINS:
                rel = path.relative_to(_policies_dir())
                stray.append(f"{rel}: {match.group(0)}")
    assert not stray, f"email-like strings outside allowed .example domains: {stray}"
