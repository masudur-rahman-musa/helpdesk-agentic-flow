"""Hybrid retrieval over one tenant's collection (the design, §12, §13).

Two arms, one fusion, one refusal predicate. The dense arm asks the
collection for the nearest chunks by cosine, the sparse arm for the best bm25
matches, and Reciprocal Rank Fusion over the two orders is what the page is
ordered by (decision B). Both arms are filtered `is_current=true`, so a
superseded version stays readable for audit and is never cited as policy;
pinned by
`test_rag_retrieval.py::test_only_the_current_version_comes_back_after_a_supersede`,
which asserts the v1 points are still in the collection so the filter and a
delete cannot be confused.

**The refusal is measured on the dense cosine, not the fused score**
(decision A, a design note). RRF with k=60 over two arms is bounded above by
2/61 ≈ 0.033, so the design's threshold of 0.35 read against a fused score
refuses every answer the system could ever give, and normalising the fused
score by its own maximum measures a rank rather than a similarity — an
out-of-corpus query still has a rank-1 result in both arms. `SearchHit`
therefore carries `dense_score` through fusion, and `should_refuse` reads
that. Pinned from both sides by
`test_rag_refusal.py::test_the_threshold_is_measured_on_dense_cosine_not_the_fused_score`
and `::test_a_page_of_real_fused_scores_still_answers_on_a_strong_cosine`.

**Fusion happens here, in Python, not in the store** (decision B). Qdrant's
own `FusionQuery.RRF` is one round trip fewer, but its k is the server's
rather than SPEC's 60 and its output does not expose the per-arm dense score
the refusal needs.

**`title` is not in the point payload** (decision E). 14c fixed that payload
as an exact six-key set, and widening it means re-ingesting every tenant, so
a citation's title is resolved from `policy_documents` in ONE query for the
whole page — never per hit, pinned by
`test_rag_citations.py::test_a_page_over_three_documents_issues_one_title_query`.
A hit whose document row has gone raises rather than rendering a blank title:
a citation the agent cannot render is a wrong answer, not a cosmetic gap.

**Three functions, because the caller needs two answers.** `search_hits`
retrieves and fuses, `should_refuse` reads that page, and
`resolve_citations` renders it for a model. `search_policy_chunks` is the two
halves together for a caller that only wants the page. The split is what lets
row 17 decide to refuse WITHOUT paying the title query for a page it will not
use, and it is the reason `Citation` can stay exactly SPEC's seven fields
while the internal cosine travels on `SearchHit` (plan §3 lists the shorter
surface; the two extra names are this plan's Deviations).

The store is reached only through `vectors.py`: its collection naming, its
named vectors and its `_translated` exception table, so a partial failure of
either arm is one typed `INTERNAL` rather than half a page returned as a
whole one.
"""

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from qdrant_client import AsyncQdrantClient, models
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.errors import ErrorCode, ToolError
from helpdesk.models import PolicyDocument
from helpdesk.rag.embedding import Embedder
from helpdesk.rag.naming import check_query, check_top_k
from helpdesk.rag.vectors import (
    DENSE_VECTOR_NAME,
    SPARSE_VECTOR_NAME,
    _translated,
    collection_name,
)

RRF_K = 60
"""The k of Reciprocal Rank Fusion, `1 / (k + rank)`, ranks 1-based.

SPEC's constant, pinned exactly by
`test_rag_fusion.py::test_the_fusion_constant_is_exactly_sixty` and, as the
number every fused score is built from, by
`::test_the_fused_score_is_the_one_based_reciprocal_rank_sum`. A larger k
flattens the two arms toward equal weight, a smaller one lets either arm's
leader win alone; neither is visible in the ORDER of a small page, which is
why the score assertion exists beside the order one.
"""

TOP_K_DEFAULT = 6
"""The default page size: how many chunks an answer is built from."""

CANDIDATES_PER_ARM = 30
"""How many candidates EACH arm fetches before fusion (decision C).

Five times the default page. Fetching `top_k` per arm makes fusion almost a
no-op — with 6 and 6 the fused page is nearly the dense page — so the case
hybrid retrieval exists for, a chunk ranked 20th by the dense arm and 1st by
the sparse one, can never reach the page at all. Pinned as a value by
`test_rag_fusion.py::test_each_arm_fetches_exactly_thirty_candidates`, as a
request by
`test_rag_retrieval.py::test_each_arm_asks_for_the_candidate_count_against_its_own_named_vector`,
and as the behaviour it buys by
`test_rag_fusion.py::test_a_chunk_ranked_twentieth_by_one_arm_and_first_by_the_other_reaches_the_page`.
"""

REFUSAL_THRESHOLD = 0.35
"""the design's floor, read against the best DENSE COSINE of the page.

Not against the fused score (decision A, a design note), and inclusive on the
ANSWERING side: exactly 0.35 answers, 0.3499 refuses. Pinned exactly by
`test_rag_refusal.py::test_the_refusal_threshold_is_exactly_thirty_five_hundredths`
and at both edges by `::test_the_refusal_decision_table`.
"""


class CitationUnresolvableError(ToolError):
    """A retrieved chunk names a document row that is not there.

    `INTERNAL`, not `VALIDATION` or `NOT_FOUND`: nothing about the caller's
    query is wrong and nothing they asked for is missing — the two stores
    disagree, which is this repo's own invariant broken (a point whose
    `document_id` has no `policy_documents` row can only come from a delete
    that left the collection behind). Raised rather than rendering the
    citation with a blank title, because a citation the agent cannot show is
    a wrong answer and not a cosmetic gap. Pinned by
    `test_rag_citations.py::test_a_hit_whose_document_row_is_gone_raises_instead_of_a_blank_title`,
    whose hit names a chunk row that DOES exist, so only the title lookup can
    be what refuses it.
    """

    code = ErrorCode.INTERNAL


@dataclass(frozen=True)
class SearchHit:
    """One retrieved chunk, as an arm returned it or as fusion left it.

    `score` is the arm's own score on the way in and the FUSED score on the
    way out; `dense_score` is the dense arm's cosine, carried through fusion
    unchanged so `should_refuse` can read it after the two orders have been
    mixed. It is `None` for a chunk only the sparse arm found — never the
    bm25 score and never 0.0, because an unbounded lexical score read as a
    cosine passes the 0.35 floor on any query. Pinned by
    `test_rag_fusion.py::test_a_chunk_only_the_sparse_arm_found_carries_no_cosine`
    and `::test_the_dense_cosine_survives_fusion_for_a_chunk_the_sparse_arm_led_with`.

    Internal to this module and to its caller's refusal decision: what
    reaches a model is `Citation`, which carries neither this cosine nor the
    `document_id`.
    """

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_slug: str
    version: str
    heading_path: tuple[str, ...]
    text: str
    score: float
    dense_score: float | None


@dataclass(frozen=True)
class Citation:
    """What an answer cites: exactly SPEC's seven fields and nothing else.

    Pinned as an exact field set by
    `test_rag_citations.py::test_the_citation_carries_exactly_the_seven_spec_fields`,
    which fails in both directions — an eighth field is the internal cosine
    leaking into a prompt, a missing one is a citation that cannot be
    rendered.

    `chunk_id` is the Qdrant point id, so a stored citation joins exactly one
    `policy_chunks` row through `qdrant_point_id` (14a decision F made that
    column non-NULL for this); pinned by
    `::test_every_returned_chunk_id_selects_exactly_one_chunk_row`.
    """

    document_slug: str
    title: str
    version: str
    heading_path: tuple[str, ...]
    chunk_id: uuid.UUID
    text: str
    score: float


def fuse(
    dense: Sequence[SearchHit],
    sparse: Sequence[SearchHit],
    *,
    top_k: int = TOP_K_DEFAULT,
) -> list[SearchHit]:
    """The two arms' orders, fused by RRF and cut to `top_k`.

    Pure: no store, no session, no model. One entry per chunk id, scored
    `sum(1 / (RRF_K + rank))` over the arms that ranked it with ranks counted
    from 1, so a chunk both arms placed second (2/62) outranks a chunk one arm
    placed first (1/61). A chunk in both arms appears ONCE — the failure a
    concatenate-and-sort produces, which is invisible to any assertion made
    against a set of ids, pinned by
    `test_rag_fusion.py::test_a_chunk_both_arms_returned_appears_once`.

    An empty arm contributes nothing and does not empty the page: this is a
    sum over the arms, never an intersection of them, so a query no bm25 term
    matches still answers from the dense arm alone. Both empty fuse to `[]`,
    which is the page `should_refuse` is defined on.

    Ties in the summed score keep the order the chunks were first met in
    (dense arm first): Python's sort is stable, and the alternative — ordering
    ties by an arm's own score — would compare a cosine against a bm25 score.
    """
    scores: dict[uuid.UUID, float] = {}
    hits: dict[uuid.UUID, SearchHit] = {}
    cosines: dict[uuid.UUID, float | None] = {}

    for arm in (dense, sparse):
        for rank, hit in enumerate(arm, start=1):
            scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + 1.0 / (RRF_K + rank)
            hits.setdefault(hit.chunk_id, hit)
            # The dense arm is walked first, so a `None` from the sparse copy
            # of a chunk both arms found can never overwrite its cosine.
            if cosines.get(hit.chunk_id) is None:
                cosines[hit.chunk_id] = hit.dense_score

    ordered = sorted(hits, key=lambda chunk_id: scores[chunk_id], reverse=True)
    return [
        _replace_scores(hits[chunk_id], score=scores[chunk_id], dense_score=cosines[chunk_id])
        for chunk_id in ordered[:top_k]
    ]


def should_refuse(hits: Sequence[SearchHit]) -> bool:
    """True when nothing on this page is a dense match worth answering from.

    Pure. The best cosine over the WHOLE page, not `hits[0]`'s: the page
    arrives in fused order, so its strongest dense match may sit second
    (pinned by
    `test_rag_refusal.py::test_the_best_cosine_is_taken_over_the_whole_page_not_the_first_hit`).
    Hits the dense arm never returned carry no cosine and are simply not
    candidates for the maximum — a page of them alone refuses, and one
    beside a strong dense hit does not hide it.

    Inclusive on the answering side (`>= REFUSAL_THRESHOLD`), and an empty
    page refuses: a query that retrieved nothing is exactly the out-of-corpus
    question requirement #14 exists to catch, so `max(..., default=)` must
    not be able to answer it.
    """
    cosines = [hit.dense_score for hit in hits if hit.dense_score is not None]
    if not cosines:
        return True
    return max(cosines) < REFUSAL_THRESHOLD


async def search_hits(
    client: AsyncQdrantClient,
    *,
    tenant_slug: str,
    query: str,
    embedder: Embedder,
    top_k: int = TOP_K_DEFAULT,
) -> list[SearchHit]:
    """Both arms against this tenant's collection, fused and cut to `top_k`.

    The three pre-checks run before ANY effect: `check_top_k` and
    `check_query` here, and `collection_name`'s own two inside it, so a
    refused argument reaches neither the embedder nor the store. Pinned
    against recording stand-ins asserting zero calls on both, rather than
    against the exception alone, by
    `test_rag_retrieval.py::test_a_refused_argument_reaches_neither_the_store_nor_the_embedder`.

    No session and no title lookup: a caller deciding to refuse (`should_refuse`)
    must not pay a read for a page it will discard.
    """
    check_top_k(top_k)
    check_query(query)
    name = collection_name(tenant_slug)

    dense_vector = embedder.embed_dense([query])[0]
    sparse_vector = embedder.embed_sparse([query])[0]

    with _translated():
        dense_response = await client.query_points(
            name,
            query=list(dense_vector),
            using=DENSE_VECTOR_NAME,
            limit=CANDIDATES_PER_ARM,
            query_filter=_current_only(),
            with_payload=True,
        )
        sparse_response = await client.query_points(
            name,
            query=models.SparseVector(
                indices=list(sparse_vector.indices), values=list(sparse_vector.values)
            ),
            using=SPARSE_VECTOR_NAME,
            limit=CANDIDATES_PER_ARM,
            query_filter=_current_only(),
            with_payload=True,
        )

    return fuse(
        _hits(dense_response.points, dense=True),
        _hits(sparse_response.points, dense=False),
        top_k=top_k,
    )


async def resolve_citations(session: AsyncSession, hits: Sequence[SearchHit]) -> list[Citation]:
    """Render a fused page as citations, titles resolved in ONE query.

    The order of `hits` is the order of the result: the title read is a batch
    lookup and never a re-ordering of the page, pinned by
    `test_rag_citations.py::test_the_citations_keep_the_fused_order`, which
    interleaves two documents so document order and fused order differ.

    A hit whose `document_id` has no row raises `CitationUnresolvableError`
    rather than defaulting the title. The lookup is tenant-scoped by the ORM
    guard on the bound session, so a point from another tenant's collection
    could not resolve either — but isolation is the collection name (SPEC
    §12.4), and this read is not what enforces it.
    """
    titles = await _titles_for(session, {hit.document_id for hit in hits})
    citations = []
    for hit in hits:
        title = titles.get(hit.document_id)
        if title is None:
            raise CitationUnresolvableError(
                f"the document of chunk {hit.chunk_id} is no longer registered"
            )
        citations.append(
            Citation(
                document_slug=hit.document_slug,
                title=title,
                version=hit.version,
                heading_path=hit.heading_path,
                chunk_id=hit.chunk_id,
                text=hit.text,
                score=hit.score,
            )
        )
    return citations


async def search_policy_chunks(
    session: AsyncSession,
    client: AsyncQdrantClient,
    *,
    tenant_slug: str,
    query: str,
    embedder: Embedder,
    top_k: int = TOP_K_DEFAULT,
) -> list[Citation]:
    """The whole search: retrieve, fuse, resolve titles. At most `top_k` hits.

    `search_hits` then `resolve_citations`, for a caller that wants the page
    and not the refusal decision. A caller that wants both calls the two
    halves itself, so `should_refuse` can run on the fused hits before any
    title is read.
    """
    hits = await search_hits(
        client, tenant_slug=tenant_slug, query=query, embedder=embedder, top_k=top_k
    )
    return await resolve_citations(session, hits)


def _current_only() -> models.Filter:
    """The filter both arms carry: only the current version of a document.

    the design keeps superseded points in the collection for audit, so
    without this every arm returns the old spans beside the new ones and the
    agent cites a policy that no longer applies.
    """
    return models.Filter(
        must=[models.FieldCondition(key="is_current", match=models.MatchValue(value=True))]
    )


def _hits(points: Iterable[Any], *, dense: bool) -> list[SearchHit]:
    """One arm's response as `SearchHit`s, in the order the store returned.

    `dense_score` is the point's score for the dense arm and `None` for the
    sparse one: the two numbers are a cosine and a bm25 score, and only the
    first has a threshold's meaning (decision A).

    Every field is read from the payload 14c pinned, so a payload key set
    that changed shows up here as a `KeyError` on the first search rather
    than as a citation missing a field.
    """
    hits = []
    for point in points:
        payload = point.payload or {}
        score = float(point.score)
        hits.append(
            SearchHit(
                chunk_id=uuid.UUID(str(point.id)),
                document_id=uuid.UUID(str(payload["document_id"])),
                document_slug=str(payload["document_slug"]),
                version=str(payload["version"]),
                heading_path=tuple(payload["heading_path"]),
                text=str(payload["text"]),
                score=score,
                dense_score=score if dense else None,
            )
        )
    return hits


async def _titles_for(session: AsyncSession, document_ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    """The title of each named document, in ONE statement for the whole page.

    A per-hit read returns exactly the same citations, so the difference is
    only visible as a statement count; that is what
    `test_rag_citations.py::test_a_page_over_three_documents_issues_one_title_query`
    asserts. An empty page issues no statement at all.

    This is a `select` against this package's own table, which is where
    `store.py` and `chunks.py` put theirs for the same reason: the package
    that owns a table owns the statements against it, and no service builds
    one. The session guard adds the tenant filter.
    """
    if not document_ids:
        return {}
    result = await session.execute(
        select(PolicyDocument.id, PolicyDocument.title).where(PolicyDocument.id.in_(document_ids))
    )
    return {row.id: row.title for row in result}


def _replace_scores(hit: SearchHit, *, score: float, dense_score: float | None) -> SearchHit:
    """The same chunk with its fused score and its carried cosine.

    Rebuilt rather than mutated because `SearchHit` is frozen, and rebuilt
    from the hit rather than from its id because the page must reach the
    citation step with its text, version and heading path intact — the only
    thing that could restore those is a per-hit read of the store. Pinned by
    `test_rag_fusion.py::test_the_fused_hit_keeps_the_chunks_payload_fields`.
    """
    return SearchHit(
        chunk_id=hit.chunk_id,
        document_id=hit.document_id,
        document_slug=hit.document_slug,
        version=hit.version,
        heading_path=hit.heading_path,
        text=hit.text,
        score=score,
        dense_score=dense_score,
    )
