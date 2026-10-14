"""An earlier change §6: the deterministic entity matcher, pure and graph-free.

Expected implementation surface (the implementer must provide exactly this):

``agent_api.graph.matching`` (PURE: no I/O, no langgraph, no SQLAlchemy)
    ``FUZZY_CUTOFF = 0.85`` (the design step 4)
    ``@dataclass(frozen=True) class Candidate`` — ``id``, ``slug``, ``name``.
    ``def normalise(value: str) -> str`` — casefolded, every run of
        non-alphanumeric characters collapsed to one space, stripped.
    ``def aliases_for(candidate: Candidate) -> frozenset[str]`` — decision D:
        derived from the normalised slug and name, plus each with its spaces
        removed. No alias column, no hand-written list.
    ``def match_candidates(term, candidates) -> tuple[Candidate, ...]`` —
        three tiers, and a NON-EMPTY HIGHER TIER WINS OUTRIGHT: exact (term
        equals the normalised slug or name) beats alias beats fuzzy
        (``difflib.SequenceMatcher(None, term, alias).ratio() >=
        FUZZY_CUTOFF``, best ratio first). The cutoff is INCLUSIVE.

Every ratio this file depends on is asserted with `difflib` BEFORE the
behaviour that depends on it, so a boundary pair that stops being the
boundary pair fails as a premise instead of quietly testing something else.
Nothing here needs a database, a socket or a graph.
"""

import ast
import difflib
import uuid
from pathlib import Path

from helpdesk.agent_api.graph import matching as matching_module
from helpdesk.agent_api.graph.matching import (
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
    """a project rule: catches a cutoff loosened to make a demo query match.
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
    """Catches decision D implemented as the slug alone: a user who types the
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
    """The inclusive half of the design's cutoff: catches a strict `>`
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
    """Catches plan §5's second risk: a misspelt `billing-read` matching
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
    decision G puts the ranking here as a pure function over candidates
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
    assert "difflib" in imported, "decision C: the fuzzy tier is stdlib difflib"
