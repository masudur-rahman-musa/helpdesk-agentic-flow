"""One argument model per tool the agent may call, and one result model.

A copy of the server's shapes: agent-api and mcp-server are separate apps
that never import each other. A test compares these against the live server's
tool list, so a rename fails the suite, not a user's turn. Tools left out here
(admin tools, for one) can never be proposed, on top of the RBAC check.
Extra fields are refused everywhere, so a model that invents a `tenant_id`
is caught. No length caps here: each cap lives once, with its handler.
"""

import uuid
from collections.abc import Mapping
from datetime import date
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from helpdesk.agent_api.agent.state import Citation
from helpdesk.shared.models.access import RequestStatus, ReviewDecision

IDEMPOTENCY_KEY = "idempotency_key"
"""The key field mutating tools carry. The agent fills it; the planner
never sees it."""

SEARCH_POLICY = "search_policy"
"""The tool name as the server spells it."""

SEARCH_POLICY_TOP_K = 6
"""How many policy passages one search asks for. A copy of the server's
default, so agent-api need not import the search code and its vector store."""

_FORBID = ConfigDict(extra="forbid")


class _ToolArgs(BaseModel):
    """Base for a READ tool's arguments: no idempotency key, no extras."""

    model_config = _FORBID


class _MutatingToolArgs(BaseModel):
    """Base for a mutating tool's arguments, with its required key.

    The planner sees `planner_schema`, which drops the key, so only the agent
    fills it. Whether a tool needs a key is read off this field, not a list.
    """

    model_config = _FORBID

    idempotency_key: str


# --- read tools ----------------------------------------------------------------


class GetMeArgs(_ToolArgs):
    """No arguments. The user described is the one on the token."""


class ListMyAccessArgs(_ToolArgs):
    """No arguments, for the same reason: the access listed is the caller's own."""


class ListMyRequestsArgs(_ToolArgs):
    """`limit` copies the server's default. A test keeps the two equal."""

    status: RequestStatus | None = Field(
        default=None, description="Only requests in this status, or all of them when absent."
    )
    limit: int = Field(default=50, description="How many requests to return, at most.")


class ListRequestableRolesArgs(_ToolArgs):
    """`limit` copies the server's default. A test keeps the two equal."""

    system: str | None = Field(
        default=None, description="Only this system's roles, by its slug, or all when absent."
    )
    limit: int = Field(default=50, description="How many roles to return, at most.")


class ClassifyAccessRequestArgs(_ToolArgs):
    """Asks for a risk decision in advance; nothing is written."""

    target_user_id: uuid.UUID = Field(description="Who the access would be for.")
    role_id: uuid.UUID = Field(description="The role being asked for.")
    requested_expiry_days: int | None = Field(
        default=None, description="How many days the access is wanted for, if the user said."
    )


class ListReviewItemsArgs(_ToolArgs):
    review_id: uuid.UUID = Field(description="The access review to list the flagged grants of.")
    limit: int = Field(default=50, description="How many items to return, at most.")


class ListPendingApprovalsArgs(_ToolArgs):
    limit: int = Field(default=50, description="How many requests to return, at most.")


class SearchPolicyArgs(_ToolArgs):
    """A question and a page size. The tenant is not an argument.

    The server picks the collection from the token. Never offered to the planner:
    the search runs in its own step.
    """

    query: str = Field(description="The policy question, in the user's own words.")
    top_k: int = Field(
        default=SEARCH_POLICY_TOP_K,
        description="How many policy chunks to return, at most.",
    )


# --- mutating tools ------------------------------------------------------------


class RequestCitation(BaseModel):
    """One citation as `create_access_request` stores it: the reference, not
    the passage text or score. The drift test does not cover this nested shape."""

    model_config = _FORBID

    document_slug: str
    title: str
    version: str
    heading_path: str
    chunk_id: str


class CreateAccessRequestArgs(_MutatingToolArgs):
    target_user_id: uuid.UUID = Field(description="Who the access is for.")
    role_id: uuid.UUID = Field(description="The role being requested.")
    justification: str = Field(description="Why the access is needed, in the user's own words.")
    requested_expiry_days: int | None = Field(
        default=None, description="How many days the access is wanted for, if the user said."
    )
    citations: list[RequestCitation] = Field(
        default_factory=list, description="The policy passages this request was decided against."
    )


class RevokeAccessArgs(_MutatingToolArgs):
    grant_id: uuid.UUID = Field(description="The grant to end.")
    reason: str = Field(description="Why the access is being removed.")


class OnboardUserArgs(_MutatingToolArgs):
    email: str = Field(description="The joiner's work address.")
    display_name: str = Field(description="The joiner's name as it should be shown.")
    department_id: uuid.UUID = Field(description="The department they join.")
    start_date: date = Field(description="The day they start, as YYYY-MM-DD.")
    manager_user_id: uuid.UUID = Field(description="Who they report to.")


class TransferUserArgs(_MutatingToolArgs):
    user_id: uuid.UUID = Field(description="The person moving.")
    new_department_id: uuid.UUID = Field(description="The department they move to.")
    effective_date: date = Field(description="The day the move takes effect, as YYYY-MM-DD.")


class OffboardUserArgs(_MutatingToolArgs):
    user_id: uuid.UUID = Field(description="The person leaving.")
    end_date: date = Field(description="Their last day, as YYYY-MM-DD.")


class OpenAccessReviewArgs(_MutatingToolArgs):
    department_id: uuid.UUID | None = Field(
        default=None, description="Review a department's access. Set this or system_id, not both."
    )
    system_id: uuid.UUID | None = Field(
        default=None, description="Review a system's access. Set this or department_id, not both."
    )
    period: str = Field(description="The quarter being reviewed, as YYYY-Qn.")


class DecideReviewItemArgs(_MutatingToolArgs):
    item_id: uuid.UUID = Field(description="The flagged grant being decided.")
    decision: ReviewDecision = Field(description="Keep the access, or revoke it.")


class ApproveRequestArgs(_MutatingToolArgs):
    request_id: uuid.UUID = Field(description="The access request being approved.")
    comment: str | None = Field(default=None, description="An optional note for the audit trail.")


class RejectRequestArgs(_MutatingToolArgs):
    request_id: uuid.UUID = Field(description="The access request being rejected.")
    reason: str = Field(description="Why it is being rejected.")


class ResetOwnPasswordArgs(_MutatingToolArgs):
    """No arguments beyond the key: the account reset is the token user's own."""


class UnlockOwnAccountArgs(_MutatingToolArgs):
    """No arguments beyond the key: the account unlocked is the token user's own."""


class CreateTicketArgs(_MutatingToolArgs):
    kind: str = Field(
        description="One of: password_reset, unlock, general, escalation.",
    )
    title: str = Field(description="A one-line summary of what is needed.")
    body: str = Field(description="What a person picking this up needs to know.")


class SearchPolicyResult(BaseModel):
    """The page, and whether the tool refused to answer from it. `refused` is
    read, never recomputed; `citations` is empty when refused."""

    citations: list[Citation]
    refused: bool


TOOL_INPUTS: Mapping[str, type[BaseModel]] = MappingProxyType(
    {
        "get_me": GetMeArgs,
        "list_my_access": ListMyAccessArgs,
        "list_my_requests": ListMyRequestsArgs,
        "list_requestable_roles": ListRequestableRolesArgs,
        "classify_access_request": ClassifyAccessRequestArgs,
        "create_access_request": CreateAccessRequestArgs,
        "revoke_access": RevokeAccessArgs,
        "onboard_user": OnboardUserArgs,
        "transfer_user": TransferUserArgs,
        "offboard_user": OffboardUserArgs,
        "open_access_review": OpenAccessReviewArgs,
        "list_review_items": ListReviewItemsArgs,
        "decide_review_item": DecideReviewItemArgs,
        "list_pending_approvals": ListPendingApprovalsArgs,
        "approve_request": ApproveRequestArgs,
        "reject_request": RejectRequestArgs,
        "reset_own_password": ResetOwnPasswordArgs,
        "unlock_own_account": UnlockOwnAccountArgs,
        SEARCH_POLICY: SearchPolicyArgs,
        "create_ticket": CreateTicketArgs,
    }
)
"""Every tool the agent may call, with its argument model. Read-only, so no
tool can be added at runtime without the drift test seeing it."""


def needs_idempotency_key(name: str) -> bool:
    """Whether this tool's call carries a key the agent must derive. Read off the
    model's fields, so it cannot disagree with them."""
    return IDEMPOTENCY_KEY in TOOL_INPUTS[name].model_fields


def planner_schema(name: str) -> dict[str, Any]:
    """The tool's JSON schema as the model sees it, without the key.

    Built from the validating model, so offered and accepted shapes match. With
    the key gone, a model that names one fails as an unknown argument rather than
    reusing a key across two requests. Nested parts are copied so nothing shared
    is changed.
    """
    schema = dict(TOOL_INPUTS[name].model_json_schema())
    properties = dict(schema.get("properties", {}))
    properties.pop(IDEMPOTENCY_KEY, None)
    schema["properties"] = properties
    required = [field for field in schema.get("required", []) if field != IDEMPOTENCY_KEY]
    if required:
        schema["required"] = required
    else:
        schema.pop("required", None)
    return schema


__all__ = [
    "IDEMPOTENCY_KEY",
    "SEARCH_POLICY",
    "SEARCH_POLICY_TOP_K",
    "TOOL_INPUTS",
    "SearchPolicyArgs",
    "SearchPolicyResult",
    "needs_idempotency_key",
    "planner_schema",
]
