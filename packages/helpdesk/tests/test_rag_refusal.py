"""An earlier change §6: the refusal predicate in `helpdesk/rag/retrieval.py`.

Expected implementation surface (the implementer must provide exactly this):

``helpdesk.rag.retrieval``
    ``REFUSAL_THRESHOLD = 0.35`` — the design's number, read against the
        best DENSE COSINE of the page and not against the fused score
        (decision A, a design note).
    ``def should_refuse(hits: Sequence[SearchHit]) -> bool`` — True when the
        page holds no dense cosine at or above the threshold; False at
        exactly the threshold (inclusive on the ANSWERING side). An empty
        page refuses.

Pure: no store, no session, no model. Every hit is hand-built, so the case
chooses both numbers the predicate could read and the two can disagree —
which is the only way decision A is testable at all.
"""

import uuid
from collections.abc import Sequence

import pytest
from helpdesk.rag.retrieval import REFUSAL_THRESHOLD, SearchHit, should_refuse

DOCUMENT_ID = uuid.UUID("22222222-2222-5222-8222-222222222222")

# What RRF over two arms actually produces for a chunk both arms ranked first:
# 2/61. Every fused score the system can ever emit is under 0.033, which is
# the whole of decision A (SPEC's 0.35 read against a fused score refuses
# every answer). Written as a literal, not derived from `RRF_K`.
BEST_POSSIBLE_FUSED_SCORE = 2.0 / 61.0


def _hit(number: int, *, score: float, dense_score: float | None) -> SearchHit:
    return SearchHit(
        chunk_id=uuid.UUID(int=number),
        document_id=DOCUMENT_ID,
        document_slug="fake-access-control-policy",
        version="v1",
        heading_path=("1. Purpose",),
        text=f"Fake chunk {number} of an obviously fake policy.",
        score=score,
        dense_score=dense_score,
    )


def _page(*cosines: float | None) -> Sequence[SearchHit]:
    """A page in fused order whose dense cosines are the case's own."""
    return [
        _hit(index + 1, score=BEST_POSSIBLE_FUSED_SCORE, dense_score=cosine)
        for index, cosine in enumerate(cosines)
    ]


# --- the constant -------------------------------------------


def test_the_refusal_threshold_is_exactly_thirty_five_hundredths() -> None:
    """Catches a threshold of any other value: this is the single number
    that decides whether the agent answers from the corpus or offers a
    ticket, and a band would admit a wrong one."""
    assert REFUSAL_THRESHOLD == 0.35


# --- the boundary, both sides -------------------------------------------------


def test_a_best_cosine_of_exactly_the_threshold_answers() -> None:
    """Catches an exclusive comparison (`> REFUSAL_THRESHOLD`): the boundary
    is inclusive on the answering side, so a page whose best cosine lands
    exactly on 0.35 is answered and not refused."""
    assert should_refuse(_page(0.35)) is False


def test_a_best_cosine_just_below_the_threshold_refuses() -> None:
    """Catches a predicate that answers whenever the page is non-empty, and
    one comparing against a lower constant: 0.3499 is the largest cosine the
    system must still refuse."""
    assert should_refuse(_page(0.3499)) is True


def test_an_empty_page_refuses() -> None:
    """Catches `max(..., default=1.0)` and any predicate that reads
    `hits[0]` without a length check: a query that retrieved nothing is
    exactly the out-of-corpus question requirement #14 exists to catch, and
    answering it is the worst case, not a neutral one."""
    assert should_refuse(()) is True


# --- what the threshold is measured on (decision A) ---------------------------


def test_the_threshold_is_measured_on_dense_cosine_not_the_fused_score() -> None:
    """Catches an implementation that gates on `hit.score`: this page's fused
    scores are 1.0 (well over the threshold) while its best dense cosine is
    0.2, so a fused-score reader ANSWERS a page nothing in the corpus
    matches. The two numbers disagree by construction; a page where both sat
    on the same side would pin neither."""
    page = [
        _hit(1, score=1.0, dense_score=0.2),
        _hit(2, score=0.9, dense_score=0.19),
    ]

    assert should_refuse(page) is True


def test_a_page_of_real_fused_scores_still_answers_on_a_strong_cosine() -> None:
    """Catches the same swap from the other side, and it is the case that
    bites in production: every fused score RRF over two arms can produce is
    under 0.033, so a `hit.score >= 0.35` implementation refuses a page whose
    dense cosine is 0.91 — every answer the system would ever give."""
    page = [
        _hit(1, score=BEST_POSSIBLE_FUSED_SCORE, dense_score=0.91),
        _hit(2, score=BEST_POSSIBLE_FUSED_SCORE / 2, dense_score=0.5),
    ]

    assert BEST_POSSIBLE_FUSED_SCORE < REFUSAL_THRESHOLD
    assert should_refuse(page) is False


def test_the_best_cosine_is_taken_over_the_whole_page_not_the_first_hit() -> None:
    """Catches a predicate reading `hits[0].dense_score` — plausible,
    because the page arrives ordered — which refuses a page whose strongest
    dense match RRF placed second. The leading hit is below the threshold and
    the second is above it, so only a max over the page answers."""
    assert should_refuse(_page(0.2, 0.9, 0.1)) is False


def test_a_page_whose_hits_all_came_from_the_sparse_arm_refuses() -> None:
    """Catches a predicate that compares `None` against the threshold (a
    TypeError at the tool boundary rather than a refusal) and one that treats
    a missing cosine as passing: a page found by bm25 alone carries no dense
    evidence, and answering from it is the lexical-coincidence case."""
    assert should_refuse(_page(None, None)) is True


def test_a_single_missing_cosine_does_not_hide_a_strong_one() -> None:
    """Catches a predicate that gives up at the first `None` (returning True
    early, or letting `None` win a `max`): the page holds one sparse-only hit
    and one dense hit at 0.9, and it must be answered."""
    assert should_refuse(_page(None, 0.9)) is False


@pytest.mark.parametrize(
    ("cosines", "expected"),
    [
        pytest.param((0.35,), False, id="exactly-the-threshold"),
        pytest.param((0.3499,), True, id="one-ten-thousandth-below"),
        pytest.param((0.3501,), False, id="just-above"),
        pytest.param((0.0,), True, id="no-similarity-at-all"),
        pytest.param((1.0,), False, id="an-exact-match"),
        pytest.param((), True, id="an-empty-page"),
        pytest.param((None,), True, id="sparse-only"),
    ],
)
def test_the_refusal_decision_table(cosines: tuple[float | None, ...], expected: bool) -> None:
    """Catches an off-by-one at either edge of the decision table in one
    place: the three rows around 0.35 pin the comparison operator, and the
    empty and sparse-only rows pin what the predicate does with no cosine at
    all."""
    assert should_refuse(_page(*cosines)) is expected
