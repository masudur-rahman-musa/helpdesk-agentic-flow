"""Request argument models for the /admin/policies routes."""

from pydantic import BaseModel, ConfigDict, Field


class ListPolicyDocumentsArgs(BaseModel):
    """No arguments: the documents listed are the token tenant's own."""


class ReingestPolicyDocumentArgs(BaseModel):
    """The three keys the re-ingest tool takes.

    Extra fields are refused here so a browser-sent `tenant_id` never travels
    on; the tenant comes from the token. The key is forwarded, never made here,
    so a retry replays instead of ingesting twice.
    """

    model_config = ConfigDict(extra="forbid")

    slug: str = Field(min_length=1)
    version: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
