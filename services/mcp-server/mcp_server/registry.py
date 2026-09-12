"""Every tool declared once: wire name, permission, schemas, audit event type.

One declaration per tool means the transport, the RBAC check, the kill
switch and the audit log all read the same name — a tool advertised under a
name the matrix does not know, or one the kill switch cannot store, fails in
`test_mcp_registry.py` rather than at the moment someone needs the stop.

`mutating` is what the pipeline's `run_once` branch keys on: a
mutating tool's input carries an idempotency key and its body runs at most
once per key, a property of the registry rather than a list kept somewhere
else.
"""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from helpdesk.auth import rbac
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_server.identity import Identity
from mcp_server.tools import (
    access,
    admin,
    approvals,
    helpdesk,
    lifecycle,
    notifications,
    policy,
    read,
    reviews,
)

SUCCESS_EVENT_TYPE = "tool.succeeded"
"""What a read tool writes when it worked. Per-tool rather than global (plan
10a decision C): row 11's mutating tools declare `grant.executed` and friends
from the design, and a single generic type would have to be replaced then."""

DENIED_EVENT_TYPE = "tool.denied"
"""The caller held a valid token and the matrix refused them."""

DISABLED_EVENT_TYPE = "tool.disabled"
"""An admin had stopped the tool. Distinct from denied: the audit trail must
be able to tell a permission decision from a platform stop."""

FAILED_EVENT_TYPE = "tool.failed"
"""The call reached the tool (or a check other than the two above) and did
not complete. Written from a fresh session so it survives the rollback that
takes the tool's own writes with it."""

REPLAYED_EVENT_TYPE = "tool.replayed"
"""A mutating call answered from its stored idempotency row (an earlier change
decision B). Distinct from the tool's declared event on purpose: a second
`ticket.created` for one ticket would be a false record, so a replay logs
that it replayed and nothing else."""


WIRE_NAME_ALIASES: Mapping[str, rbac.Tool] = MappingProxyType(
    {
        "create_access_request": rbac.Tool.CREATE_ACCESS_REQUEST_SELF,
    }
)
"""Wire names that deliberately differ from their matrix member's value.

the design gives `create_access_request` TWO rows — one for a self-request and
one for naming someone else — so the matrix has two members where the wire
has one name. The invariant a wire name has to satisfy
is therefore "equals the member's value, or is the one declared alias for
it"; without this table it would just be absent for that tool. The
on-behalf member is not registered at all: it is a permission the handler
checks, never a tool a client can call.

Anything here still has to be a legal kill-switch key and audit subject, and
the parametrized cases over `REGISTRY` cover it because the alias IS the
registered name. Pinned by
`test_mcp_registry.py::test_the_wire_name_aliases_are_exactly_the_declared_pair`."""


@dataclass(frozen=True)
class ToolSpec[I: BaseModel, O: BaseModel]:
    """One tool, declared for every part of the pipeline that needs it.

    `name` is the wire name. It equals `tool.value` unless `WIRE_NAME_ALIASES`
    declares otherwise, which is what
    `test_mcp_registry.py::test_each_spec_names_a_real_matrix_tool_and_agrees_with_its_key`
    asserts: the string a client sends, the string the kill switch keys on
    and the matrix member are then the same string by construction, or paired
    once and explicitly.
    """

    name: str
    tool: rbac.Tool
    description: str
    input_model: type[I]
    output_model: type[O]
    event_type: str
    mutating: bool
    handler: Callable[[AsyncSession, Identity, I], Awaitable[O]]


_SPECS: tuple[ToolSpec[Any, Any], ...] = (
    ToolSpec(
        name=rbac.Tool.GET_ME.value,
        tool=rbac.Tool.GET_ME,
        description="Return the calling user's own directory record.",
        input_model=read.GetMeInput,
        output_model=read.GetMeOutput,
        event_type=SUCCESS_EVENT_TYPE,
        mutating=False,
        handler=read.get_me,
    ),
    ToolSpec(
        name=rbac.Tool.LIST_MY_ACCESS.value,
        tool=rbac.Tool.LIST_MY_ACCESS,
        description="List the access the calling user holds right now.",
        input_model=read.ListMyAccessInput,
        output_model=read.ListMyAccessOutput,
        event_type=SUCCESS_EVENT_TYPE,
        mutating=False,
        handler=read.list_my_access,
    ),
    ToolSpec(
        name=rbac.Tool.CLASSIFY_ACCESS_REQUEST.value,
        tool=rbac.Tool.CLASSIFY_ACCESS_REQUEST,
        description="Classify a hypothetical access request; decides nothing, writes nothing.",
        input_model=access.ClassifyAccessRequestInput,
        output_model=access.ClassifyAccessRequestOutput,
        event_type=access.CLASSIFIED_EVENT_TYPE,
        mutating=False,
        handler=access.classify_access_request,
    ),
    ToolSpec(
        name=rbac.Tool.LIST_MY_REQUESTS.value,
        tool=rbac.Tool.LIST_MY_REQUESTS,
        description="List the access requests the calling user has filed.",
        input_model=access.ListMyRequestsInput,
        output_model=access.ListMyRequestsOutput,
        event_type=SUCCESS_EVENT_TYPE,
        mutating=False,
        handler=access.list_my_requests,
    ),
    ToolSpec(
        # The wire name is the design's, not the matrix member's: §7 splits
        # this one tool into a self row and an on-behalf row (decision J).
        name="create_access_request",
        tool=rbac.Tool.CREATE_ACCESS_REQUEST_SELF,
        description=(
            "File an access request; classifies, then grants, queues for approval, or blocks."
        ),
        input_model=access.CreateAccessRequestInput,
        output_model=access.CreateAccessRequestOutput,
        event_type=access.REQUEST_CREATED_EVENT_TYPE,
        mutating=True,
        handler=access.create_access_request,
    ),
    ToolSpec(
        # `tool.succeeded` is the SUMMARY event: the
        # handler appends one `grant.revoked` per revoked row, because a set of
        # two revocations recorded as one event naming the tool answers no
        # question an auditor asks.
        name=rbac.Tool.REVOKE_ACCESS.value,
        tool=rbac.Tool.REVOKE_ACCESS,
        description=(
            "Revoke a grant, and every other live grant the same user holds for that role."
        ),
        input_model=access.RevokeAccessInput,
        output_model=access.RevokeAccessOutput,
        event_type=SUCCESS_EVENT_TYPE,
        mutating=True,
        handler=access.revoke_access,
    ),
    ToolSpec(
        # `tool.succeeded` is the SUMMARY event: the
        # handler appends `user.onboarded` itself with the NEW USER as its
        # subject, because the pipeline's event names the tool and would
        # record that somebody called one, not who was hired.
        name=rbac.Tool.ONBOARD_USER.value,
        tool=rbac.Tool.ONBOARD_USER,
        description=("Create a joiner in this tenant and apply their department's role template."),
        input_model=lifecycle.OnboardUserInput,
        output_model=lifecycle.OnboardUserOutput,
        event_type=SUCCESS_EVENT_TYPE,
        mutating=True,
        handler=lifecycle.onboard_user,
    ),
    ToolSpec(
        # `tool.succeeded` is the SUMMARY event: the
        # handler appends `user.transferred` itself with the MOVER as its
        # subject and the whole diff as its payload, which is what an auditor
        # asking "why does this person hold this" reads. The pipeline's event
        # names the tool and would answer that for nobody.
        name=rbac.Tool.TRANSFER_USER.value,
        tool=rbac.Tool.TRANSFER_USER,
        description=(
            "Move a user to another department, applying the new template and revoking the old."
        ),
        input_model=lifecycle.TransferUserInput,
        output_model=lifecycle.TransferUserOutput,
        event_type=SUCCESS_EVENT_TYPE,
        mutating=True,
        handler=lifecycle.transfer_user,
    ),
    ToolSpec(
        # `tool.succeeded` is the SUMMARY event: the handler appends
        # `user.offboarded` itself with the LEAVER as its subject, plus one
        # `grant.revoked` per revoked row and one `request.rejected` per closed
        # request. The pipeline's event names the tool, so as the declared type
        # it would record that somebody called one and not who left.
        name=rbac.Tool.OFFBOARD_USER.value,
        tool=rbac.Tool.OFFBOARD_USER,
        description=(
            "Mark a user a leaver, revoke every grant they hold and close their open requests."
        ),
        input_model=lifecycle.OffboardUserInput,
        output_model=lifecycle.OffboardUserOutput,
        event_type=SUCCESS_EVENT_TYPE,
        mutating=True,
        handler=lifecycle.offboard_user,
    ),
    ToolSpec(
        # `tool.succeeded` is the SUMMARY event: the
        # domain appends `review.opened` itself with the REVIEW as its subject
        # and the flag tally as its payload. The pipeline's event names the
        # tool, so as the declared type it would record that somebody called
        # one and not which scope was reviewed or what was found.
        name=rbac.Tool.OPEN_ACCESS_REVIEW.value,
        tool=rbac.Tool.OPEN_ACCESS_REVIEW,
        description=(
            "Open an access review over a department or a system, flagging every live grant in it."
        ),
        input_model=reviews.OpenAccessReviewInput,
        output_model=reviews.OpenAccessReviewOutput,
        event_type=SUCCESS_EVENT_TYPE,
        mutating=True,
        handler=reviews.open_access_review,
    ),
    ToolSpec(
        # `tool.succeeded` is the SUMMARY event: the
        # handler appends `review.item_decided` itself with the ITEM as its
        # subject, and a revoke decision appends `grant.revoked` before it.
        # The pipeline's event names the tool, so as the declared type it
        # would record that somebody called one and not which grant was ended
        # or whether the review is now finished.
        name=rbac.Tool.DECIDE_REVIEW_ITEM.value,
        tool=rbac.Tool.DECIDE_REVIEW_ITEM,
        description="Keep or revoke one flagged grant of an access review.",
        input_model=reviews.DecideReviewItemInput,
        output_model=reviews.DecideReviewItemOutput,
        event_type=SUCCESS_EVENT_TYPE,
        mutating=True,
        handler=reviews.decide_review_item,
    ),
    ToolSpec(
        name=rbac.Tool.LIST_REVIEW_ITEMS.value,
        tool=rbac.Tool.LIST_REVIEW_ITEMS,
        description="List the flagged grants of one access review.",
        input_model=reviews.ListReviewItemsInput,
        output_model=reviews.ListReviewItemsOutput,
        event_type=SUCCESS_EVENT_TYPE,
        mutating=False,
        handler=reviews.list_review_items,
    ),
    ToolSpec(
        # An earlier change: a read, so the generic `tool.succeeded` is the WHOLE
        # record — nothing here appends an event of its own, because reading a
        # queue changes nothing an auditor could ask about.
        name=rbac.Tool.LIST_REVIEWS.value,
        tool=rbac.Tool.LIST_REVIEWS,
        description="List the access reviews the calling user's scope reaches, oldest due first.",
        input_model=reviews.ListReviewsInput,
        output_model=reviews.ListReviewsOutput,
        event_type=SUCCESS_EVENT_TYPE,
        mutating=False,
        handler=reviews.list_reviews,
    ),
    ToolSpec(
        name=rbac.Tool.LIST_PENDING_APPROVALS.value,
        tool=rbac.Tool.LIST_PENDING_APPROVALS,
        description="List the access requests waiting on the calling user's approval.",
        input_model=approvals.ListPendingApprovalsInput,
        output_model=approvals.ListPendingApprovalsOutput,
        event_type=SUCCESS_EVENT_TYPE,
        mutating=False,
        handler=approvals.list_pending_approvals,
    ),
    ToolSpec(
        name=rbac.Tool.APPROVE_REQUEST.value,
        tool=rbac.Tool.APPROVE_REQUEST,
        description=(
            "Approve one slot of an access request's chain; grants when it was the last one."
        ),
        input_model=approvals.ApproveRequestInput,
        output_model=approvals.ApproveRequestOutput,
        event_type=SUCCESS_EVENT_TYPE,
        mutating=True,
        handler=approvals.approve_request,
    ),
    ToolSpec(
        name=rbac.Tool.REJECT_REQUEST.value,
        tool=rbac.Tool.REJECT_REQUEST,
        description="Reject an access request; ends the approval chain and grants nothing.",
        input_model=approvals.RejectRequestInput,
        output_model=approvals.RejectRequestOutput,
        event_type=SUCCESS_EVENT_TYPE,
        mutating=True,
        handler=approvals.reject_request,
    ),
    ToolSpec(
        name=rbac.Tool.RESET_OWN_PASSWORD.value,
        tool=rbac.Tool.RESET_OWN_PASSWORD,
        description="Simulated password reset for the calling user's own account.",
        input_model=helpdesk.ResetOwnPasswordInput,
        output_model=helpdesk.ResetOwnPasswordOutput,
        event_type=helpdesk.RESET_EVENT_TYPE,
        mutating=True,
        handler=helpdesk.reset_own_password,
    ),
    ToolSpec(
        name=rbac.Tool.UNLOCK_OWN_ACCOUNT.value,
        tool=rbac.Tool.UNLOCK_OWN_ACCOUNT,
        description="Simulated unlock of the calling user's own account.",
        input_model=helpdesk.UnlockOwnAccountInput,
        output_model=helpdesk.UnlockOwnAccountOutput,
        event_type=helpdesk.UNLOCK_EVENT_TYPE,
        mutating=True,
        handler=helpdesk.unlock_own_account,
    ),
    ToolSpec(
        name=rbac.Tool.ROLLBACK_REQUEST.value,
        tool=rbac.Tool.ROLLBACK_REQUEST,
        description=(
            # The number comes from the domain's own constant, never a
            # literal: a widened window must not leave the description
            # telling an agent an older request cannot be undone.
            f"Undo an executed access request within {admin.WINDOW_HOURS} hours: "
            "revoke every live grant it produced and close it."
        ),
        input_model=admin.RollbackRequestInput,
        output_model=admin.RollbackRequestOutput,
        event_type=SUCCESS_EVENT_TYPE,
        mutating=True,
        handler=admin.rollback_request,
    ),
    ToolSpec(
        # A READ: `search_audit` changes nothing, so it carries no
        # idempotency key and the pipeline's generic `tool.succeeded` is the
        # whole record it leaves. The wire name, the matrix member, the kill
        # switch key and the audit subject are one string by construction.
        name=rbac.Tool.SEARCH_AUDIT.value,
        tool=rbac.Tool.SEARCH_AUDIT,
        description=("Search this tenant's audit log, newest first, within the caller's scope."),
        input_model=read.SearchAuditInput,
        output_model=read.SearchAuditOutput,
        event_type=SUCCESS_EVENT_TYPE,
        mutating=False,
        handler=read.search_audit,
    ),
    ToolSpec(
        # A non-mutating spec that WRITES: one SET or
        # DELETE of one key is idempotent by nature, and the design omits the
        # idempotency key for this tool alone, so it carries none and stays
        # off `test_mcp_registry.py::MUTATING_TOOL_NAMES`.
        #
        # `tool.succeeded` is the SUMMARY event: `helpdesk.killswitch.change`
        # appends `killswitch.changed` itself with the TARGET TOOL as its
        # subject. Declaring that type here instead would file the change
        # under `set_kill_switch` and answer nobody asking when a given tool
        # was stopped.
        name=rbac.Tool.SET_KILL_SWITCH.value,
        tool=rbac.Tool.SET_KILL_SWITCH,
        description=(
            # The polarity in words, decision B: an agent reading this cannot
            # get `enabled` backwards.
            "Turn a tool's kill switch on or off. enabled=true STOPS the named tool; "
            'enabled=false lets it run again. scope is "global" or your own tenant id.'
        ),
        input_model=admin.SetKillSwitchInput,
        output_model=admin.SetKillSwitchOutput,
        event_type=SUCCESS_EVENT_TYPE,
        mutating=False,
        handler=admin.set_kill_switch,
    ),
    ToolSpec(
        # A READ, and the half of the row above that was missing: nothing
        # could say WHICH tools were stopped. It writes nothing, so it carries
        # no idempotency key, stays off
        # `test_mcp_registry.py::MUTATING_TOOL_NAMES`, and the pipeline's
        # generic `tool.succeeded` is the whole record a render of the board
        # leaves — a `killswitch.changed` per page view would put a governance
        # record in the hash chain for a call that changed nothing.
        #
        # It is NOT in `helpdesk.killswitch.PROTECTED_TOOLS`: only the tool
        # that can UNDO a switch is protected, so this one can be stopped like
        # any other.
        name=rbac.Tool.LIST_KILL_SWITCHES.value,
        tool=rbac.Tool.LIST_KILL_SWITCHES,
        description=(
            "List every tool and the state of its kill switches, global and this tenant's. "
            "stopped=true means the tool is currently refusing calls."
        ),
        input_model=admin.ListKillSwitchesInput,
        output_model=admin.ListKillSwitchesOutput,
        event_type=SUCCESS_EVENT_TYPE,
        mutating=False,
        handler=admin.list_kill_switches,
    ),
    ToolSpec(
        # A READ over Qdrant: nothing is written, so no idempotency key and no
        # domain event of its own — the pipeline's generic `tool.succeeded` is
        # the whole record a search leaves. The wire name, the matrix member,
        # the kill switch key and the audit subject are one string by
        # construction, which is what lets an admin stop policy search alone.
        name=rbac.Tool.SEARCH_POLICY.value,
        tool=rbac.Tool.SEARCH_POLICY,
        description=(
            "Search this tenant's policy corpus and return the passages an answer may cite. "
            "Returns refused=true with no citations when nothing matches well enough to "
            "answer from."
        ),
        input_model=policy.SearchPolicyInput,
        output_model=policy.SearchPolicyOutput,
        event_type=SUCCESS_EVENT_TYPE,
        mutating=False,
        handler=policy.search_policy,
    ),
    ToolSpec(
        # An earlier change: a READ, so it carries no idempotency key and the generic
        # `tool.succeeded` is the whole record it leaves — reading your own
        # bell changes nothing an auditor could ask about.
        name=rbac.Tool.LIST_NOTIFICATIONS.value,
        tool=rbac.Tool.LIST_NOTIFICATIONS,
        description=(
            "List the calling user's own notifications, newest first, with their unread total."
        ),
        input_model=notifications.ListNotificationsInput,
        output_model=notifications.ListNotificationsOutput,
        event_type=SUCCESS_EVENT_TYPE,
        mutating=False,
        handler=notifications.list_notifications,
    ),
    ToolSpec(
        # MUTATING, so the pipeline's `run_once` covers a retried click, and
        # the generic `tool.succeeded` is the DECLARED type rather than a
        # `notification.read` of its own: the audit log
        # records who got access to what and who decided it, and one row in
        # the hash chain per UI interaction would be noise in a table whose
        # whole value is that every row matters.
        name=rbac.Tool.MARK_NOTIFICATION_READ.value,
        tool=rbac.Tool.MARK_NOTIFICATION_READ,
        description=(
            "Mark one of the calling user's own notifications read; "
            "an already-read one answers the moment it was first seen."
        ),
        input_model=notifications.MarkNotificationReadInput,
        output_model=notifications.MarkNotificationReadOutput,
        event_type=SUCCESS_EVENT_TYPE,
        mutating=True,
        handler=notifications.mark_notification_read,
    ),
    ToolSpec(
        name=rbac.Tool.CREATE_TICKET.value,
        tool=rbac.Tool.CREATE_TICKET,
        description="File a helpdesk ticket for the calling user.",
        input_model=helpdesk.CreateTicketInput,
        output_model=helpdesk.CreateTicketOutput,
        event_type=helpdesk.TICKET_EVENT_TYPE,
        mutating=True,
        handler=helpdesk.create_ticket,
    ),
)

REGISTRY: Mapping[str, ToolSpec[Any, Any]] = MappingProxyType({spec.name: spec for spec in _SPECS})


def lookup(name: str) -> ToolSpec[Any, Any]:
    """The spec for a wire name, or a typed refusal.

    This is the gate the plan's criterion 8 names: the client's string is
    matched against the registry before anything downstream can turn it into
    a Redis key, so an unregistered or malformed name writes nothing at all
    (`test_mcp_registry.py::test_an_unregistered_wire_name_touches_no_redis_key`).

    `UnknownToolError` carries `INTERNAL`, not `FORBIDDEN`: a name outside
    the registry means the caller is wrong, and reading it as a permission
    decision would hide a wiring bug behind a legitimate-looking denial.
    """
    spec = REGISTRY.get(name)
    if spec is None:
        raise rbac.UnknownToolError(f"unknown tool: {name!r}")
    return spec
