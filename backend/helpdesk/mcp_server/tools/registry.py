"""Every tool declared once: wire name, permission, schemas, audit event type.

One declaration per tool means the transport, the role check, the kill
switch and the audit log all read the same name. `mutating` decides whether
a call needs an idempotency key and runs at most once per key.

Reads, and several writes, declare the generic `tool.succeeded`. Those
writes append their own event naming the real subject (the person, the
grant, the review), and the pipeline's event lands last as a summary.
"""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel

from helpdesk.mcp_server.tools import (
    access,
    approvals,
    killswitch,
    lifecycle,
    notifications,
    policy_docs,
    policy_search,
    read,
    reviews,
    role_suggest,
    rollback,
    rules,
    self_service,
    tenant_settings,
    tickets,
)
from helpdesk.mcp_server.tools.context import ToolContext
from helpdesk.shared.auth import rbac

SUCCESS_EVENT_TYPE = "tool.succeeded"
"""What a tool writes when it worked, unless it declares its own event type."""

DENIED_EVENT_TYPE = "tool.denied"
"""The caller held a valid token and the matrix refused them."""

DISABLED_EVENT_TYPE = "tool.disabled"
"""An admin had stopped the tool. Kept apart from denied, so the log can tell a
permission decision from a platform stop.
"""

FAILED_EVENT_TYPE = "tool.failed"
"""The call did not complete. Written from a fresh session, so it survives the
rollback that undoes the tool's own writes.
"""

REPLAYED_EVENT_TYPE = "tool.replayed"
"""A mutating call answered from its stored idempotency row.

Not the tool's own event: a second `ticket.created` for one ticket would be a
false record.
"""


WIRE_NAME_ALIASES: Mapping[str, rbac.Tool] = MappingProxyType(
    {
        "create_access_request": rbac.Tool.CREATE_ACCESS_REQUEST_SELF,
    }
)
"""Wire names that differ on purpose from their matrix member's value.

`create_access_request` has two permission rows, one for yourself and one
for someone else, but one wire name. The on-behalf row is a check inside the
handler, never a tool a client can call.
"""


@dataclass(frozen=True)
class ToolSpec[I: BaseModel, O: BaseModel]:
    """One tool, declared for every part of the pipeline that needs it.

    `name` is the wire name. It equals `tool.value` unless `WIRE_NAME_ALIASES`
    says otherwise, so the client's string, the kill-switch key and the matrix
    member are the same string.
    """

    name: str
    tool: rbac.Tool
    description: str
    input_model: type[I]
    output_model: type[O]
    event_type: str
    mutating: bool
    handler: Callable[[ToolContext, I], Awaitable[O]]


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
        name=rbac.Tool.LIST_REQUESTABLE_ROLES.value,
        tool=rbac.Tool.LIST_REQUESTABLE_ROLES,
        description=(
            "List the systems and roles the calling user may request, marking their "
            "template and held roles; filing still goes through the policy engine."
        ),
        input_model=read.ListRequestableRolesInput,
        output_model=read.ListRequestableRolesOutput,
        event_type=SUCCESS_EVENT_TYPE,
        mutating=False,
        handler=read.list_requestable_roles,
    ),
    ToolSpec(
        # One wire name, two matrix rows: self and on behalf.
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
        name=rbac.Tool.CANCEL_REQUEST.value,
        tool=rbac.Tool.CANCEL_REQUEST,
        description=(
            "Cancel an access request the calling user filed, or one filed for them, "
            "while it still waits for approval."
        ),
        input_model=approvals.CancelRequestInput,
        output_model=approvals.CancelRequestOutput,
        event_type=SUCCESS_EVENT_TYPE,
        mutating=True,
        handler=approvals.cancel_request,
    ),
    ToolSpec(
        name=rbac.Tool.RESET_OWN_PASSWORD.value,
        tool=rbac.Tool.RESET_OWN_PASSWORD,
        description="Simulated password reset for the calling user's own account.",
        input_model=self_service.ResetOwnPasswordInput,
        output_model=self_service.ResetOwnPasswordOutput,
        event_type=self_service.RESET_EVENT_TYPE,
        mutating=True,
        handler=self_service.reset_own_password,
    ),
    ToolSpec(
        name=rbac.Tool.UNLOCK_OWN_ACCOUNT.value,
        tool=rbac.Tool.UNLOCK_OWN_ACCOUNT,
        description="Simulated unlock of the calling user's own account.",
        input_model=self_service.UnlockOwnAccountInput,
        output_model=self_service.UnlockOwnAccountOutput,
        event_type=self_service.UNLOCK_EVENT_TYPE,
        mutating=True,
        handler=self_service.unlock_own_account,
    ),
    ToolSpec(
        name=rbac.Tool.ROLLBACK_REQUEST.value,
        tool=rbac.Tool.ROLLBACK_REQUEST,
        description=(
            # Read from the constant, so a wider window never leaves this text stale.
            f"Undo an executed access request within {rollback.WINDOW_HOURS} hours: "
            "revoke every live grant it produced and close it."
        ),
        input_model=rollback.RollbackRequestInput,
        output_model=rollback.RollbackRequestOutput,
        event_type=SUCCESS_EVENT_TYPE,
        mutating=True,
        handler=rollback.rollback_request,
    ),
    ToolSpec(
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
        # The handler writes `killswitch.changed` naming the TARGET tool, so an
        # auditor can find when a given tool was stopped.
        name=rbac.Tool.SET_KILL_SWITCH.value,
        tool=rbac.Tool.SET_KILL_SWITCH,
        description=(
            # Spelled out so an agent cannot read `enabled` backwards.
            "Turn a tool's kill switch on or off. enabled=true STOPS the named tool; "
            'enabled=false lets it run again. scope is "global" or your own tenant id.'
        ),
        input_model=killswitch.SetKillSwitchInput,
        output_model=killswitch.SetKillSwitchOutput,
        event_type=SUCCESS_EVENT_TYPE,
        mutating=True,
        handler=killswitch.set_kill_switch,
    ),
    ToolSpec(
        name=rbac.Tool.SET_TOOL_STEPS_VISIBLE.value,
        tool=rbac.Tool.SET_TOOL_STEPS_VISIBLE,
        description=(
            "Show or hide the tool steps box in this tenant's chat. "
            "show_tool_steps=true shows it; false hides it."
        ),
        input_model=tenant_settings.SetToolStepsVisibleInput,
        output_model=tenant_settings.SetToolStepsVisibleOutput,
        event_type=SUCCESS_EVENT_TYPE,
        mutating=True,
        handler=tenant_settings.set_tool_steps_visible,
    ),
    ToolSpec(
        # Not protected: only the tool that can undo a switch is, so this one can
        # be stopped like any other.
        name=rbac.Tool.LIST_KILL_SWITCHES.value,
        tool=rbac.Tool.LIST_KILL_SWITCHES,
        description=(
            "List every tool and the state of its kill switches, global and this tenant's. "
            "stopped=true means the tool is currently refusing calls."
        ),
        input_model=killswitch.ListKillSwitchesInput,
        output_model=killswitch.ListKillSwitchesOutput,
        event_type=SUCCESS_EVENT_TYPE,
        mutating=False,
        handler=killswitch.list_kill_switches,
    ),
    ToolSpec(
        name=rbac.Tool.LIST_SOD_RULES.value,
        tool=rbac.Tool.LIST_SOD_RULES,
        description=(
            "List this tenant's separation-of-duties rules: the two conflicting roles with "
            "their system, the rationale and the policy reference. Takes no arguments; "
            "reads only."
        ),
        input_model=rules.ListSodRulesInput,
        output_model=rules.ListSodRulesOutput,
        event_type=SUCCESS_EVENT_TYPE,
        mutating=False,
        handler=rules.list_sod_rules,
    ),
    ToolSpec(
        name=rbac.Tool.LIST_POLICY_DOCUMENTS.value,
        tool=rbac.Tool.LIST_POLICY_DOCUMENTS,
        description=(
            "List this tenant's policy documents: one row per document with its title and "
            "every ingested version, each carrying whether it is current, when it was "
            "ingested, how many chunks it holds, its source path and its sha256. Takes no "
            "arguments; reads only."
        ),
        input_model=policy_docs.ListPolicyDocumentsInput,
        output_model=policy_docs.ListPolicyDocumentsOutput,
        event_type=SUCCESS_EVENT_TYPE,
        mutating=False,
        handler=policy_docs.list_policy_documents,
    ),
    ToolSpec(
        # Open to every person, and not offered to the agent, which has
        # `search_policy` for the passages an answer cites.
        name=rbac.Tool.READ_POLICIES.value,
        tool=rbac.Tool.READ_POLICIES,
        description=(
            "Read this tenant's policy documents: the live version of each, by title, and "
            "when a slug is given that document's whole text, section by section. An "
            "unknown slug is NOT_FOUND. Reads only."
        ),
        input_model=policy_docs.ReadPoliciesInput,
        output_model=policy_docs.ReadPoliciesOutput,
        event_type=SUCCESS_EVENT_TYPE,
        mutating=False,
        handler=policy_docs.read_policies,
    ),
    ToolSpec(
        # Declares its own event: the corpus is what every access decision
        # cites. Not offered to the agent, so a model can never change the
        # evidence its own next answer cites.
        name=rbac.Tool.REINGEST_POLICY_DOCUMENT.value,
        tool=rbac.Tool.REINGEST_POLICY_DOCUMENT,
        description=(
            "Re-read one policy document from the corpus and make it live. Names the "
            "document by its slug and its version directory (spelled v1). An unchanged "
            "source writes nothing and answers created=false with zero chunks."
        ),
        input_model=policy_docs.ReingestPolicyDocumentInput,
        output_model=policy_docs.ReingestPolicyDocumentOutput,
        event_type=policy_docs.REINGESTED_EVENT_TYPE,
        mutating=True,
        handler=policy_docs.reingest_policy_document,
    ),
    ToolSpec(
        name=rbac.Tool.SEARCH_POLICY.value,
        tool=rbac.Tool.SEARCH_POLICY,
        description=(
            "Search this tenant's policy corpus and return the passages an answer may cite. "
            "Returns refused=true with no citations when nothing matches well enough to "
            "answer from."
        ),
        input_model=policy_search.SearchPolicyInput,
        output_model=policy_search.SearchPolicyOutput,
        event_type=SUCCESS_EVENT_TYPE,
        mutating=False,
        handler=policy_search.search_policy,
    ),
    ToolSpec(
        name=rbac.Tool.SUGGEST_ROLES.value,
        tool=rbac.Tool.SUGGEST_ROLES,
        description=(
            "Suggest up to three of this tenant's roles closest in meaning to the words "
            "a person typed, best first. Offers a name only; nothing is filed."
        ),
        input_model=role_suggest.SuggestRolesInput,
        output_model=role_suggest.SuggestRolesOutput,
        event_type=SUCCESS_EVENT_TYPE,
        mutating=False,
        handler=role_suggest.suggest_roles,
    ),
    ToolSpec(
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
        # Mutating so a retried click runs once, but no event of its own: one
        # audit row per click would be noise in a log where every row matters.
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
        input_model=self_service.CreateTicketInput,
        output_model=self_service.CreateTicketOutput,
        event_type=self_service.TICKET_EVENT_TYPE,
        mutating=True,
        handler=self_service.create_ticket,
    ),
    ToolSpec(
        name=rbac.Tool.LIST_MY_TICKETS.value,
        tool=rbac.Tool.LIST_MY_TICKETS,
        description="List the tickets the calling user has opened, newest first.",
        input_model=self_service.ListMyTicketsInput,
        output_model=self_service.ListMyTicketsOutput,
        event_type=SUCCESS_EVENT_TYPE,
        mutating=False,
        handler=self_service.list_my_tickets,
    ),
    ToolSpec(
        name=rbac.Tool.LIST_OPEN_TICKETS.value,
        tool=rbac.Tool.LIST_OPEN_TICKETS,
        description=(
            "List every open ticket in this tenant, oldest first, with who opened it. Reads only."
        ),
        input_model=tickets.ListOpenTicketsInput,
        output_model=tickets.ListOpenTicketsOutput,
        event_type=SUCCESS_EVENT_TYPE,
        mutating=False,
        handler=tickets.list_open_tickets,
    ),
    ToolSpec(
        name=rbac.Tool.SOLVE_TICKET.value,
        tool=rbac.Tool.SOLVE_TICKET,
        description=(
            "Mark one open ticket in this tenant solved; "
            "an already solved one answers its first solve."
        ),
        input_model=tickets.SolveTicketInput,
        output_model=tickets.SolveTicketOutput,
        event_type=tickets.SOLVED_EVENT_TYPE,
        mutating=True,
        handler=tickets.solve_ticket,
    ),
)

REGISTRY: Mapping[str, ToolSpec[Any, Any]] = MappingProxyType({spec.name: spec for spec in _SPECS})


def lookup(name: str) -> ToolSpec[Any, Any]:
    """The spec for a wire name, or a typed refusal.

    The name is matched before anything can turn it into a Redis key, so an
    unknown name writes nothing. The error is `INTERNAL`, not `FORBIDDEN`: an
    unknown name is a wiring bug, and a denial would hide it.
    """
    spec = REGISTRY.get(name)
    if spec is None:
        raise rbac.UnknownToolError(f"unknown tool: {name!r}")
    return spec
