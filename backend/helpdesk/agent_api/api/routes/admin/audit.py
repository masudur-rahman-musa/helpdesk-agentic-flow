"""The admin audit routes: chain check and log search.

The chain check walks the token's own tenant, never one named in a parameter.
Search calls the `search_audit` MCP tool with the caller's token, so access
checks, the kill switch and per-row scope apply as they do in a chat. Every
filter is passed as sent and the tool alone checks it, so a bad value gets a
typed 400 rather than FastAPI's untyped 422. That is also why the two dates
are plain strings here.
"""

from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Query, Request

from helpdesk.agent_api.core.deps import request_claims
from helpdesk.agent_api.schemas.audit import SearchAuditArgs
from helpdesk.agent_api.services import audit as services_audit
from helpdesk.agent_api.services.tool_proxy import proxy_tool
from helpdesk.shared.core.config import load_settings
from helpdesk.shared.audit.chain import VerifyResult

router = APIRouter()

SEARCH_AUDIT_TOOL = "search_audit"


@router.get("/admin/audit/verify", response_model=VerifyResult)
async def verify_audit_chain(request: Request) -> VerifyResult:
    """Is this tenant's audit chain intact? Admins only."""
    claims = request_claims(request)
    return await services_audit.verify_chain(claims, load_settings().database_url)


@router.get("/admin/audit/search")
async def search_audit_events(
    request: Request,
    event_type: str | None = Query(default=None),
    subject_type: str | None = Query(default=None),
    subject_id: str | None = Query(default=None),
    actor_ref: str | None = Query(default=None),
    occurred_from: str | None = Query(default=None),
    occurred_to: str | None = Query(default=None),
    before_seq: int | None = Query(default=None),
    limit: int | None = Query(default=None),
) -> Mapping[str, Any]:
    """This tenant's audit events, newest first, filtered as asked.

    Each filter is sent only when present. The tool decides which events this
    caller may see.
    """
    asked: dict[str, Any] = {
        "event_type": event_type,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "actor_ref": actor_ref,
        "occurred_from": occurred_from,
        "occurred_to": occurred_to,
        "before_seq": before_seq,
        "limit": limit,
    }
    arguments = SearchAuditArgs.model_validate(
        {name: value for name, value in asked.items() if value is not None}
    )
    return await proxy_tool(request, SEARCH_AUDIT_TOOL, arguments)
