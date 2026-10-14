"""The admin tools: the design's Admin group. `rollback_request` is the first, `search_audit` lives with the reads, and
`set_kill_switch` is the third and `list_kill_switches` the fourth.

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

`list_kill_switches` is the read half of the row above, and the whole of what
the design's `/admin/killswitch` page is drawn from: every registered tool, both
layers of its switch, in one `MGET`. It takes no arguments at all, so there is
nothing for a caller to choose and nothing to trust, and it
writes nothing — the same single `platform_admin` cell as the write, because a
board whose audience is wider than the write's would show six principals
switches none of them can change.

`reingest_policy_document` is the WRITE half of the read below it,
and what the design means by calling ingestion "also an admin API": one corpus
document, named by its slug and version, re-read and made live. Everything it
decides lives in `helpdesk.rag.pipeline.ingest_one_document` -- the corpus
path, the `source_path` string, both name checks and the eight-effect order --
and what lives here is the wire contract, the tenant slug read back off the
token, and one clock read. Its two cells are `list_policy_documents`'s, and it
is absent from the agent's catalogue (decision G): a model that can re-ingest
a corpus can change the evidence its own next answer cites, so the only caller
is a person on `/admin/policies`, exactly as `set_kill_switch` is.

`list_policy_documents` is the sixth, and the read the design's
`/admin/policies` page is drawn from: which documents this tenant's corpus
holds, which versions exist of each, which one is live and whether a version
actually landed. Everything it decides lives in
`helpdesk.rag.documents.read_policy_documents` -- the two statements, the
grouping by slug, the title rule and both orders -- and what lives here is the
wire contract alone. It takes no arguments either, and its two cells are
`it_admin` and `platform_admin`, the design's audience for that page (a design note,
cited rather than re-opened).

`list_sod_rules` is the fifth, and the read the design's
`/admin/rules` page is drawn from: the tenant's separation-of-duties rules
with both sides resolved to a role and system a person can read. Everything
it decides lives in `helpdesk.access.rules` -- the three statements, the
total order and the null block on a side that cannot be resolved -- and what
lives here is the wire contract alone. It takes no arguments either, and its
two cells are `it_admin` and `platform_admin`, the design's audience for that
page.

No function here opens a session or touches the transport: the pipeline binds
the session to the token's tenant before the handler is called.
"""

import uuid
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.mcp_server import rag_clients
from helpdesk.mcp_server.identity import Identity
from helpdesk.mcp_server.redis_client import _redis
from helpdesk.mcp_server.services.access_requests.rollback import (
    WINDOW_HOURS as WINDOW_HOURS,  # re-exported for the registry
)
from helpdesk.mcp_server.services.access_requests.rollback import (
    RequestNotExecutedError as RequestNotExecutedError,  # re-exported for the tests
)
from helpdesk.mcp_server.services.access_requests.rollback import (
    RollbackWindowExpiredError as RollbackWindowExpiredError,  # re-exported for the tests
)
from helpdesk.mcp_server.services.access_requests.rollback import (
    rollback_request as rollback,
)
from helpdesk.mcp_server.services.access_requests.rules import SodRuleRole, read_sod_rules
from helpdesk.mcp_server.services.rag.documents import read_policy_documents
from helpdesk.mcp_server.services.rag.pipeline import ingest_one_document
from helpdesk.mcp_server.tools.access import REASON_MAX_LENGTH
from helpdesk.mcp_server.tools.base import MutatingToolInput
from helpdesk.mcp_server.tools.policy import UnknownTenantError
from helpdesk.shared.access import (
    SimulatedDirectory,
    lock_request,
)
from helpdesk.shared.auth import rbac
from helpdesk.shared.core.repository import find_tenant_by_id
from helpdesk.shared.core.settings import load_settings
from helpdesk.shared.errors import NotFoundError
from helpdesk.shared.killswitch import (
    GLOBAL,
    GLOBAL_SEGMENT,
    PROTECTED_TOOLS,
    KillSwitch,
    Scope,
    SwitchState,
    change_switch,
)
from helpdesk.shared.killswitch import (
    REASON_MAX_LENGTH as SWITCH_REASON_MAX_LENGTH,
)
from helpdesk.shared.killswitch import (
    TOOL_NAME_MAX_LENGTH as TOOL_NAME_MAX_LENGTH,  # re-exported for the tests
)
from helpdesk.shared.killswitch.switch import InvalidSwitchTargetError
from helpdesk.shared.models import RequestStatus


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
    from helpdesk.mcp_server.registry import REGISTRY

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


class ListKillSwitchesInput(BaseModel):
    """No fields at all, and nothing for a caller to choose.

    The tenant is the token's and the tool list is the server's own registry, so there is no argument to trust and none to validate.
    `extra="forbid"` rather than `ignore`: a caller who sends a `tenant_id`
    believing they are reading another tenant's board must be refused, not
    quietly answered with their own. Pinned as an EXACTLY empty field set by
    `test_mcp_list_kill_switches.py::test_the_tool_takes_no_arguments_at_all`,
    which no later field can ride along into.
    """

    model_config = ConfigDict(extra="forbid")


class KillSwitchRow(BaseModel):
    """One tool's line on the admin board.

    Both layers, never one effective state (decision B): an admin clearing
    the tenant switch has to be told the global one still holds the tool
    down. `stopped` is carried beside them so row 24d renders a line without
    re-implementing the precedence, and `protected` so its toggle is disabled
    from the row rather than from a second copy of a security-relevant list
    (decision G). The exact six keys are pinned by
    `test_mcp_list_kill_switches.py::test_the_board_answers_the_envelope_and_an_exact_six_key_row`.
    """

    tool: str
    protected: bool
    """True when no switch may stop this tool at all: read off
    `helpdesk.killswitch.PROTECTED_TOOLS`, the frozenset that actually
    refuses the write, never retyped here. Pinned by
    `::test_protected_is_true_for_the_one_tool_no_switch_may_stop`."""

    stopped: bool
    """True when EITHER layer holds the tool down, or when its stored value
    could not be read. Never derived from the tenant layer alone: the board
    and `KillSwitch.check` must agree about what is running, pinned together
    in `::test_a_stopped_tool_reads_stopped_on_the_layer_it_was_stopped_on`,
    which calls the stopped tool and asserts the refusal."""

    global_state: SwitchState | None
    tenant_state: SwitchState | None
    unreadable: bool
    """The stored value is present but will not parse. Reported rather than
    raised (decision D), because this board is the only surface from which a
    bad key can be seen and cleared, and always alongside `stopped` -- a row
    that read "running" while `check` denied every call would be the one
    answer an operator cannot act on
    (`::test_an_unreadable_value_marks_its_row_and_the_board_still_answers`)."""


class ListKillSwitchesOutput(BaseModel):
    """The whole board: one row per registered tool, in the registry's order."""

    switches: list[KillSwitchRow]


async def list_kill_switches(
    session: AsyncSession, identity: Identity, args: ListKillSwitchesInput
) -> ListKillSwitchesOutput:
    """Every registered tool and the state of its two kill switches.

    The read half of `set_kill_switch`, and what the design's
    `/admin/killswitch` page is drawn from: `KillSwitch.status` answers for
    one tool at a time and `check` answers by raising, so before this there
    was no way to ask which tools are currently stopped.

    The tool names come from the REGISTRY, so every registered tool has a
    line and a tool added by a later plan cannot be invisible on the page
    (`test_mcp_list_kill_switches.py::test_the_listed_names_are_exactly_the_registry_in_its_own_order`).
    Not from a Redis `SCAN`, which shows only the keys that exist and could
    never say "this tool is running", and not from the `rbac.Tool` members,
    which are a different vocabulary from the one `check` is called with
    (decision A). The import is deferred to the call for the same reason
    `set_kill_switch`'s is: `registry` imports this module.

    The whole board is ONE `MGET` inside `list_states` (decision C), against
    the token's tenant and the global scope alone — another tenant's switch
    appears nowhere here (a project rule,
    `::test_another_tenants_switch_is_invisible_to_this_caller`).

    No scope helper and no second permission check: the design gives the row one
    cell, so the pipeline's matrix stage IS the whole check and deleting it
    hands the board to the six principals with no cell, pinned by
    `::test_the_six_denied_principals_read_no_board` (a project rule: there
    is no second copy of that guard to hide behind).

    Nothing is written, so the pipeline's generic `tool.succeeded` is the
    whole record a read of the board leaves
    (`::test_one_call_writes_exactly_one_event_naming_the_tool_and_the_caller`).
    The session is unused: this tool reads Redis and nothing else.
    """
    from helpdesk.mcp_server.registry import REGISTRY

    tools = list(REGISTRY)
    states = await KillSwitch(_redis(load_settings())).list_states(identity.tenant_id, tools)
    return ListKillSwitchesOutput(
        switches=[
            KillSwitchRow(
                tool=row.tool,
                protected=row.tool in PROTECTED_TOOLS,
                stopped=row.unreadable
                or row.global_state is not None
                or row.tenant_state is not None,
                global_state=row.global_state,
                tenant_state=row.tenant_state,
                unreadable=row.unreadable,
            )
            for row in states
        ]
    )


class ListSodRulesInput(BaseModel):
    """No fields at all, and nothing for a caller to choose.

    The tenant is the token's and the rules are that tenant's own rows (hard
    rule 6), so there is no argument to trust and none to validate.
    `extra="forbid"` rather than `ignore`, exactly as `ListKillSwitchesInput`:
    a caller who sends a `tenant_id` believing they are reading another
    tenant's rules must be refused, not quietly answered with their own.
    Pinned as an EXACTLY empty field set by
    `test_mcp_list_sod_rules.py::test_the_tool_takes_no_arguments_at_all`,
    which no later field can ride along into.
    """

    model_config = ConfigDict(extra="forbid")


class SodRuleRoleBlock(BaseModel):
    """One side of a rule, resolved. Exactly five fields (plan criterion 4).

    Null on a side whose role row cannot be read, never absent and never a
    dropped rule: the id stays top-level on the row so the admin reading the
    page can act on it (decision B). Pinned by
    `test_mcp_list_sod_rules.py::test_the_board_answers_the_envelope_and_an_exact_seven_key_row`.
    """

    role_id: uuid.UUID
    slug: str
    name: str
    system_slug: str
    system_name: str


class SodRuleRow(BaseModel):
    """One rule as 24eb puts it on the wire. Exactly seven fields.

    Both ids are always present and each resolved block may be null, which is
    the whole of decision B: nullability is the marker, and no flag is
    carried beside it because there is no second fact to say.
    """

    rule_id: uuid.UUID
    role_a_id: uuid.UUID
    role_a: SodRuleRoleBlock | None
    role_b_id: uuid.UUID
    role_b: SodRuleRoleBlock | None
    rationale: str
    policy_ref: str


class ListSodRulesOutput(BaseModel):
    """Every rule the tenant has, in the helper's order."""

    rules: list[SodRuleRow]


async def list_sod_rules(
    session: AsyncSession, identity: Identity, args: ListSodRulesInput
) -> ListSodRulesOutput:
    """This tenant's separation-of-duties rules, resolved to names.

    What the design's `/admin/rules` page is drawn from. The rows have been
    readable as a DECISION since an earlier change -- `snapshot.py` hands the engine
    three ids and a `policy_ref` -- and readable as RULES nowhere: which two
    roles conflict, in words, and why, is the question this answers and
    nothing else did.

    Transport only. The order, the three statements and the null block on an
    unresolvable side are `helpdesk.access.rules.read_sod_rules`'s, so 24f's
    page and every later caller read one sequence rather than a second copy
    inside a handler; the mapping below adds nothing but the wire's names.

    No scope helper and no second permission check: the design gives the row two
    cells, both `Scope.ANY`, so the pipeline's matrix stage IS the whole check
    and deleting it hands the rules to the five principals with no cell,
    pinned by `test_mcp_list_sod_rules.py::test_the_five_denied_principals_read_no_rules`
    (a project rule: there is no second copy of that guard to hide behind).

    Nothing is written, so the pipeline's generic `tool.succeeded` is the
    whole record a read leaves
    (`::test_one_call_writes_exactly_one_event_naming_the_tool_and_the_caller`).
    The session is the pipeline's, already bound to the token's tenant.
    """

    def block(role: SodRuleRole | None) -> SodRuleRoleBlock | None:
        if role is None:
            return None
        return SodRuleRoleBlock(
            role_id=role.role_id,
            slug=role.slug,
            name=role.name,
            system_slug=role.system_slug,
            system_name=role.system_name,
        )

    details = await read_sod_rules(session)
    return ListSodRulesOutput(
        rules=[
            SodRuleRow(
                rule_id=detail.rule_id,
                role_a_id=detail.role_a_id,
                role_a=block(detail.role_a),
                role_b_id=detail.role_b_id,
                role_b=block(detail.role_b),
                rationale=detail.rationale,
                policy_ref=detail.policy_ref,
            )
            for detail in details
        ]
    )


class ListPolicyDocumentsInput(BaseModel):
    """No fields at all, and nothing for a caller to choose.

    The tenant is the token's and the corpus is that tenant's own rows (hard
    rule 6), so there is no argument to trust and none to validate.
    `extra="forbid"` rather than `ignore`, exactly as `ListSodRulesInput`: a
    caller who sends a `tenant_id` believing they are reading another tenant's
    corpus must be refused, not quietly answered with their own, and a `slug`
    or a `version` asking the board to narrow itself is refused for the same
    reason. Pinned as an EXACTLY empty field set by
    `test_mcp_list_policy_documents.py::test_the_tool_declares_no_field_at_all`,
    which no later field can ride along into.
    """

    model_config = ConfigDict(extra="forbid")


class PolicyVersionRow(BaseModel):
    """One ingested version as 24g puts it on the wire. Exactly six fields.

    `chunk_count` is the only thing on this surface that separates a version
    that ingested from one that registered a document and wrote no span, and
    `sha256` the only evidence that two versions differ at all (plan decisions
    B and G). `source_path` is corpus-relative and never absolute, so nothing
    about the ingest host crosses. Pinned by
    `test_mcp_list_policy_documents.py::test_the_output_models_carry_exactly_the_planned_field_sets`.
    """

    version: str
    is_current: bool
    ingested_at: datetime
    chunk_count: int
    source_path: str
    sha256: str


class PolicyDocumentRow(BaseModel):
    """One document slug and every version of it. Exactly three fields.

    Grouped rather than flat (plan decision A): the page's question is which
    documents there are and which version of each is live, and a flat list
    would be regrouped in the browser on every paint. `title` is the current
    version's where one exists and the newest-ingested version's otherwise,
    which `read_policy_documents` decides.
    """

    slug: str
    title: str
    versions: list[PolicyVersionRow]


class ListPolicyDocumentsOutput(BaseModel):
    """The whole corpus, in the helper's order."""

    documents: list[PolicyDocumentRow]


async def list_policy_documents(
    session: AsyncSession, identity: Identity, args: ListPolicyDocumentsInput
) -> ListPolicyDocumentsOutput:
    """This tenant's policy documents, their versions, and which one is live.

    What the design's `/admin/policies` page is drawn from. The rows have been
    written since an earlier change and readable as a CORPUS nowhere: `search_policy`
    answers the spans an answer may cite, and nothing said which documents a
    tenant has, which versions exist of each or whether a version landed with
    no chunk at all.

    Transport only. The two statements, the grouping by slug, the title rule
    and both orders are `helpdesk.rag.documents.read_policy_documents`'s, so
    24h's page and every later caller read one sequence rather than a second
    copy inside a handler; the mapping below adds nothing but the wire's
    names.

    No scope helper and no second permission check: the design gives the row two
    cells, both `Scope.ANY`, so the pipeline's matrix stage IS the whole check
    and deleting it hands the corpus to the five principals with no cell,
    pinned by
    `test_mcp_list_policy_documents.py::test_the_five_denied_principals_are_refused_whatever_the_corpus_holds`
    (a project rule: there is no second copy of that guard to hide behind).
    That case runs with a corpus and without one, which is criterion 9's other
    half: the refusal is the same either way, so the tool is not an existence
    oracle for a tenant's corpus.

    Nothing is written, so the pipeline's generic `tool.succeeded` is the whole
    record a read leaves. The session is the pipeline's, already bound to the
    token's tenant.
    """
    details = await read_policy_documents(session)
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
"""The re-ingest's own success event.

The corpus is what every access decision cites, so a change to it is a
governance record in its own right rather than a generic `tool.succeeded`
naming the tool that happened to run. Named here beside the handler, the way
`access.CLASSIFIED_EVENT_TYPE` is, so the registry and the audit assertion
read one string. Pinned by
`test_mcp_reingest_policy_document.py::test_the_audit_event_type_is_policy_reingested`,
which asserts the type and its subject against a before snapshot."""


class ReingestPolicyDocumentInput(MutatingToolInput):
    """Which corpus document to make live. Exactly three fields.

    `slug` and `version` address ONE file; the tenant is the token's and is
    resolved through `find_tenant_by_id`, so a `tenant_id`
    riding along is refused rather than quietly ignored. `source_path` is not
    a field either: where a corpus file lives is `rag.pipeline`'s (decision
    H), and accepting it here would let a caller name a path.

    `idempotency_key` and `extra="forbid"` both come from
    `MutatingToolInput`, which is what makes the key required and the field
    set exact. Pinned by
    `test_mcp_reingest_policy_document.py::test_the_input_declares_exactly_slug_version_and_idempotency_key`,
    which sends an own-tenant `tenant_id` and asserts `VALIDATION` with
    nothing written.

    Neither field declares its bound here: `ingest_one_document` runs
    `check_slug` and `check_version` before the path is composed, so the caps
    have one home and one error and a refused name opens no file
    (`test_rag_pipeline.py::test_a_traversing_slug_is_refused_before_the_filesystem_is_touched`).
    """

    slug: str = Field(description="The document slug, as the corpus file is named.")
    version: str = Field(description="The version directory, spelled `v1`.")


class ReingestPolicyDocumentOutput(BaseModel):
    """What the re-ingest did. Exactly eight fields.

    `created` and `chunk_count` are the pair that separates a source that
    changed from one that did not: an unchanged source answers `false` and
    `0` because nothing was written, never because the document has no
    chunks. `is_current` says which version is live afterwards, which a
    re-ingest of an OLDER version moves (decision E). `sha256` is the only
    evidence on this surface that two versions differ at all.

    `summary` is a FIELD beside the values and never the whole response
    (the design), so the page renders the numbers and a person reads the
    sentence. Pinned by
    `test_mcp_reingest_policy_document.py::test_the_output_carries_all_seven_fields_and_a_summary`.
    """

    slug: str
    version: str
    created: bool
    chunk_count: int
    is_current: bool
    sha256: str
    ingested_at: datetime
    summary: str


async def reingest_policy_document(
    session: AsyncSession, identity: Identity, args: ReingestPolicyDocumentInput
) -> ReingestPolicyDocumentOutput:
    """Re-read one corpus file and make it live, in the caller's transaction.

    The write half of `list_policy_documents`, and what the design means by
    "also an admin API": until this existed, an edited policy file could only
    be made live from a shell on the ingest host, so the corpus every access
    decision cites drifted from the repository that holds it.

    Transport only. Where the file lives, what its `source_path` string is,
    both name checks and the whole eight-effect order are
    `helpdesk.rag.pipeline`'s (decision H), so this handler holds no path and
    mcp-server owns no filesystem convention.

    The tenant SLUG is read back from the token's tenant id, exactly as
    `search_policy` does it and never from an argument; a token
    whose tenant no longer exists is that tool's `UnknownTenantError`.

    Synchronous rather than queued (decision B): a queued answer could carry
    no `chunk_count`, which is the one field that says the ingest actually
    landed. The gateway client and the embedder are read through
    `rag_clients` by module attribute, so both are pooled for the loop rather
    than built per call (decision F).

    No scope helper and no second permission check: the design gives the row two
    cells, both `Scope.ANY`, so the pipeline's matrix stage IS the whole check
    and deleting it hands the corpus to the five principals with no cell,
    pinned by
    `test_mcp_reingest_policy_document.py::test_the_five_denied_principals_re_ingest_nothing_whatever_is_on_disk`. That case runs with the corpus file present and
    absent, so the refusal is the same either way and the tool is not an
    existence oracle for a tenant's corpus.

    The registry declares `policy.reingested` (decision D), so a change to the
    evidence every decision cites is its own governance record rather than a
    generic `tool.succeeded`; the replay of a spent key writes `tool.replayed`
    and re-runs nothing
    (`::test_a_replayed_call_is_byte_identical_and_ingests_once`).
    """
    settings = load_settings()
    tenant = await find_tenant_by_id(session, identity.tenant_id)
    if tenant is None:
        raise UnknownTenantError("the token's tenant no longer exists")

    result = await ingest_one_document(
        session,
        rag_clients.qdrant(settings),
        corpus_root=settings.policy_dir,
        tenant_slug=tenant.slug,
        slug=args.slug,
        version=args.version,
        redactor=rag_clients.redactor(settings),
        embedder=rag_clients.embedder(),
        ingested_at=datetime.now(UTC),
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
    """The sentence beside the values, in a person's words.

    Two shapes rather than one with a zero in it: "re-ingested, 0 chunks"
    reads like a failed ingest, and the case an admin most needs to tell
    apart is a file they edited from one they did not.
    """
    if not created:
        return f"{slug} {version} is unchanged; nothing was re-ingested."
    return f"{slug} {version} was re-ingested as {chunk_count} chunks and is now live."


__all__ = [
    "REINGESTED_EVENT_TYPE",
    "TOOL_NAME_MAX_LENGTH",
    "WINDOW_HOURS",
    "KillSwitchRow",
    "ListKillSwitchesInput",
    "ListKillSwitchesOutput",
    "ListPolicyDocumentsInput",
    "ListPolicyDocumentsOutput",
    "ListSodRulesInput",
    "ListSodRulesOutput",
    "PolicyDocumentRow",
    "PolicyVersionRow",
    "ReingestPolicyDocumentInput",
    "ReingestPolicyDocumentOutput",
    "RequestNotExecutedError",
    "RollbackRequestInput",
    "RollbackRequestOutput",
    "RollbackWindowExpiredError",
    "SetKillSwitchInput",
    "SetKillSwitchOutput",
    "SodRuleRoleBlock",
    "SodRuleRow",
    "list_kill_switches",
    "list_policy_documents",
    "list_sod_rules",
    "reingest_policy_document",
    "rollback_request",
    "set_kill_switch",
]
