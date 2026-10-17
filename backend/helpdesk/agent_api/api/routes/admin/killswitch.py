"""GET and POST /admin/killswitch: the board and the toggle.

`list_kill_switches` is an earlier change's read and `set_kill_switch` is an earlier change's
write, and nothing in the browser could reach either. This module is the wire
and nothing else: both calls cross the MCP boundary, so RBAC, the kill switch
itself, the protected-tool guard and the audit event apply to an admin acting
from a page exactly as they do to one acting in a turn (23a decision A,
unchanged).

Unlike every wire plan before it this one carries a MUTATION, and a project rule
holds without an exception: the mutation is `set_kill_switch`, a typed MCP
tool behind RBAC, and this route only forwards the caller's own bearer to it.
The LLM is not in the path at all - the tool is deliberately absent from the
agent's catalogue (13c and 24ca decision F), so the only caller is a person
clicking a toggle.

**Identity comes from the token and from nowhere else**. The
bearer is forwarded verbatim by `deps.call_tool_route`, the board sends no
argument at all, and the toggle's tenant is read off the VERIFIED claims and
never off the body - which is what `SCOPE_TENANT` below is for. Pinned by
`test_killswitch_routes.py::test_the_board_route_sends_an_exactly_empty_argument_set`
and
`::test_the_set_route_substitutes_the_tokens_own_tenant_for_the_literal_tenant`,
which assert the arguments as exact key sets, so a field riding along fails
the case too.

**A new module rather than a route in `admin_routes`** (decision B). That
module predates `deps` and carries its own `_bearer`, `_claims` and
`_is_permitted`; folding these in would mean either a second guard shape
inside one module or re-guarding an earlier change's verify route in a diff no criterion
here asks for. The duplication stays named rather than half-paid (24a's own
Deviations).

**The body is validated HERE and refused typed.** Both routes' refusals are
SPEC section 8.1 codes dressed by the shared `deps.STATUS_FOR_CODE`, so the
body model is applied by hand rather than declared as a FastAPI parameter: a
declared model answers the framework's untyped 422, carrying a `detail`/`loc`
shape 24d has no sentence for. The two bounds inside it are the TOOL's own
constants, imported from `helpdesk.killswitch` and never retyped (standing
rules 1 and 6), so the only thing this router decides for itself is the
two-member `scope` enum. Pinned at both ends of each bound by
`::test_a_tool_name_of_exactly_sixty_four_characters_is_accepted`,
`::test_a_tool_name_one_character_over_the_cap_is_a_typed_400_with_no_call`,
`::test_the_inclusive_ends_of_the_reason_bound_are_accepted`,
`::test_a_reason_outside_the_bound_is_a_typed_400_with_no_call` and, for the
enum, `::test_a_scope_outside_the_two_literals_is_a_typed_400_and_opens_no_session`.

**The body of a response is the tool's structured output, unchanged**, for the
same reason the `/me` reads are: a route that reshaped on the way out would be
a second contract for 24d's page to track, and `stopped`'s polarity would stop
being the tool's to decide.

The idempotency key is derived here and never accepted from the browser (ADR
0032): `set_kill_switch` is a mutating tool like the rest, so the key is made
the way the approval and review routes make theirs, from the verified caller
and the toggle itself (`helpdesk.agent_api.agent.keys.derive_kill_switch_key`).

No lifespan resource is touched: neither route reads anything off `app.state`,
which is why their tests enter no lifespan and open no socket.
"""

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request
from pydantic import ValidationError

from helpdesk.agent_api.agent.keys import derive_kill_switch_key
from helpdesk.agent_api.core.deps import InvalidBodyError, request_claims
from helpdesk.agent_api.schemas.killswitch import (
    ListKillSwitchesArgs,
    SetKillSwitchArgs,
    SetKillSwitchBody,
)
from helpdesk.agent_api.services.tool_proxy import proxy_tool

router = APIRouter()

KILLSWITCH_PATH = "/admin/killswitch"
"""One path for both verbs (decision E): the board and the toggle are one
resource and one page (SPEC section 14's own route name), so one proxy file
exporting two verbs is one bearer seam to keep in step instead of two."""

LIST_KILL_SWITCHES_TOOL = "list_kill_switches"
SET_KILL_SWITCH_TOOL = "set_kill_switch"

SCOPE_GLOBAL = "global"
"""The literal the tool itself reads as the platform-wide layer. Sent on as it
arrived: translating it to the caller's tenant would leave every other tenant
running a tool somebody decided to stop (section 5's second risk)."""

SCOPE_TENANT = "tenant"
"""The browser's word for "my own tenant", and the only scope this router
rewrites (decision C).

a project rule read strictly: the tenant is derived from the JWT, so a tenant
identifier never travels client-ward at all and the page needs no way to learn
one in order to trip a switch. The uuid form would be safe - the tool refuses
a foreign tenant `FORBIDDEN` - but it would give a future page an identifier
to leak. The substitution is asserted against the uuid read off the fixture's
own token by
`test_killswitch_routes.py::test_the_set_route_substitutes_the_tokens_own_tenant_for_the_literal_tenant`,
so a router substituting a constant fails it."""


@router.get(KILLSWITCH_PATH)
async def kill_switch_board(request: Request) -> Mapping[str, Any]:
    """Every registered tool and the state of its two kill switches.

    Both layers per row rather than one effective state (24ca decision B), and
    the tool's output is handed back unchanged: an admin clearing the tenant
    switch has to be told the global one still holds the tool down, and a route
    that merged the two would leave the page unable to say so.

    No argument of any kind, so there is nothing here for a caller to trust.
    Which board this caller reads is the token's tenant, decided behind the MCP
    boundary.
    """
    return await proxy_tool(request, LIST_KILL_SWITCHES_TOOL, ListKillSwitchesArgs())


@router.post(KILLSWITCH_PATH)
async def set_kill_switch_route(request: Request) -> Mapping[str, Any]:
    """Stop one tool, or let it run again, for this tenant or the whole platform.

    `enabled` describes the SWITCH and `stopped` in the answer describes the
    TOOL (13c decision B). Neither is rewritten here: the polarity is the
    tool's, echoed back as it came, so 24d's toggle inherits it as an asserted
    fact rather than as a docstring's word
    (`::test_the_set_route_answers_the_polarity_the_tool_decided`).

    The tenant the `"tenant"` scope is substituted with is read off the
    verified claims on `request.state`, which `RequireAuthMiddleware`
    put there after the one verify every request gets; the browser never names a
    tenant (a project rule,
    `::test_the_set_route_substitutes_the_tokens_own_tenant_for_the_literal_tenant`).

    The idempotency key is derived from the verified admin and the toggle, so
    a double click replays the stored answer and the browser never names a key
    (`::test_the_set_route_derives_the_key_and_the_body_still_forbids_one`).

    The body is read and validated before any session is opened, and a refusal
    is the typed `VALIDATION` dressed as a 400 rather than FastAPI's untyped
    422. An unparseable body is the same refusal: a request whose JSON does not
    decode carries no `tool_name` and no `reason` either.
    """
    claims = request_claims(request)

    try:
        payload = await request.json()
        body = SetKillSwitchBody.model_validate(payload)
    except (ValueError, ValidationError):
        raise InvalidBodyError("the kill-switch body did not validate") from None

    scope = SCOPE_GLOBAL if body.scope == SCOPE_GLOBAL else claims.tenant_id
    arguments = SetKillSwitchArgs(
        tool_name=body.tool_name,
        enabled=body.enabled,
        scope=scope,
        reason=body.reason,
        idempotency_key=derive_kill_switch_key(
            admin_user_id=uuid.UUID(claims.sub),
            tool_name=body.tool_name,
            enabled=body.enabled,
            scope=scope,
            reason=body.reason,
            minute=datetime.now(UTC),
        ),
    )
    return await proxy_tool(request, SET_KILL_SWITCH_TOOL, arguments)
