"""The policy-document tools: read the rules, list the corpus, re-ingest one document.

Reading is open to every signed-in person: it is the company's own policy,
shown to its own people, in the same redacted text the agent cites. The
version list and the re-ingest stay with the admins.

Re-ingest is not offered to the agent: a model that could re-ingest the
corpus could change the evidence its own next answer cites.
"""

import re
from collections.abc import Sequence
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from helpdesk.mcp_server.services.rag.documents import read_policy_documents
from helpdesk.mcp_server.services.rag.pipeline import ingest_one_document
from helpdesk.mcp_server.tools.base import MutatingToolInput
from helpdesk.mcp_server.tools.context import ToolContext
from helpdesk.mcp_server.tools.policy_search import UnknownTenantError
from helpdesk.shared.core.errors import NotFoundError
from helpdesk.shared.core.naming import SLUG_LENGTH, SLUG_PATTERN
from helpdesk.shared.crud.rag import fetch_current_documents, fetch_document_chunks
from helpdesk.shared.crud.tenants import find_tenant_by_id
from helpdesk.shared.models import PolicyChunk


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


class ReadPoliciesInput(BaseModel):
    """One optional slug. Extras are refused, so a stray `tenant_id` gets a clear no."""

    model_config = ConfigDict(extra="forbid")

    slug: str | None = Field(
        default=None,
        max_length=SLUG_LENGTH,
        description="The document to read in full. Leave it out to get the list only.",
    )

    @field_validator("slug")
    @classmethod
    def _slug_is_one_spelling(cls, value: str | None) -> str | None:
        """`fullmatch`, not `pattern=`, which would let a trailing newline through."""
        if value is not None and SLUG_PATTERN.fullmatch(value) is None:
            raise ValueError("slug must be lowercase letters and digits joined by single dashes")
        return value


class PolicyListRow(BaseModel):
    """One document's live version, for the list."""

    slug: str
    title: str
    version: str


class PolicySection(BaseModel):
    """One heading's text. `heading_path` is outermost first; the heading line is not in `text`."""

    heading_path: list[str]
    text: str


class PolicyText(BaseModel):
    """One document's live version, whole, section by section in document order."""

    slug: str
    title: str
    version: str
    sections: list[PolicySection]


class ReadPoliciesOutput(BaseModel):
    """The list of live documents, and the one asked for, or null when none was."""

    documents: list[PolicyListRow]
    document: PolicyText | None


_HEADING_LINE = re.compile(r"[ \t]*#{1,6}[ \t]+\S[^\n]*\n?")
"""A markdown heading line at the very start of a section's text."""


def stitch_sections(chunks: Sequence[PolicyChunk]) -> list[PolicySection]:
    """Join chunks, already in cut order, back into one text per heading.

    A chunk repeats the last sentence of the one before it, so a chunk that
    starts before the last one ended adds only the part after that end. The
    offsets index the whole redacted document, so they are compared directly.
    The heading line is dropped from the text, since the page draws it from
    `heading_path`. Two headings with the same path are told apart by the
    heading line a section's first chunk starts with.
    """
    sections: list[PolicySection] = []
    path: list[str] | None = None
    parts: list[str] = []
    end = 0

    def close() -> None:
        if path is None:
            return
        text = "".join(parts)
        heading = _HEADING_LINE.match(text) if path else None
        if heading is not None:
            text = text[heading.end() :]
        text = text.strip()
        if text:
            sections.append(PolicySection(heading_path=path, text=text))

    for chunk in chunks:
        repeated_heading = (
            bool(parts) and chunk.char_start >= end and _HEADING_LINE.match(chunk.text) is not None
        )
        if list(chunk.heading_path) != path or repeated_heading:
            close()
            path, parts, end = list(chunk.heading_path), [], chunk.char_start
        if chunk.char_end <= end and parts:
            continue
        parts.append(chunk.text[max(0, end - chunk.char_start) :])
        end = chunk.char_end
    close()
    return sections


async def read_policies(ctx: ToolContext, args: ReadPoliciesInput) -> ReadPoliciesOutput:
    """This tenant's live policy documents, and one of them in full when asked.

    An unknown slug, or one with no live version, is `NOT_FOUND`, the same
    answer another tenant's slug gets.
    """
    current = await fetch_current_documents(ctx.session)
    document: PolicyText | None = None
    if args.slug is not None:
        chosen = next((row for row in current if row.slug == args.slug), None)
        if chosen is None:
            raise NotFoundError("no live policy document with that slug")
        document = PolicyText(
            slug=chosen.slug,
            title=chosen.title,
            version=chosen.version,
            sections=stitch_sections(await fetch_document_chunks(ctx.session, chosen.id)),
        )
    return ReadPoliciesOutput(
        documents=[
            PolicyListRow(slug=row.slug, title=row.title, version=row.version) for row in current
        ],
        document=document,
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
    "PolicyListRow",
    "PolicySection",
    "PolicyText",
    "PolicyVersionRow",
    "ReadPoliciesInput",
    "ReadPoliciesOutput",
    "ReingestPolicyDocumentInput",
    "ReingestPolicyDocumentOutput",
    "list_policy_documents",
    "read_policies",
    "reingest_policy_document",
    "stitch_sections",
]
