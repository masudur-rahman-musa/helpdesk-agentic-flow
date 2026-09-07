"""An earlier change §6: Reciprocal Rank Fusion in `helpdesk/rag/retrieval.py`.

Expected implementation surface (the implementer must provide exactly this):

``helpdesk.rag.retrieval``
    ``RRF_K = 60`` — SPEC's fusion constant, the k of ``1 / (k + rank)``.
    ``TOP_K_DEFAULT = 6`` — the default page size.
    ``CANDIDATES_PER_ARM = 30`` — how many candidates EACH arm fetches before
        fusion (decision C), five times the default page.
    ``@dataclass(frozen=True) class SearchHit`` — ``chunk_id: uuid.UUID``,
        ``document_id: uuid.UUID``, ``document_slug: str``, ``version: str``,
        ``heading_path: tuple[str, ...]``, ``text: str``, ``score: float``,
        ``dense_score: float | None``. ``score`` is the arm's own score on the
        way in and the FUSED score on the way out; ``dense_score`` is the dense
        arm's cosine, carried through fusion so `should_refuse` can read it
        (decision A), and ``None`` for a chunk only the sparse arm found.
    ``def fuse(dense: Sequence[SearchHit], sparse: Sequence[SearchHit], *,
        top_k: int = TOP_K_DEFAULT) -> list[SearchHit]`` — one entry per
        chunk id, ordered by descending ``1/(RRF_K + rank)`` summed over the
        arms that ranked it, ranks 1-based, truncated to ``top_k``.

Pure: no store, no session, no model. Every hit here is hand-built, so the
ranks are the case's own and nothing depends on what a real arm would return.
"""

import uuid
from collections.abc import Sequence

import pytest
from helpdesk.rag.retrieval import (
    CANDIDATES_PER_ARM,
    RRF_K,
    TOP_K_DEFAULT,
    SearchHit,
    fuse,
)

DOCUMENT_ID = uuid.UUID("11111111-1111-5111-8111-111111111111")

# Chunk ids as small integers, so an expected ORDER reads as a list of numbers.
A, B, C, D, E = 1, 2, 3, 4, 5


def _chunk_id(number: int) -> uuid.UUID:
    return uuid.UUID(int=number)


def _hit(number: int, *, score: float = 0.0, dense_score: float | None = None) -> SearchHit:
    """One arm's hit for chunk `number`, with everything but its rank fixed."""
    return SearchHit(
        chunk_id=_chunk_id(number),
        document_id=DOCUMENT_ID,
        document_slug="fake-access-control-policy",
        version="v1",
        heading_path=("1. Purpose",),
        text=f"Fake chunk {number} of an obviously fake policy.",
        score=score,
        dense_score=dense_score,
    )


def _ids(hits: Sequence[SearchHit]) -> list[int]:
    return [hit.chunk_id.int for hit in hits]


# The literal 60, not `RRF_K`: a score expectation written against the
# implementation's own constant would agree with any value it happened to
# hold, and pinning the constant is a separate case below.
def _expected(*ranks: int) -> float:
    return sum(1.0 / (60 + rank) for rank in ranks)


# --- the constants ------------------------------------------


def test_the_fusion_constant_is_exactly_sixty() -> None:
    """Catches fusion done with any other k — the qdrant server-side
    `FusionQuery.RRF` default that decision B rejected, or a bare `1/rank` —
    each of which changes every fused score the page is ordered by."""
    assert RRF_K == 60


def test_the_default_page_size_is_exactly_six() -> None:
    """Catches a default page of any other size: `TOP_K_DEFAULT` is the
    number of chunks the agent's prompt is built from, and a silently wider
    page is a longer context on every answer."""
    assert TOP_K_DEFAULT == 6


def test_each_arm_fetches_exactly_thirty_candidates() -> None:
    """Catches decision C's rejected shape, an arm fetching `top_k`
    candidates: with 6 and 6 the fused page is nearly the dense page, so
    fusion still runs and every other case here still passes while the sparse
    arm can no longer rescue anything it alone ranked."""
    assert CANDIDATES_PER_ARM == 30


# --- the fused order ----------------------------------------------------------


def test_fusion_orders_by_the_summed_reciprocal_ranks() -> None:
    """Catches a fuse that orders by either arm's own score, or by the better
    of the two ranks instead of their sum: the expected list is pinned as an
    exact ORDER, so a page holding the right five chunks in the wrong
    sequence fails here."""
    dense = [_hit(A), _hit(B), _hit(C), _hit(D)]
    sparse = [_hit(C), _hit(A), _hit(E)]

    fused = fuse(dense, sparse, top_k=5)

    assert _ids(fused) == [A, C, B, E, D]


def test_the_fused_score_is_the_one_based_reciprocal_rank_sum() -> None:
    """Catches ranks counted from zero (`1/(60 + 0)` for the leader) and a k
    of anything but 60: both keep the ORDER above intact and change every
    number, so only an exact score assertion separates them."""
    dense = [_hit(A), _hit(B), _hit(C), _hit(D)]
    sparse = [_hit(C), _hit(A), _hit(E)]

    fused = fuse(dense, sparse, top_k=5)
    scores = {hit.chunk_id.int: hit.score for hit in fused}

    assert scores[A] == pytest.approx(_expected(1, 2))
    assert scores[C] == pytest.approx(_expected(3, 1))
    assert scores[B] == pytest.approx(_expected(2))
    assert scores[E] == pytest.approx(_expected(3))
    assert scores[D] == pytest.approx(_expected(4))


def test_a_chunk_in_both_arms_outranks_one_that_leads_a_single_arm() -> None:
    """Catches a fuse that keeps an arm's leader at the top by construction
    (a merge that walks the dense arm first, or one that adds nothing for a
    second appearance): the chunk both arms placed SECOND scores 2/62 against
    the dense leader's 1/61 and must come first."""
    dense = [_hit(B), _hit(A)]
    sparse = [_hit(C), _hit(A)]

    fused = fuse(dense, sparse, top_k=3)

    assert _ids(fused)[0] == A
    assert fused[0].score == pytest.approx(_expected(2, 2))


def test_a_chunk_ranked_twentieth_by_one_arm_and_first_by_the_other_reaches_the_page() -> None:
    """Catches the failure decision C's 30 candidates exist to prevent, at
    the fusion level: with a full 30-deep dense arm, a chunk sitting 20th
    there and 1st in the sparse arm must still make a six-hit page. A fuse
    that ordered by the dense arm's rank alone would leave it at 20."""
    dense = [_hit(index) for index in range(100, 100 + CANDIDATES_PER_ARM)]
    rescued = dense[19]
    sparse = [rescued, *(_hit(index) for index in range(200, 200 + 5))]

    fused = fuse(dense, sparse, top_k=TOP_K_DEFAULT)

    assert rescued.chunk_id in {hit.chunk_id for hit in fused}
    assert len(fused) == TOP_K_DEFAULT


def test_a_chunk_both_arms_returned_appears_once() -> None:
    """Catches a fuse that concatenates the two arms and sorts: the shared
    chunk then appears twice in one page, which is the plan §5 off-by-one and
    is invisible to any assertion made against a SET of ids."""
    dense = [_hit(A), _hit(B)]
    sparse = [_hit(A), _hit(B)]

    fused = fuse(dense, sparse, top_k=6)

    assert _ids(fused) == [A, B]


def test_the_page_is_truncated_to_top_k() -> None:
    """Catches a fuse that ignores `top_k` and returns every candidate it
    fused: the arms hold five chunks between them and a page of two was
    asked for, so a longer page reaches the agent's prompt unbounded."""
    dense = [_hit(A), _hit(B), _hit(C)]
    sparse = [_hit(D), _hit(E), _hit(A)]

    fused = fuse(dense, sparse, top_k=2)

    assert len(fused) == 2
    assert _ids(fused)[0] == A


# --- the empty arms -----------------------------------------------------------


def test_an_empty_sparse_arm_returns_the_dense_arm_order_unchanged() -> None:
    """Catches a fuse that intersects the arms instead of summing over them:
    a query no bm25 term matches then returns nothing at all, and the dense
    half of a hybrid search is silently switched off."""
    dense = [_hit(A), _hit(B), _hit(C)]

    fused = fuse(dense, [], top_k=6)

    assert _ids(fused) == [A, B, C]


def test_an_empty_dense_arm_returns_the_sparse_arm_order_unchanged() -> None:
    """Catches a fuse written as "walk the dense arm, then top it up": the
    sparse-only page comes back empty, which the previous case cannot see
    because it fills the dense arm."""
    sparse = [_hit(C), _hit(B), _hit(A)]

    fused = fuse([], sparse, top_k=6)

    assert _ids(fused) == [C, B, A]


def test_two_empty_arms_fuse_to_an_empty_page() -> None:
    """Catches a fuse that returns a placeholder or raises on an empty
    corpus: `should_refuse` is defined on an empty page, so an empty page
    must be reachable rather than an error."""
    assert fuse([], [], top_k=6) == []


# --- what fusion must carry forward -------------------------------------------


def test_the_dense_cosine_survives_fusion_for_a_chunk_the_sparse_arm_led_with() -> None:
    """Catches a fuse that builds each fused hit from whichever arm it met
    FIRST: the sparse arm's copy carries `dense_score=None`, so the cosine
    decision A gates the refusal on is lost for exactly the chunks fusion
    promoted, and `should_refuse` then refuses a strong dense match."""
    dense = [_hit(B, score=0.4, dense_score=0.4), _hit(A, score=0.91, dense_score=0.91)]
    sparse = [_hit(A, score=7.5), _hit(B, score=6.0)]

    fused = fuse(dense, sparse, top_k=2)
    cosines = {hit.chunk_id.int: hit.dense_score for hit in fused}

    assert cosines[A] == pytest.approx(0.91)
    assert cosines[B] == pytest.approx(0.4)


def test_a_chunk_only_the_sparse_arm_found_carries_no_cosine() -> None:
    """Catches a fuse that invents a cosine for a sparse-only chunk (0.0 is
    harmless, the sparse arm's own bm25 score is not: an unbounded lexical
    score read as a cosine passes the 0.35 refusal on any query)."""
    dense = [_hit(A, score=0.5, dense_score=0.5)]
    sparse = [_hit(B, score=9.25)]

    fused = fuse(dense, sparse, top_k=2)
    cosines = {hit.chunk_id.int: hit.dense_score for hit in fused}

    assert cosines[B] is None


def test_the_fused_hit_keeps_the_chunks_payload_fields() -> None:
    """Catches a fuse that rebuilds hits from ids alone: the page would then
    reach the citation step with no text, version or heading path, and the
    only thing that could restore them is a per-hit read of the store."""
    dense = [_hit(A)]

    fused = fuse(dense, [], top_k=1)

    assert fused[0].chunk_id == _chunk_id(A)
    assert fused[0].document_id == DOCUMENT_ID
    assert fused[0].document_slug == "fake-access-control-policy"
    assert fused[0].version == "v1"
    assert fused[0].heading_path == ("1. Purpose",)
    assert fused[0].text == _hit(A).text
