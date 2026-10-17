"""/admin/policies: list this tenant's policy documents, and re-ingest one.

Both call MCP tools with the caller's own token, so access checks, the kill
switch and audit apply. Neither route names a tenant; it comes from the token.
The tool's output is returned unchanged.
"""

from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Request
from pydantic import ValidationError

from helpdesk.agent_api.core.deps import InvalidBodyError
from helpdesk.agent_api.schemas.policies import (
    ListPolicyDocumentsArgs as ListPolicyDocumentsArgs,
)
from helpdesk.agent_api.schemas.policies import (
    ReingestPolicyDocumentArgs as ReingestPolicyDocumentArgs,
)
from helpdesk.agent_api.services.tool_proxy import proxy_tool

router = APIRouter()

POLICIES_PATH = "/admin/policies"
LIST_POLICY_DOCUMENTS_TOOL = "list_policy_documents"

REINGEST_PATH = "/admin/policies/reingest"
"""Its own path, so a read and a write of different things never share one name."""

REINGEST_POLICY_DOCUMENT_TOOL = "reingest_policy_document"


@router.get(POLICIES_PATH)
async def policy_documents_board(request: Request) -> Mapping[str, Any]:
    """Every policy document this tenant has, with each ingested version.

    Versions with no chunks, and documents with no live version, are shown on
    purpose: a half-finished ingest is what this page is for.
    """
    return await proxy_tool(request, LIST_POLICY_DOCUMENTS_TOOL, ListPolicyDocumentsArgs())


@router.post(REINGEST_PATH)
async def reingest_policy_document_route(request: Request) -> Mapping[str, Any]:
    """Re-read one policy document and make it live.

    The body is checked here by hand so a bad one gets a typed 400, not FastAPI's
    untyped 422. An unchanged source answers 200 with `created=false`.
    """
    try:
        arguments = ReingestPolicyDocumentArgs.model_validate(await request.json())
    except (ValueError, ValidationError):
        raise InvalidBodyError("the re-ingest body did not validate") from None

    return await proxy_tool(request, REINGEST_POLICY_DOCUMENT_TOOL, arguments)
