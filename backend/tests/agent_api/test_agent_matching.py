"""The deterministic entity matcher, pure and graph-free.

Expected implementation surface (the implementer must provide exactly this):

``agent_api.agent.matching`` (PURE: no I/O, no langgraph, no SQLAlchemy)
    ``FUZZY_CUTOFF = 0.85`` (the fuzzy tier)
    ``@dataclass(frozen=True) class Candidate`` — ``id``, ``slug``, ``name``.
    ``def normalise(value: str) -> str`` — casefolded, every run of
        non-alphanumeric characters collapsed to one space, stripped.
    ``def aliases_for(candidate: Candidate) -> frozenset[str]`` —
        derived from the normalised slug and name, plus each with its spaces
        removed. No alias column, no hand-written list.
    ``def match_candidates(term, candidates) -> tuple[Candidate, ...]`` —
        four tiers, and a NON-EMPTY HIGHER TIER WINS OUTRIGHT: exact (term
        equals the normalised slug or name) beats alias beats whole-name words
        (every word of a candidate's normalised slug, or of its normalised name,
        is among the typed words; the most matched words wins, a tie returns all
        tied in the order given) beats fuzzy
        (``difflib.SequenceMatcher(None, term, alias).ratio() >=
        FUZZY_CUTOFF``, best ratio first). The cutoff is INCLUSIVE.

The old filler-word list was replaced by the whole-name word step.

Every ratio this file depends on is asserted with `difflib` BEFORE the
behaviour that depends on it, so a boundary pair that stops being the
boundary pair fails as a premise instead of quietly testing something else.
Nothing here needs a database, a socket or a graph.
"""

import ast
import difflib
import uuid
from pathlib import Path

import pytest

from helpdesk.agent_api.agent import matching as matching_module
from helpdesk.agent_api.agent.matching import (
    FUZZY_CUTOFF,
    Candidate,
    aliases_for,
    match_candidates,
    normalise,
)


def _candidate(slug: str, name: str) -> Candidate:
    return Candidate(id=uuid.uuid4(), slug=slug, name=name)


def _ratio(term: str, alias: str) -> float:
    return difflib.SequenceMatcher(None, term, alias).ratio()


# The boundary pair, found by search and asserted as a premise below:
# "marketing management" against the aliases of a system slugged
# "reporting-management" scores EXACTLY 0.85 on its best alias.
BOUNDARY_TERM = "marketing management"
BOUNDARY_SLUG = "reporting-management"
BOUNDARY_NAME = "Reporting Management"

# Just under: "billing manager" against "billing-management" / "Billing
# Management" scores 0.848485 on its best alias.
UNDER_TERM = "billing manager"
UNDER_SLUG = "billing-management"
UNDER_NAME = "Billing Management"


def test_the_fuzzy_cutoff_is_exactly_the_spec_value() -> None:
    """Catches a cutoff loosened to make a demo query match.
    At 0.80 the near-miss pair below collapses `billing-read` into
    `billing-admin`, which is a privilege escalation dressed up as a typo."""
    assert FUZZY_CUTOFF == 0.85


def test_normalise_collapses_case_separators_and_punctuation() -> None:
    """Catches a normaliser that only lowercases: `Billing_Admin` and
    `billing-admin` would then be two different systems to the matcher, so a
    user typing the name they see in the UI resolves nothing."""
    assert normalise("Billing-Admin") == "billing admin"
    assert normalise("Billing_Admin") == "billing admin"
    assert normalise("  Billing   Admin!  ") == "billing admin"
    assert normalise("billing/admin (EU)") == "billing admin eu"
    assert normalise("BILLING") == "billing"


def test_aliases_for_derives_both_spellings_of_the_slug_and_the_name() -> None:
    """Catches aliases built from the slug alone: a user who types the
    system's display name, or types it without spaces, would fall through to
    the fuzzy tier where a near-miss can win instead."""
    candidate = _candidate("billing-admin", "Billing Admin")
    assert aliases_for(candidate) == frozenset({"billing admin", "billingadmin"}), (
        "slug and name normalise to the same pair here, plus the space-free spelling"
    )

    two_named = _candidate("erp-finance", "Fake Finance Suite")
    assert aliases_for(two_named) == frozenset(
        {"erp finance", "erpfinance", "fake finance suite", "fakefinancesuite"}
    )


def test_an_exact_match_wins_outright_over_an_alias_and_a_fuzzy_candidate() -> None:
    """Catches a matcher that unions the tiers instead of returning the best
    non-empty one: three candidates would come back, the turn would branch to
    `ask_disambiguation`, and a user who typed a system's exact slug would be
    asked which one they meant."""
    exact = _candidate("billingadmin", "Billing Admin Console")
    alias_only = _candidate("billing-admin", "Billing Admin")
    fuzzy_only = _candidate("billing-admins", "Billing Admins")

    assert normalise("billingadmin") == "billingadmin"
    assert "billingadmin" in aliases_for(alias_only)
    assert _ratio("billingadmin", "billingadmins") >= FUZZY_CUTOFF

    assert match_candidates("billingadmin", [alias_only, fuzzy_only, exact]) == (exact,)


def test_an_alias_match_wins_outright_over_a_fuzzy_candidate() -> None:
    """Catches the second tier boundary: with no exact match present, a
    candidate whose derived alias IS the term must be the only answer. A union
    would return two, and the turn would ask a needless question."""
    alias_only = _candidate("billing-admin", "Billing Admin")
    fuzzy_only = _candidate("billing-admins", "Billing Admins")

    assert normalise("billingadmin") not in {normalise(alias_only.slug), normalise(alias_only.name)}
    assert "billingadmin" in aliases_for(alias_only)
    assert _ratio("billingadmin", "billingadmins") >= FUZZY_CUTOFF

    assert match_candidates("billingadmin", [fuzzy_only, alias_only]) == (alias_only,)


def test_a_pair_scoring_exactly_the_cutoff_matches() -> None:
    """The inclusive half of the cutoff: catches a strict `>`
    comparison. The premise is asserted first — the best alias ratio is
    exactly 0.85 — so if the pair ever stops being the boundary pair this
    fails as a premise rather than passing for the wrong reason."""
    candidate = _candidate(BOUNDARY_SLUG, BOUNDARY_NAME)
    ratios = {alias: _ratio(BOUNDARY_TERM, alias) for alias in aliases_for(candidate)}

    assert max(ratios.values()) == FUZZY_CUTOFF, ratios

    assert match_candidates(BOUNDARY_TERM, [candidate]) == (candidate,)


def test_a_pair_just_under_the_cutoff_does_not_match() -> None:
    """The exclusive half: catches a cutoff read as `>= 0.8` or compared
    against a rounded ratio. Every derived alias scores under 0.85 and the
    best of them is 0.848485, so this pair sits one step below the boundary
    pair above and nothing but the comparison decides it."""
    candidate = _candidate(UNDER_SLUG, UNDER_NAME)
    ratios = {alias: _ratio(UNDER_TERM, alias) for alias in aliases_for(candidate)}

    assert max(ratios.values()) < FUZZY_CUTOFF, ratios
    assert max(ratios.values()) > 0.84, ratios

    assert match_candidates(UNDER_TERM, [candidate]) == ()


def test_the_near_miss_role_pair_does_not_collapse_into_one_answer() -> None:
    """Catches a misspelt `billing-read` matching
    `billing-admin` as well. The term is a typo, so no exact or alias tier can
    decide it and only the cutoff separates the two — a looser cutoff hands a
    read request an admin role, or asks the user to choose between them."""
    read = _candidate("billing-read", "Billing Read")
    admin = _candidate("billing-admin", "Billing Admin")
    term = "billing raed"

    assert max(_ratio(term, alias) for alias in aliases_for(read)) >= FUZZY_CUTOFF
    assert max(_ratio(term, alias) for alias in aliases_for(admin)) < FUZZY_CUTOFF

    assert match_candidates(term, [read, admin]) == (read,)


def test_fuzzy_matches_come_back_best_ratio_first() -> None:
    """Catches a fuzzy tier returned in candidate order: the first three of
    those become the options a user is offered, so an unranked list can push
    the closest system off a capped disambiguation entirely."""
    closest = _candidate("billing-admins", "Billing Admins")
    further = _candidate("billing-adminz-eu", "Billing Adminz EU")
    term = "billingadmin"

    best_closest = max(_ratio(term, alias) for alias in aliases_for(closest))
    best_further = max(_ratio(term, alias) for alias in aliases_for(further))
    assert best_closest >= FUZZY_CUTOFF
    assert best_further >= FUZZY_CUTOFF
    assert best_closest > best_further, (best_closest, best_further)

    assert match_candidates(term, [further, closest]) == (closest, further)


def test_a_term_matching_nothing_returns_no_candidates() -> None:
    """Catches a matcher that falls back to "closest anyway" when nothing
    clears the cutoff: zero candidates is what routes the turn to
    `ask_clarifying`, and a fallback would resolve a system the user never
    named."""
    candidates = [_candidate("billing-admin", "Billing Admin"), _candidate("payroll", "Payroll")]
    assert match_candidates("warehouse logistics", candidates) == ()
    assert match_candidates("billing-admin", []) == ()


def test_the_matching_module_imports_no_framework_and_no_io() -> None:
    """Catches the matcher acquiring a repository read or a graph import:
    the ranking lives here as a pure function over candidates
    supplied as data, which is what lets the node keep the tenant-scoped read
    and this file need no database."""
    source = Path(str(matching_module.__file__))
    tree = ast.parse(source.read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module.split(".")[0])

    forbidden = {"langgraph", "sqlalchemy", "httpx", "redis", "qdrant_client", "rapidfuzz"}
    assert imported & forbidden == set()
    assert "difflib" in imported, "the fuzzy tier is stdlib difflib"


# --- filler words in a typed name ----------------------------------------


def _kyc_roles() -> tuple[Candidate, Candidate, Candidate]:
    return (
        _candidate("approver", "kyc.approver"),
        _candidate("analyst", "kyc.analyst"),
        _candidate("editor", "kyc.editor"),
    )


def test_approver_access_matches_the_approver_role() -> None:
    """Catches a matcher that keeps the word "access" in the typed name: the
    live chat typed "approver access" for KYC and matched no role at all. The
    premise first: the raw phrase is far below the cutoff against every alias,
    so only the word step (every word of "approver" is typed) can make it
    match."""
    approver, analyst, editor = _kyc_roles()
    assert max(_ratio("approver access", alias) for alias in aliases_for(approver)) < FUZZY_CUTOFF

    assert match_candidates("approver access", [analyst, approver, editor]) == (approver,)


@pytest.mark.parametrize("filler", ["role", "rights", "permissions", "level"])
def test_each_filler_word_is_dropped(filler: str) -> None:
    """Catches a matcher without the word step: each of "editor role", "editor
    rights", "editor permissions" and "editor level" must match the editor role
    and nothing else. Each phrase is below the cutoff, so the word step alone
    decides it, whatever the extra word is."""
    approver, analyst, editor = _kyc_roles()
    term = f"editor {filler}"
    assert max(_ratio(term, alias) for alias in aliases_for(editor)) < FUZZY_CUTOFF

    assert match_candidates(term, [approver, analyst, editor]) == (editor,)


def test_the_plural_roles_is_a_filler_word_too() -> None:
    """Catches a matcher without the word step: "editor roles" scores under the
    cutoff against the editor aliases, so only the word step (the word "editor"
    is typed) makes it match, with no plural handling needed."""
    approver, analyst, editor = _kyc_roles()
    assert max(_ratio("editor roles", alias) for alias in aliases_for(editor)) < FUZZY_CUTOFF

    assert match_candidates("editor roles", [approver, analyst, editor]) == (editor,)


def test_a_term_of_only_filler_words_is_kept() -> None:
    """Catches a matcher that drops or empties the typed word "access": with no
    candidate of that name the word step matches nothing, and with a candidate
    literally named Access the word step (and exact) matches that one, which an
    emptied term (the early empty return) could never do."""
    approver, analyst, editor = _kyc_roles()
    named_access = _candidate("access", "Access")

    assert match_candidates("access", [approver, analyst, editor]) == ()
    assert match_candidates("access", [approver, named_access]) == (named_access,)


# --- the whole-name word step ---------------------------------


def _best(term: str, candidate: Candidate) -> float:
    return max(_ratio(normalise(term), alias) for alias in aliases_for(candidate))


def test_the_filler_list_is_gone() -> None:
    """Catches a matcher that keeps the hand-kept word list next to the word
    step: any leftover list is a second place that decides what a phrase means."""
    assert not hasattr(matching_module, "FILLER_WORDS")
    assert not hasattr(matching_module, "without_filler")


def test_every_word_of_a_role_name_in_the_term_matches_it() -> None:
    """Catches a word step that is missing or too loose: "read access to
    billing" holds every word of `billing-read` and not all of `billing-admin`,
    so only the first may come back. Premise first: neither scores 0.85 on
    fuzzy, so only the word step can decide it."""
    read = _candidate("billing-read", "Billing Read")
    admin = _candidate("billing-admin", "Billing Admin")
    term = "read access to billing"
    assert _best(term, read) < FUZZY_CUTOFF
    assert _best(term, admin) < FUZZY_CUTOFF

    assert match_candidates(term, [admin, read]) == (read,)


def test_one_shared_word_is_not_enough() -> None:
    """Catches a word step where one shared word is enough ("most shared words
    wins"): "billing read access" would turn into `billing-admin`, a role the
    person never asked for. Premise first: fuzzy scores it under 0.85."""
    admin = _candidate("billing-admin", "Billing Admin")
    term = "billing read access"
    assert _best(term, admin) < FUZZY_CUTOFF

    assert match_candidates(term, [admin]) == ()


def test_the_name_with_more_matched_words_wins() -> None:
    """Catches a word step that returns every match: "billing admin access"
    holds all of `admin` and all of `billing-admin`, and the longer name is the
    better answer, so asking "which one" would be a needless question."""
    short = _candidate("admin", "Admin")
    long = _candidate("billing-admin", "Billing Admin")
    term = "billing admin access"
    assert _best(term, short) < FUZZY_CUTOFF
    assert _best(term, long) < FUZZY_CUTOFF

    assert match_candidates(term, [short, long]) == (long,)


def test_a_tie_returns_every_tied_candidate() -> None:
    """Catches a word step that picks one of two equal matches: "editor
    approver" names two roles, so both must come back, in the order given, to
    lead to the "which one do you mean" question. The analyst, not typed, must
    stay out."""
    approver, analyst, editor = _kyc_roles()
    term = "editor approver"
    assert _best(term, approver) < FUZZY_CUTOFF
    assert _best(term, editor) < FUZZY_CUTOFF

    assert match_candidates(term, [approver, analyst, editor]) == (approver, editor)
    assert match_candidates(term, [editor, analyst, approver]) == (editor, approver)


def test_the_display_name_words_count_too() -> None:
    """Catches a word step that reads only the slug: the slug `kyc-appr` has
    the word "appr", which was not typed, so only the words of the display name
    "KYC Approver" can match "kyc approver please". Premise first: fuzzy scores
    it under 0.85."""
    candidate = _candidate("kyc-appr", "KYC Approver")
    term = "kyc approver please"
    assert _best(term, candidate) < FUZZY_CUTOFF

    assert match_candidates(term, [candidate]) == (candidate,)


def test_an_exact_match_beats_the_word_step() -> None:
    """Catches a word step placed before the exact step: "wiki editor" holds
    every word of slug `editor` too, but the row whose name is exactly "wiki
    editor" must be the only answer."""
    exact = _candidate("wiki-ed", "wiki editor")
    words_only = _candidate("editor", "Editor")

    assert match_candidates("wiki editor", [words_only, exact]) == (exact,)
