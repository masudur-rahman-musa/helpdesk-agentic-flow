"""GET /admin/rules: this tenant's separation-of-duties rules.

`list_sod_rules` is an earlier change's read and nothing in the browser could reach it.
This module is the wire and nothing else: the call crosses the MCP boundary, so
RBAC, the kill switch and the audit event apply to an admin reading the board
from a page exactly as they do to one reading it in a turn (23a decision A,
unchanged). Everything here is a READ, so a project rule has nothing to bite on.

**Identity comes from the token and from nowhere else**. The
caller's own bearer is forwarded verbatim by `deps.call_tool_route`, and this
route takes no query parameter, no body and no argument of any kind, so there
is nothing here for a caller to trust: the tenant whose board is read is the
token's, decided behind the MCP boundary. Pinned by
`test_rules_routes.py::test_the_board_route_sends_an_exactly_empty_argument_set`,
which asserts the arguments as the exact dict, so a field riding along fails
the case too.

**A new module rather than a route in `admin_routes`** (decision B). That
module predates `deps` and carries its own `_bearer`, `_claims` and
`_is_permitted`; folding this in would mean either a second guard shape inside
one module or re-guarding an earlier change's verify route in a diff no criterion here
asks for. `audit_routes` is the audit log's surface and this is a different
resource on a different page. The duplication stays named rather than half-paid
(23a Deviations).

**The body is the tool's structured output, unchanged**, for the same reason
the `/me` reads and `/admin/audit/search` are: a route that reshaped on the way
out would be a second contract for 24f's page to track, and 24ea's order and
its nullable blocks are already the shape a design note fixed. The rows cross in the
tool's own sequence, unfiltered and unreordered, which
`::test_the_rows_cross_in_the_tools_own_order` asserts as the exact list of
`rule_id`s and `::test_an_unresolvable_side_crosses_as_null_with_its_id_intact`
as the nulls and the ids that survive beside them.

No new row in `deps.STATUS_FOR_CODE`: its five are 24cb's and this route
answers with `FORBIDDEN` as 403 and `TOOL_DISABLED` as 503 (pinned by
`::test_a_tool_refusal_answers_its_own_typed_code`), while `VALIDATION`,
`NOT_FOUND` and `POLICY_BLOCKED` are unreachable from a route with no input at
all.

No lifespan resource is touched: this route reads nothing off `app.state`,
which is why its tests enter no lifespan and open no socket.
"""

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse

from helpdesk.agent_api.schemas.rules import ListSodRulesArgs
from helpdesk.agent_api.services.tool_proxy import call_tool_route

router = APIRouter()

RULES_PATH = "/admin/rules"
"""SPEC section 14's own route name, identical upstream and browser-side
(decision C): one path is one thing to keep in step. The rules a tenant has are
its SoD rules and there is no second kind for the name to disambiguate."""

LIST_SOD_RULES_TOOL = "list_sod_rules"


@router.get(RULES_PATH)
async def sod_rules_board(authorization: str | None = Header(default=None)) -> JSONResponse:
    """Every separation-of-duties rule this tenant has, resolved to names.

    Which two roles conflict, in words, and why: readable as a DECISION since
    an earlier change and readable as RULES nowhere until 24ea's tool. Both role ids stay
    top-level on each row and a side whose role cannot be read crosses as
    `null` (24ea decision B), so an admin can still act on the rule rather than
    never seeing it.

    Nothing is decided here. Whether this caller may read the board at all is
    SPEC section 7's matrix, behind the MCP boundary, and an empty board is a
    200 carrying an empty list — never a 404
    (`::test_a_tenant_with_no_rules_is_a_200_and_an_empty_list`).
    """
    return await call_tool_route(authorization, LIST_SOD_RULES_TOOL, ListSodRulesArgs())
