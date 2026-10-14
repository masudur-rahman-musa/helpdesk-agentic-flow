"""The policy corpus read as DOCUMENTS, for a person to look at.

`pipeline.py` and `store.py` already write `policy_documents` and
`policy_chunks` on every ingest, and `retrieval.py` reads a chunk's document
row to put a title on a citation. Neither answers the question SPEC section
14's `/admin/policies` page asks: which documents this tenant has, which
versions exist of each, which one is live, and whether a version actually
landed. That is what `read_policy_documents` is for, and it decides nothing.

The read is two statements whatever the corpus size: the
documents, then one grouped `count_chunks_by_document` over exactly the ids
just read. A `SELECT count(*)` per version answers the same numbers and costs
a round trip per row, so the count is pinned rather than the shape --
`test_rag_documents.py::test_the_whole_read_issues_exactly_two_selects`.

Grouping by slug, choosing the title and both orderings are Python's, because
`Repository.list()` takes no ordering argument and this module does not reach
for `session.execute` -- the same division `access/rules.py` settled.

Every read goes through the tenant-scoped repository helpers, so the tenant
filter is the guard in `helpdesk/core/db.py` and this module adds no second
one: another tenant's version of the same document slug is absent rather than
merged into this tenant's row, which is the only genuinely dangerous answer
this read could give
(`::test_the_same_slug_in_two_tenants_never_mixes_titles_counts_or_digests`).

`helpdesk/policy/` is not imported and gains nothing: these rows
are records, not inputs to a decision. Nothing here writes.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.core.repository import Repository, count_chunks_by_document
from helpdesk.shared.models import PolicyDocument


@dataclass(frozen=True, slots=True)
class PolicyVersion:
    """One ingested version of one document. Exactly six fields (criterion 3).

    A frozen dataclass around the row and its chunk count, the shape
    `SodRuleRole` already uses: the wire model is the tool's, and a second
    Pydantic model here would be two contracts to keep in step. The exact
    field set is pinned by
    `test_rag_documents.py::test_the_corpus_comes_back_one_row_per_slug_with_the_exact_two_field_sets`.

    `source_path` is corpus-relative and never absolute (plan decision G), so
    nothing about the ingest host crosses; `sha256` is the only evidence on
    this surface that two versions differ at all.
    """

    version: str
    is_current: bool
    ingested_at: datetime
    chunk_count: int
    """How many `policy_chunks` rows this version has (decision B). Zero is a
    value, never a reason to drop the row: an ingest that registered a
    document and wrote no span is invisible any other way, pinned by
    `::test_each_versions_chunk_count_is_exact_and_a_version_with_no_chunk_is_kept`."""

    source_path: str
    sha256: str


@dataclass(frozen=True, slots=True)
class PolicyDocumentDetail:
    """One document SLUG and every version of it (decision A).

    Grouped here rather than in the page, so 24h and every later caller read
    one shape instead of regrouping a flat list in the browser on every paint.
    Exactly three fields, pinned by
    `test_rag_documents.py::test_the_corpus_comes_back_one_row_per_slug_with_the_exact_two_field_sets`.
    """

    slug: str
    title: str
    """The CURRENT version's title where one exists, and the newest-ingested
    version's otherwise (criterion 4): a re-titled document reads as its live
    title, never as its first. `is_current` is per (tenant, slug), so a
    partially re-ingested document legitimately has none, and both rules are
    pinned separately --
    `::test_the_title_is_the_current_versions_even_when_another_version_is_newer`
    and `::test_a_document_with_no_current_version_appears_with_every_flag_false`."""

    versions: tuple[PolicyVersion, ...]


async def read_policy_documents(session: AsyncSession) -> tuple[PolicyDocumentDetail, ...]:
    """The bound tenant's policy corpus, grouped by slug, in one total order.

    Documents are ordered by `slug` ascending and versions by `ingested_at`
    descending, tiebroken by `version` descending (decision C, the direction
    recorded in the plan's Deviations). Never by the `version` string alone,
    which sorts `v10` before `v2` and would put the tenth ingest in the middle
    of the list; `ingested_at` is non-null on every row, so the order is total
    and assertable as an exact sequence
    (`test_rag_documents.py::test_the_documents_are_slug_ascending_and_the_versions_ingested_at_descending`,
    `::test_two_versions_ingested_at_one_instant_are_tiebroken_by_version_descending`).

    A document with no current version is kept, with every flag false: a
    half-finished ingest is exactly the state this page exists to show, and a
    read that hid it would report a corpus as clean
    (`::test_a_document_with_no_current_version_appears_with_every_flag_false`).

    A tenant with no corpus answers an empty tuple, never a raise: nothing is
    looked up by id, and the grouped count runs no statement for an empty id
    set (`::test_a_tenant_with_no_corpus_answers_an_empty_tuple`).

    The session is tenant-bound, so there is no tenant argument to trust (hard
    rule 6): the corpus is the token's tenant's own rows.
    """
    rows = await Repository(session, PolicyDocument).list()
    counts = await count_chunks_by_document(session, [row.id for row in rows])

    by_slug: dict[str, list[PolicyDocument]] = {}
    for row in rows:
        by_slug.setdefault(row.slug, []).append(row)

    details = []
    for slug, planted in by_slug.items():
        ordered = sorted(planted, key=lambda row: (row.ingested_at, row.version), reverse=True)
        current = next((row for row in ordered if row.is_current), None)
        newest = ordered[0]
        details.append(
            PolicyDocumentDetail(
                slug=slug,
                title=current.title if current is not None else newest.title,
                versions=tuple(
                    PolicyVersion(
                        version=row.version,
                        is_current=row.is_current,
                        ingested_at=row.ingested_at,
                        chunk_count=counts.get(row.id, 0),
                        source_path=row.source_path,
                        sha256=row.sha256,
                    )
                    for row in ordered
                ),
            )
        )
    return tuple(sorted(details, key=lambda detail: detail.slug))
