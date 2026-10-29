"""GET /policies: the company's access rules, for every signed-in person.

Calls the `read_policies` tool with the caller's own token, so access checks,
the kill switch and audit apply. The tenant comes from the token, never from
the request. The tool's output is returned unchanged.
"""

from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Query, Request

from helpdesk.agent_api.schemas.policies import ReadPoliciesArgs
from helpdesk.agent_api.services.tool_proxy import proxy_tool

router = APIRouter()

READ_POLICIES_PATH = "/policies"
READ_POLICIES_TOOL = "read_policies"


@router.get(READ_POLICIES_PATH)
async def read_policies_route(
    request: Request, slug: str | None = Query(default=None)
) -> Mapping[str, Any]:
    """The live documents, and the one named by `slug` in full when given."""
    return await proxy_tool(request, READ_POLICIES_TOOL, ReadPoliciesArgs(slug=slug))
