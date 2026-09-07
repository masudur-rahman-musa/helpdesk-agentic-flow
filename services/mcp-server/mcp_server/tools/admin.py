"""The admin tools: the design's Admin group. `rollback_request` is the first, `search_audit` lives with the reads, and
`set_kill_switch` is the third.

`rollback_request` is the design's undo: an executed request, within the
domain's `WINDOW_HOURS` of the grant, taken back in full. Everything it
decides lives in `helpdesk.access.rollback`, refusals included: what lives
here is the wire contract, the tenant-scoped lookup and the single clock read.
`WINDOW_HOURS` is re-exported so the registry's description says the same
number the refusal does.

There is NO scope helper on this tool, unlike `revoke_access` and the review
tools. the design gives the row `it_admin` and `platform_admin` only, both at
`Scope.ANY`, so there is no narrower scope for a helper to compare and the
pipeline's matrix stage IS the whole permission check. That makes the denial
case its own kill: the handler never re-runs
`rbac.authorize`, so deleting the pipeline stage changes the answer for the
five principals the design gives no cell, pinned by
`test_mcp_rollback_request.py::test_the_five_denied_principals_roll_nothing_back`.

Because permission is settled before the handler runs, this tool tests
EXISTENCE first and belongs in `test_mcp_rbac_matrix.py::ALLOW_ANSWERS_NOT_FOUND`
rather than `SCOPE_BEFORE_EXISTENCE_TOOLS`: a permitted
caller sending an id that does not exist must read `NOT_FOUND`.

The registry declares the generic `tool.succeeded`: the handler's own domain
call appends one `grant.revoked` and one `grant.rolled_back` per revoked row
(decision E), each naming the GRANT, and the pipeline's event lands last as
the summary — the same shape as an earlier change's `grant.revoked`.

`set_kill_switch` is the admin half of the design. Everything it DECIDES —
the protected-tool guard and the `killswitch.changed` record that cannot be
skipped on the way to the write — lives in `helpdesk.killswitch.change`, so
row 24's web admin reaches the same guards. What lives here is the wire
contract, the registry lookup that turns a client string into a tool this
platform actually has, the scope check against the token, and one clock read.

It has no scope helper either, for the same reason as `rollback_request` and
more sharply: the design gives the row ONE cell, `platform_admin` at
`Scope.ANY`. The pipeline's matrix stage is the whole permission check, so
deleting it changes the answer for the six principals with no cell — pinned
by `test_mcp_set_kill_switch.py::test_the_six_denied_principals_change_no_switch`.

It reaches Redis through `mcp_server.redis_client`, the same per-loop pooled
client with the same pinned timeouts the pipeline reads the switch with.

No function here opens a session or touches the transport: the pipeline binds
the session to the token's tenant before the handler is called.
"""

import uuid
from datetime import UTC, datetime
from typing import Literal

from helpdesk.access import (
    RequestNotExecutedError as RequestNotExecutedError,  # re-exported for the tests
)
from helpdesk.access import (
    RollbackWindowExpiredError as RollbackWindowExpiredError,  # re-exported for the tests
)
from helpdesk.access import (
    SimulatedDirectory,
    lock_request,
)
from helpdesk.access import (
    rollback_request as rollback,
)
from helpdesk.access.rollback import WINDOW_HOURS as WINDOW_HOURS  # re-exported for the registry
from helpdesk.auth import rbac
from helpdesk.core.settings import load_settings
from helpdesk.errors import NotFoundError
from helpdesk.killswitch import (
    GLOBAL,
    GLOBAL_SEGMENT,
    KillSwitch,
    Scope,
    change_switch,
)
from helpdesk.killswitch import (
    REASON_MAX_LENGTH as SWITCH_REASON_MAX_LENGTH,
)
from helpdesk.killswitch import (
    TOOL_NAME_MAX_LENGTH as TOOL_NAME_MAX_LENGTH,  # re-exported for the tests
)
from helpdesk.killswitch.switch import InvalidSwitchTargetError
from helpdesk.models import RequestStatus
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_server.identity import Identity
from mcp_server.redis_client import _redis
from mcp_server.tools.access import REASON_MAX_LENGTH
from mcp_server.tools.base import MutatingToolInput


class RollbackRequestInput(MutatingToolInput):
    """the design's signature. `extra="forbid"` comes from `MutatingToolInput`.

    No tenant and no user argument: both are the token's. The
    request id is the only thing the caller chooses, and what it produced is
    read off the `grants` rows rather than named.
    """

    request_id: uuid.UUID
    reason: str = Field(min_length=1, max_length=REASON_MAX_LENGTH)
    """Bounded against `revoke_access`'s own constant, IMPORTED rather than
    redeclared: both tools write a caller's sentence about
    ending access, and two spellings of one bound would let a reason that one
    tool accepts be refused by the other. Pre-checked by the boundary before
    any effect runs, inclusively at 1 and 2000, pinned by
    `test_mcp_rollback_request.py::test_the_reason_boundary_is_inclusive_at_1_and_2000`,
    which reads the number off `mcp_server.tools.access.REASON_MAX_LENGTH`."""


class RollbackRequestOutput(BaseModel):
    """What the undo ended, and where the request now stands."""

    request_id: uuid.UUID
    status: RequestStatus
    revoked_grant_ids: list[uuid.UUID]
    """EVERY live row the request produced, not just the first: an earlier change's
    onboarding executes a whole template through one request, so a shorter
    output would misreport how much access was taken back."""

    revoked_count: int


async def rollback_request(
    session: AsyncSession, identity: Identity, args: RollbackRequestInput
) -> RollbackRequestOutput:
    """Undo an executed request within the design's 24 hours.

    This handler owns exactly two things: the lookup and the clock. Every
    refusal about the request itself — its status, the 24h window, whether
    anything live is left — is raised by `helpdesk.access.rollback`, beside
    the writes it guards, so a second caller of that exported function cannot
    get the writes without the guards.

    `NOT_FOUND` — no such request, or one in another tenant. `lock_request`
    runs through the tenant-bound session, so the two are indistinguishable
    and a request id is not an existence oracle
    (`test_mcp_rollback_request.py::test_a_missing_and_another_tenants_request_answer_the_same_not_found`).
    It takes the row's lock at the same time, which is what serialises two
    rollbacks of one request: the loser waits, then re-reads a row the winner
    has already moved to `rolled_back` and is refused `POLICY_BLOCKED` by the
    domain's status test — never `FORBIDDEN`, and never a second revocation.
    Two callers send two idempotency keys, so the guard cannot serialise them.
    `::test_two_callers_racing_the_same_rollback_undo_it_once` pins that
    outcome but does NOT kill the lock (it passes with `with_for_update()`
    deleted, because two client calls rarely overlap); its docstring carries
    the reason.

    The three `POLICY_BLOCKED` refusals and the case that separates each are
    documented on `helpdesk.access.rollback.rollback_request`.

    `now` is read ONCE and passed down, so the window test, the locked
    liveness read and every `revoked_at` answer for one instant. Everything
    runs in the transaction the pipeline opened, so the revoked rows, both
    events per row and the status write commit or roll back together.
    """
    request = await lock_request(session, args.request_id)
    if request is None:
        raise NotFoundError("no such access request in this tenant")

    now = datetime.now(UTC)
    result = await rollback(
        session,
        SimulatedDirectory(session),
        request=request,
        reason=args.reason,
        rolled_back_by_user_id=identity.user_id,
        actor_ref=str(identity.user_id),
        session_id=identity.session_id,
        now=now,
    )
    return RollbackRequestOutput(
        request_id=result.request.id,
        status=RequestStatus(result.request.status),
        revoked_grant_ids=[grant.id for grant in result.revoked_grants],
        revoked_count=len(result.revoked_grants),
    )


class SetKillSwitchInput(BaseModel):
    """the design's signature, with `tenant_id?` read as a REQUIRED `scope`.

    Taking the optional field literally would make a forgotten argument trip
    the platform-wide stop, which is the most dangerous default in the system
    (decision A). So the scope is required and spelled out: the literal
    `"global"` for the platform, or a tenant id — which must be the token's,
    or the call is refused `FORBIDDEN`.

    `enabled` describes the KILL SWITCH, not the tool: `True` stops the tool
    (decision B). `SetKillSwitchOutput.stopped` echoes the outcome and the
    registry description says it in words, so neither an agent nor a person
    can get the polarity backwards from one surface alone.

    Not a `MutatingToolInput`: one SET or DELETE of one key is idempotent by
    nature, and the design omits the key for this tool alone (decision C).
    """

    model_config = ConfigDict(extra="forbid")

    tool_name: str = Field(min_length=1, max_length=TOOL_NAME_MAX_LENGTH)
    """The wire name of the tool to stop or release, bounded against the kill
    switch's OWN constant, imported rather than redeclared:
    a name this model accepted and `key_for` refused would be a `VALIDATION`
    raised after the audit event rather than before it. Checked against the
    registry too, so a name no tool answers to never becomes a Redis key
    (`test_mcp_set_kill_switch.py::test_an_unregistered_tool_name_is_refused_before_any_key_is_written`).
    The 64/65 boundary is pinned by
    `::test_the_tool_name_length_boundary_is_inclusive_at_64`."""

    enabled: bool
    scope: Literal["global"] | uuid.UUID
    reason: str = Field(min_length=1, max_length=SWITCH_REASON_MAX_LENGTH)
    """Required in BOTH directions (decision H). Tripping stores it in the
    `SwitchState` every refused caller sees; clearing has nowhere to store it,
    so the `killswitch.changed` payload is the only record that turning a tool
    back on was a decision somebody made and explained. Bounded by
    `helpdesk.killswitch.REASON_MAX_LENGTH`, pinned inclusively at 1 and 500
    by `::test_the_reason_boundary_is_inclusive_at_1_and_500`."""


class SetKillSwitchOutput(BaseModel):
    """What the switch now says, in the words the caller can act on."""

    tool_name: str
    stopped: bool
    """True when the tool is now OFF. The echo decision B asks for: the input
    names the switch and this names the tool, so the two together cannot be
    read backwards."""

    scope: str
    """`global`, or the tenant id the switch was written for."""

    changed_at: datetime


async def set_kill_switch(
    session: AsyncSession, identity: Identity, args: SetKillSwitchInput
) -> SetKillSwitchOutput:
    """Stop a tool, or let it run again, for one tenant or the whole platform.

    Three checks run here and nowhere else, all before any effect:

    * the tool name is looked up in the REGISTRY, so a switch can only be set
      on a tool this platform actually serves. `VALIDATION`, not `NOT_FOUND`:
      the argument is malformed for this tool, and a name outside the registry
      is not a row a caller could have reached.
    * a `scope` naming a tenant other than the token's is refused `FORBIDDEN`
      and recorded `tool.denied` — `Scope.ANY` in the design means every subject
      in the caller's own tenant, never every tenant.
    * `"global"` is turned into the `GLOBAL` sentinel here, so the string from
      the request body never reaches `key_for` as a scope.

    Everything else — the protected-tool guard, the event, the ordering that
    makes a Redis failure leave no record behind — is
    `helpdesk.killswitch.change_switch`, beside the write it guards.

    The registry import is deferred to the call: `registry` imports this
    module, so a module-level import would be a cycle (decision F).

    `now` is read once and used for the event and the answer alike.
    """
    from mcp_server.registry import REGISTRY

    if args.tool_name not in REGISTRY:
        raise InvalidSwitchTargetError(f"no registered tool named {args.tool_name!r}")

    scope: Scope
    if args.scope == GLOBAL_SEGMENT:
        scope = GLOBAL
    elif args.scope == identity.tenant_id:
        scope = identity.tenant_id
    else:
        raise rbac.RbacDenied("a kill switch may only be scoped globally or to your own tenant")

    now = datetime.now(UTC)
    await change_switch(
        session,
        KillSwitch(_redis(load_settings())),
        scope=scope,
        tool=args.tool_name,
        stopped=args.enabled,
        reason=args.reason,
        actor_ref=str(identity.user_id),
        session_id=identity.session_id,
        now=now,
    )
    return SetKillSwitchOutput(
        tool_name=args.tool_name,
        stopped=args.enabled,
        scope=GLOBAL_SEGMENT if scope is GLOBAL else str(scope),
        changed_at=now,
    )


__all__ = [
    "TOOL_NAME_MAX_LENGTH",
    "WINDOW_HOURS",
    "RequestNotExecutedError",
    "RollbackRequestInput",
    "RollbackRequestOutput",
    "RollbackWindowExpiredError",
    "SetKillSwitchInput",
    "SetKillSwitchOutput",
    "rollback_request",
    "set_kill_switch",
]
