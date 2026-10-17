"""Turns a typed name into directory rows: exact, then alias, then fuzzy.

The first tier with a match wins, so an exact name never comes back mixed
with near neighbours. Pure: the node reads the rows and passes them in.
"""

import difflib
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

FUZZY_CUTOFF = 0.85
"""Inclusive. It keeps a typo of `billing-read` off `billing-admin`, so
loosening it grants more than was asked."""

_SEPARATORS = re.compile(r"[^a-z0-9]+")
"""Runs of non-alphanumerics, so `Billing_Admin` and `billing-admin` are
one term."""


@dataclass(frozen=True)
class Candidate:
    """One directory row, reduced so the matcher needs no ORM."""

    id: uuid.UUID
    slug: str
    name: str


def normalise(value: str) -> str:
    """Casefold, turn each non-alphanumeric run into one space, strip."""
    return _SEPARATORS.sub(" ", value.casefold()).strip()


def aliases_for(candidate: Candidate) -> frozenset[str]:
    """The names this candidate answers to: slug and name, each also without
    spaces, since people type `billingadmin` and would otherwise fall to fuzzy."""
    forms = {normalise(candidate.slug), normalise(candidate.name)}
    return frozenset(forms | {form.replace(" ", "") for form in forms})


def match_candidates(term: str, candidates: Sequence[Candidate]) -> tuple[Candidate, ...]:
    """The candidates `term` names, best first. Empty leads to a question; there
    is no "closest anyway" guess."""
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
    # Best first, stable on ties. Only the first few are offered, so an
    # unsorted list could drop the closest one.
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return tuple(candidate for _, candidate in scored)


def _best_ratio(normalised_term: str, candidate: Candidate) -> float:
    """The score of the candidate's closest alias, so a near display name counts
    even when the slug is far off."""
    return max(
        difflib.SequenceMatcher(None, normalised_term, alias).ratio()
        for alias in aliases_for(candidate)
    )
