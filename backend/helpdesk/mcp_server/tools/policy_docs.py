"""The policy-document tools: list the corpus, and re-ingest one document.

Re-ingest is not offered to the agent: a model that could re-ingest the
corpus could change the evidence its own next answer cites.
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
    """No fields. Extras are refused, so a stray `tenant_id` gets a clear no."""

    model_config = ConfigDict(extra="forbid")


class PolicyVersionRow(BaseModel):
    """One version. `chunk_count` shows it landed; `source_path` is corpus-relative."""

    version: str
    is_current: bool
    ingested_at: datetime
    chunk_count: int
    source_path: str
    sha256: str


class PolicyDocumentRow(BaseModel):
    """One document slug and every version of it."""

    slug: str
    title: str
    versions: list[PolicyVersionRow]


class ListPolicyDocumentsOutput(BaseModel):
    """The whole corpus, in the helper's order."""

    documents: list[PolicyDocumentRow]


async def list_policy_documents(
    ctx: ToolContext, args: ListPolicyDocumentsInput
) -> ListPolicyDocumentsOutput:
    """This tenant's policy documents, their versions, and which one is live."""
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
"""Its own event, because every access decision cites the corpus."""


class ReingestPolicyDocumentInput(MutatingToolInput):
    """No path field, so a caller can never name a file. The tenant comes from the token."""

    slug: str = Field(description="The document slug, as the corpus file is named.")
    version: str = Field(description="The version directory, spelled `v1`.")


class ReingestPolicyDocumentOutput(BaseModel):
    """What the re-ingest did. An unchanged source gives `created=false` and 0 chunks."""

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

    Run right away rather than queued, so the answer can carry `chunk_count`,
    the proof the ingest landed.
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
    """Two wordings, because "re-ingested, 0 chunks" would read like a failure."""
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
