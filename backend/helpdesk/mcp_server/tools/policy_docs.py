"""The policy-document tools: the design's Admin group, the corpus read and re-ingest.

`list_policy_documents` is the read the design's `/admin/policies`
page is drawn from: which documents this tenant's corpus holds, which versions
exist of each, which one is live and whether a version actually landed.
Everything it decides lives in
`helpdesk.mcp_server.services.rag.documents.read_policy_documents` -- the two statements, the
grouping by slug, the title rule and both orders -- and what lives here is the
wire contract alone. It takes no arguments, and its two cells are `it_admin`
and `platform_admin`, the design's audience for that page (a design note, cited
rather than re-opened).

`reingest_policy_document` is the WRITE half, and what the design
means by calling ingestion "also an admin API": one corpus document, named by
its slug and version, re-read and made live. Everything it decides lives in
`helpdesk.mcp_server.services.rag.pipeline.ingest_one_document` -- the corpus path, the
`source_path` string, both name checks and the eight-effect order -- and what
lives here is the wire contract, the tenant slug read back off the token, and
one clock read. Its two cells are `list_policy_documents`'s, and it is absent
from the agent's catalogue (decision G): a model that can re-ingest a corpus
can change the evidence its own next answer cites, so the only caller is a
person on `/admin/policies`, exactly as `set_kill_switch` is.

No function here opens a session or touches the transport: the pipeline binds
the session to the token's tenant before the handler is called.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from helpdesk.mcp_server.services.rag.documents import read_policy_documents
from helpdesk.mcp_server.services.rag.pipeline import ingest_one_document
from helpdesk.mcp_server.tools.base import MutatingToolInput
from helpdesk.mcp_server.tools.context import ToolContext
from helpdesk.mcp_server.tools.policy_search import UnknownTenantError
from helpdesk.shared.crud.tenants import find_tenant_by_id


class ListPolicyDocumentsInput(BaseModel):
    """No fields at all, and nothing for a caller to choose.

    The tenant is the token's and the corpus is that tenant's own rows (hard
    rule 6), so there is no argument to trust and none to validate.
    `extra="forbid"` rather than `ignore`, exactly as `ListSodRulesInput`: a
    caller who sends a `tenant_id` believing they are reading another tenant's
    corpus must be refused, not quietly answered with their own, and a `slug`
    or a `version` asking the board to narrow itself is refused for the same
    reason. Pinned as an EXACTLY empty field set by
    `test_mcp_list_policy_documents.py::test_the_tool_declares_no_field_at_all`,
    which no later field can ride along into.
    """

    model_config = ConfigDict(extra="forbid")


class PolicyVersionRow(BaseModel):
    """One ingested version as 24g puts it on the wire. Exactly six fields.

    `chunk_count` is the only thing on this surface that separates a version
    that ingested from one that registered a document and wrote no span, and
    `sha256` the only evidence that two versions differ at all (plan decisions
    B and G). `source_path` is corpus-relative and never absolute, so nothing
    about the ingest host crosses. Pinned by
    `test_mcp_list_policy_documents.py::test_the_output_models_carry_exactly_the_planned_field_sets`.
    """

    version: str
    is_current: bool
    ingested_at: datetime
    chunk_count: int
    source_path: str
    sha256: str


class PolicyDocumentRow(BaseModel):
    """One document slug and every version of it. Exactly three fields.

    Grouped rather than flat (plan decision A): the page's question is which
    documents there are and which version of each is live, and a flat list
    would be regrouped in the browser on every paint. `title` is the current
    version's where one exists and the newest-ingested version's otherwise,
    which `read_policy_documents` decides.
    """

    slug: str
    title: str
    versions: list[PolicyVersionRow]


class ListPolicyDocumentsOutput(BaseModel):
    """The whole corpus, in the helper's order."""

    documents: list[PolicyDocumentRow]


async def list_policy_documents(
    ctx: ToolContext, args: ListPolicyDocumentsInput
) -> ListPolicyDocumentsOutput:
    """This tenant's policy documents, their versions, and which one is live.

    What the design's `/admin/policies` page is drawn from. The rows have been
    written since an earlier change and readable as a CORPUS nowhere: `search_policy`
    answers the spans an answer may cite, and nothing said which documents a
    tenant has, which versions exist of each or whether a version landed with
    no chunk at all.

    Transport only. The two statements, the grouping by slug, the title rule
    and both orders are `helpdesk.mcp_server.services.rag.documents.read_policy_documents`'s, so
    24h's page and every later caller read one sequence rather than a second
    copy inside a handler; the mapping below adds nothing but the wire's
    names.

    No scope helper and no second permission check: the design gives the row two
    cells, both `Scope.ANY`, so the pipeline's matrix stage IS the whole check
    and deleting it hands the corpus to the five principals with no cell,
    pinned by
    `test_mcp_list_policy_documents.py::test_the_five_denied_principals_are_refused_whatever_the_corpus_holds`
    (a project rule: there is no second copy of that guard to hide behind).
    That case runs with a corpus and without one, which is criterion 9's other
    half: the refusal is the same either way, so the tool is not an existence
    oracle for a tenant's corpus.

    Nothing is written, so the pipeline's generic `tool.succeeded` is the whole
    record a read leaves. The session is the pipeline's, already bound to the
    token's tenant.
    """
    details = await read_policy_documents(ctx.session)
    return ListPolicyDocumentsOutput(
        documents=[
            PolicyDocumentRow(
                slug=detail.slug,
                title=detail.title,
                versions=[
                    PolicyVersionRow(
                        version=version.version,
                        is_current=version.is_current,
                        ingested_at=version.ingested_at,
                        chunk_count=version.chunk_count,
                        source_path=version.source_path,
                        sha256=version.sha256,
                    )
                    for version in detail.versions
                ],
            )
            for detail in details
        ]
    )


REINGESTED_EVENT_TYPE = "policy.reingested"
"""The re-ingest's own success event.

The corpus is what every access decision cites, so a change to it is a
governance record in its own right rather than a generic `tool.succeeded`
naming the tool that happened to run. Named here beside the handler, the way
`access.CLASSIFIED_EVENT_TYPE` is, so the registry and the audit assertion
read one string. Pinned by
`test_mcp_reingest_policy_document.py::test_the_audit_event_type_is_policy_reingested`,
which asserts the type and its subject against a before snapshot."""


class ReingestPolicyDocumentInput(MutatingToolInput):
    """Which corpus document to make live. Exactly three fields.

    `slug` and `version` address ONE file; the tenant is the token's and is
    resolved through `find_tenant_by_id`, so a `tenant_id`
    riding along is refused rather than quietly ignored. `source_path` is not
    a field either: where a corpus file lives is `rag.pipeline`'s (decision
    H), and accepting it here would let a caller name a path.

    `idempotency_key` and `extra="forbid"` both come from
    `MutatingToolInput`, which is what makes the key required and the field
    set exact. Pinned by
    `test_mcp_reingest_policy_document.py::test_the_input_declares_exactly_slug_version_and_idempotency_key`,
    which sends an own-tenant `tenant_id` and asserts `VALIDATION` with
    nothing written.

    Neither field declares its bound here: `ingest_one_document` runs
    `check_slug` and `check_version` before the path is composed, so the caps
    have one home and one error and a refused name opens no file
    (`test_rag_pipeline.py::test_a_traversing_slug_is_refused_before_the_filesystem_is_touched`).
    """

    slug: str = Field(description="The document slug, as the corpus file is named.")
    version: str = Field(description="The version directory, spelled `v1`.")


class ReingestPolicyDocumentOutput(BaseModel):
    """What the re-ingest did. Exactly eight fields.

    `created` and `chunk_count` are the pair that separates a source that
    changed from one that did not: an unchanged source answers `false` and
    `0` because nothing was written, never because the document has no
    chunks. `is_current` says which version is live afterwards, which a
    re-ingest of an OLDER version moves (decision E). `sha256` is the only
    evidence on this surface that two versions differ at all.

    `summary` is a FIELD beside the values and never the whole response
    (the design), so the page renders the numbers and a person reads the
    sentence. Pinned by
    `test_mcp_reingest_policy_document.py::test_the_output_carries_all_seven_fields_and_a_summary`.
    """

    slug: str
    version: str
    created: bool
    chunk_count: int
    is_current: bool
    sha256: str
    ingested_at: datetime
    summary: str


async def reingest_policy_document(
    ctx: ToolContext, args: ReingestPolicyDocumentInput
) -> ReingestPolicyDocumentOutput:
    """Re-read one corpus file and make it live, in the caller's transaction.

    The write half of `list_policy_documents`, and what the design means by
    "also an admin API": until this existed, an edited policy file could only
    be made live from a shell on the ingest host, so the corpus every access
    decision cites drifted from the repository that holds it.

    Transport only. Where the file lives, what its `source_path` string is,
    both name checks and the whole eight-effect order are
    `helpdesk.mcp_server.services.rag.pipeline`'s (decision H), so this handler holds no path and
    mcp-server owns no filesystem convention.

    The tenant SLUG is read back from the token's tenant id, exactly as
    `search_policy` does it and never from an argument; a token
    whose tenant no longer exists is that tool's `UnknownTenantError`.

    Synchronous rather than queued (decision B): a queued answer could carry
    no `chunk_count`, which is the one field that says the ingest actually
    landed. The gateway client and the embedder are read off `ctx.clients`,
    whose accessors keep both pooled for the loop rather than built per call
    (decision F; the pooling is `rag_clients`', wrapped by the context).

    No scope helper and no second permission check: the design gives the row two
    cells, both `Scope.ANY`, so the pipeline's matrix stage IS the whole check
    and deleting it hands the corpus to the five principals with no cell,
    pinned by
    `test_mcp_reingest_policy_document.py::test_the_five_denied_principals_re_ingest_nothing_whatever_is_on_disk`. That case runs with the corpus file present and
    absent, so the refusal is the same either way and the tool is not an
    existence oracle for a tenant's corpus.

    The registry declares `policy.reingested` (decision D), so a change to the
    evidence every decision cites is its own governance record rather than a
    generic `tool.succeeded`; the replay of a spent key writes `tool.replayed`
    and re-runs nothing
    (`::test_a_replayed_call_is_byte_identical_and_ingests_once`).
    """
    tenant = await find_tenant_by_id(ctx.session, ctx.identity.tenant_id)
    if tenant is None:
        raise UnknownTenantError("the token's tenant no longer exists")

    result = await ingest_one_document(
        ctx.session,
        ctx.clients.qdrant(),
        corpus_root=ctx.settings.policy_dir,
        tenant_slug=tenant.slug,
        slug=args.slug,
        version=args.version,
        redactor=ctx.clients.redactor(),
        embedder=ctx.clients.embedder(),
        ingested_at=ctx.now,
    )
    document = result.document
    return ReingestPolicyDocumentOutput(
        slug=document.slug,
        version=document.version,
        created=result.created,
        chunk_count=result.chunk_count,
        is_current=document.is_current,
        sha256=str(document.sha256),
        ingested_at=document.ingested_at,
        summary=_reingest_summary(
            document.slug, document.version, created=result.created, chunk_count=result.chunk_count
        ),
    )


def _reingest_summary(slug: str, version: str, *, created: bool, chunk_count: int) -> str:
    """The sentence beside the values, in a person's words.

    Two shapes rather than one with a zero in it: "re-ingested, 0 chunks"
    reads like a failed ingest, and the case an admin most needs to tell
    apart is a file they edited from one they did not.
    """
    if not created:
        return f"{slug} {version} is unchanged; nothing was re-ingested."
    return f"{slug} {version} was re-ingested as {chunk_count} chunks and is now live."


__all__ = [
    "REINGESTED_EVENT_TYPE",
    "ListPolicyDocumentsInput",
    "ListPolicyDocumentsOutput",
    "PolicyDocumentRow",
    "PolicyVersionRow",
    "ReingestPolicyDocumentInput",
    "ReingestPolicyDocumentOutput",
    "list_policy_documents",
    "reingest_policy_document",
]
