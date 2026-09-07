"""The deterministic entity matcher (the design step 4, an earlier change decision G).

Three tiers, and a non-empty higher tier WINS OUTRIGHT: exact, then derived
alias, then fuzzy at an inclusive 0.85. Winning outright is the point — a
union of the tiers would return an exact slug match alongside two near
neighbours, and the turn would stop to ask a user which of three systems they
meant when they had already typed one of them exactly.

Pure and framework-free (decision G): the ranking takes candidates as DATA, so
the tenant-scoped read stays in the node and this module needs no database to
test. `policy/` stays out of it for the opposite reason — matching a string
against a directory is not an access decision.

Aliases are DERIVED (decision D), not stored: the normalised slug, the
normalised name, and each of those with its spaces removed. No schema change
and no seed change, and the day a tenant supplies its own alias list, that
list joins the derived set here rather than replacing this function.

`difflib` rather than `rapidfuzz` (decision C): the candidate set is one
tenant's systems or roles, tens of strings, and a dependency would buy speed
nothing on this path needs.
"""

import difflib
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

FUZZY_CUTOFF = 0.85
"""the design step 4's cutoff, INCLUSIVE: exactly 0.85 matches.

a project rule pins it from both sides with a pair whose ratios are asserted
as premises (`test_agent_matching.py::test_a_pair_scoring_exactly_the_cutoff_matches`
and `::test_a_pair_just_under_the_cutoff_does_not_match`). It is the only
thing separating `billing-read` from `billing-admin` on a misspelt term, so
loosening it is a privilege escalation wearing a typo's clothes —
`::test_the_near_miss_role_pair_does_not_collapse_into_one_answer`.
"""

_SEPARATORS = re.compile(r"[^a-z0-9]+")
"""Every run of non-alphanumeric characters, after casefolding. Applied to
both sides of every comparison, so `Billing_Admin`, `billing-admin` and
`Billing / Admin` are one term."""


@dataclass(frozen=True)
class Candidate:
    """One directory row a term might name, reduced to what matching needs.

    Deliberately not the ORM row: the matcher is pure, and a detached
    SQLAlchemy instance would drag a session into every test of it.
    """

    id: uuid.UUID
    slug: str
    name: str


def normalise(value: str) -> str:
    """Casefold, collapse every non-alphanumeric run to one space, strip.

    Applied to the user's wording and to the candidate alike, so the two are
    compared on the same footing: a user typing the display name they see in
    the UI must reach the same row as one typing the slug.
    """
    return _SEPARATORS.sub(" ", value.casefold()).strip()


def aliases_for(candidate: Candidate) -> frozenset[str]:
    """The names this candidate answers to, derived from its slug and name.

    Both normalised, and both with spaces removed: people type `billingadmin`
    for `billing-admin` constantly, and without the space-free spelling that
    term would fall through to the fuzzy tier where a near neighbour can win.
    Pinned by
    `test_agent_matching.py::test_aliases_for_derives_both_spellings_of_the_slug_and_the_name`.
    """
    forms = {normalise(candidate.slug), normalise(candidate.name)}
    return frozenset(forms | {form.replace(" ", "") for form in forms})


def match_candidates(term: str, candidates: Sequence[Candidate]) -> tuple[Candidate, ...]:
    """The candidates `term` names, best tier first and best ratio first.

    Returns the highest non-empty tier and nothing from below it. An empty
    result is a real answer — it routes the turn to `ask_clarifying` — and
    there is deliberately no "closest anyway" fallback, which would resolve a
    system the user never named
    (`test_agent_matching.py::test_a_term_matching_nothing_returns_no_candidates`).
    """
    normalised = normalise(term)
    if not normalised or not candidates:
        return ()

    exact = tuple(
        candidate
        for candidate in candidates
        if normalised in {normalise(candidate.slug), normalise(candidate.name)}
    )
    if exact:
        return exact

    alias = tuple(candidate for candidate in candidates if normalised in aliases_for(candidate))
    if alias:
        return alias

    scored = [
        (best, candidate)
        for candidate, best in (
            (candidate, _best_ratio(normalised, candidate)) for candidate in candidates
        )
        if best >= FUZZY_CUTOFF
    ]
    # Sorted on the ratio alone, descending, and `sorted` is stable, so equal
    # ratios keep the order the caller read them in. The first three of these
    # become the options a user is offered, so an unranked list could push the
    # closest system off a capped disambiguation entirely.
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return tuple(candidate for _, candidate in scored)


def _best_ratio(normalised_term: str, candidate: Candidate) -> float:
    """The candidate's closest alias, as a `SequenceMatcher` ratio.

    The best of the aliases rather than the slug alone: a candidate whose
    display name is nearly the term but whose slug is not would otherwise be
    invisible to the fuzzy tier.
    """
    return max(
        difflib.SequenceMatcher(None, normalised_term, alias).ratio()
        for alias in aliases_for(candidate)
    )
