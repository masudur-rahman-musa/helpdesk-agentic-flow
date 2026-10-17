"""GET /admin/rules: this tenant's separation-of-duties rules.

Calls the `list_sod_rules` MCP tool with the caller's own token, so access
checks, the kill switch and audit apply. No argument at all; the tenant comes
from the token. The tool's output is returned unchanged.
"""

from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Request

from helpdesk.agent_api.schemas.rules import ListSodRulesArgs as ListSodRulesArgs
from helpdesk.agent_api.services.tool_proxy import proxy_tool

router = APIRouter()

RULES_PATH = "/admin/rules"
LIST_SOD_RULES_TOOL = "list_sod_rules"


@router.get(RULES_PATH)
async def sod_rules_board(request: Request) -> Mapping[str, Any]:
    """Every separation-of-duties rule this tenant has, resolved to names.

    A side whose role cannot be read comes back as `null` with its id kept, so
    the admin still sees the rule.
    """
    return await proxy_tool(request, LIST_SOD_RULES_TOOL, ListSodRulesArgs())
