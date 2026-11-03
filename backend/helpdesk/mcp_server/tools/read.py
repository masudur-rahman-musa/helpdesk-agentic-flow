"""The read tools: `get_me`, `list_my_access`, `list_person_access`,
`list_requestable_roles` and `search_audit`.

The user is always the token's; a stray `user_id` argument is ignored. The one
exception is `list_person_access`, whose whole job is another person.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import String
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.mcp_server.tools.context import ToolContext
from helpdesk.shared.access.approvals import request_in_scope
from helpdesk.shared.audit import search_audit_events
from helpdesk.shared.auth import rbac
from helpdesk.shared.auth.rbac import Scope
from helpdesk.shared.core.errors import NotFoundError
from helpdesk.shared.crud import Repository
from helpdesk.shared.crud.directory import (
    fetch_live_role_ids_for_user,
    fetch_role_templates_for_department,
    list_role_catalogue,
)
from helpdesk.shared.crud.grants import ActiveGrant, list_active_grants_for_user
from helpdesk.shared.crud.tenants import find_tenant_by_id, tool_steps_visible
from helpdesk.shared.crud.users import find_user_by_id
from helpdesk.shared.models import (
    AccessRequest,
    AccessReview,
    AuditEvent,
    Department,
    Grant,
    ReviewItem,
    Role,
    System,
    User,
)
from helpdesk.shared.models.numbers import NumberKind, format_number

_IGNORE_EXTRAS = ConfigDict(extra="ignore")


class GetMeInput(BaseModel):
    """No fields: everything `get_me` needs is on the token."""

    model_config = _IGNORE_EXTRAS


class GetMeOutput(BaseModel):
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    email: str
    display_name: str
    department_id: uuid.UUID | None
    department_name: str | None
    """The department's name, so the chat can say "Finance", not an id."""
    principals: list[str]
    status: str
    show_tool_steps: bool
    """The tenant's choice about the chat's steps box; off unless an admin turned it on."""


class ListMyAccessInput(BaseModel):
    """No fields: the grants listed are the token user's."""

    model_config = _IGNORE_EXTRAS


class ListMyAccessOutput(BaseModel):
    grants: list[ActiveGrant]


class ListPersonAccessInput(BaseModel):
    model_config = _IGNORE_EXTRAS

    user_id: uuid.UUID
    """The person whose access to list."""


class ListPersonAccessOutput(BaseModel):
    grants: list[ActiveGrant]


async def get_me(ctx: ToolContext, args: GetMeInput) -> GetMeOutput:
    """The token user's own directory record. Foreign users read as not found."""
    user = await find_user_by_id(ctx.session, ctx.identity.user_id)
    if user is None:
        raise NotFoundError("no such user in this tenant")
    tenant = await find_tenant_by_id(ctx.session, ctx.identity.tenant_id)
    department = (
        None
        if user.department_id is None
        else await Repository(ctx.session, Department).get(user.department_id)
    )
    return GetMeOutput(
        user_id=user.id,
        tenant_id=user.tenant_id,
        email=user.email,
        display_name=user.display_name,
        department_id=user.department_id,
        department_name=None if department is None else department.name,
        principals=list(user.principals),
        status=user.status,
        show_tool_steps=tenant is not None and tool_steps_visible(tenant),
    )


async def list_my_access(ctx: ToolContext, args: ListMyAccessInput) -> ListMyAccessOutput:
    """Every grant the token user holds at the call's own instant."""
    grants = await list_active_grants_for_user(ctx.session, ctx.identity.user_id, ctx.now)
    return ListMyAccessOutput(grants=list(grants))


async def list_person_access(
    ctx: ToolContext, args: ListPersonAccessInput
) -> ListPersonAccessOutput:
    """Every live grant of one person the caller looks after.

    Who the caller looks after is the scope this tool shares with
    `revoke_access`, checked with the same `request_in_scope`. A whole-person
    scope (any, own department) lists every grant; `own_systems` lists only
    grants on systems the caller owns. A person out of reach reads as
    `NOT_FOUND`, the same as an unknown id, so nobody can probe who exists.
    Departments come from the directory, never the token.
    """
    target = await find_user_by_id(ctx.session, args.user_id)
    if target is None:
        raise NotFoundError("no such user in this tenant")
    grants = await list_active_grants_for_user(ctx.session, target.id, ctx.now)
    if target.id == ctx.identity.user_id:
        return ListPersonAccessOutput(grants=list(grants))
    scopes = rbac.authorize(ctx.identity.claims, rbac.Tool.LIST_PERSON_ACCESS).scopes
    caller = await ctx.directory.lookup_user(ctx.identity.user_id)
    holder = await ctx.directory.lookup_user(target.id)

    whole_person = request_in_scope(
        scopes,
        approver_user_id=ctx.identity.user_id,
        approver_department_id=None if caller is None else caller.department_id,
        target_department_id=None if holder is None else holder.department_id,
        system_owner_user_id=None,
    )
    if whole_person:
        return ListPersonAccessOutput(grants=list(grants))
    visible: list[ActiveGrant] = []
    if Scope.OWN_SYSTEMS in scopes:
        owned = {
            system.slug
            for system in await Repository(ctx.session, System).list()
            if system.owner_user_id == ctx.identity.user_id
        }
        visible = [grant for grant in grants if grant.system_slug in owned]
    if not visible:
        raise NotFoundError("no such user in this tenant")
    return ListPersonAccessOutput(grants=visible)


# --- list_requestable_roles ------------------------------------------------------
#
# The tenant's catalogue plus the caller's own template and grants; nothing
# about another person. A role listed is not a promise: the policy engine
# decides when a request is filed.

ROLES_LIMIT_MIN = 1
ROLES_LIMIT_DEFAULT = 50
ROLES_LIMIT_MAX = 200

_system_slug_type = System.__table__.c["slug"].type
assert isinstance(_system_slug_type, String) and _system_slug_type.length is not None
SYSTEM_SLUG_MAX_LENGTH: int = _system_slug_type.length
"""The slug column's length, so the input cap and storage never disagree."""


class ListRequestableRolesInput(BaseModel):
    model_config = _IGNORE_EXTRAS

    system: str | None = Field(default=None, max_length=SYSTEM_SLUG_MAX_LENGTH)
    """A system slug, matched exactly; all systems when absent."""

    limit: int = Field(default=ROLES_LIMIT_DEFAULT, ge=ROLES_LIMIT_MIN, le=ROLES_LIMIT_MAX)


class RequestableRole(BaseModel):
    system_slug: str
    system_name: str
    system_sensitivity: str
    role_slug: str
    role_name: str
    privilege: str
    default_expiry_days: int
    in_my_template: bool
    """The caller's own department template gives this role."""

    already_held: bool
    """The caller has a live grant of this role."""


class ListRequestableRolesOutput(BaseModel):
    roles: list[RequestableRole]


async def list_requestable_roles(
    ctx: ToolContext, args: ListRequestableRolesInput
) -> ListRequestableRolesOutput:
    """The roles the token user may ask for, marked against their own template and grants."""
    rows = await list_role_catalogue(ctx.session, args.system, args.limit)
    user = await find_user_by_id(ctx.session, ctx.identity.user_id)
    template: frozenset[uuid.UUID] = frozenset()
    if user is not None and user.department_id is not None:
        template = await fetch_role_templates_for_department(ctx.session, user.department_id)
    held = await fetch_live_role_ids_for_user(ctx.session, ctx.identity.user_id, ctx.now)
    return ListRequestableRolesOutput(
        roles=[
            RequestableRole(
                system_slug=system.slug,
                system_name=system.name,
                system_sensitivity=system.sensitivity,
                role_slug=role.slug,
                role_name=role.name,
                privilege=role.privilege,
                default_expiry_days=role.default_expiry_days,
                in_my_template=role.id in template,
                already_held=role.id in held,
            )
            for role, system in rows
        ]
    )


# --- search_audit ---------------------------------------------------------------
#
# Not self-scoped. An audit row has no department or system of its own, so a
# narrow caller's reach is checked by mapping each row's subject to a
# (department, system owner) pair and asking `request_in_scope`, the same
# check the approval queue uses.

SEARCH_LIMIT_MIN = 1
SEARCH_LIMIT_DEFAULT = 50
SEARCH_LIMIT_MAX = 200
"""Caps the response. The scope filter runs after the read, so a page can be short."""


def _column_length(name: str) -> int:
    """A column's declared length, so the input cap and storage never disagree."""
    column_type = AuditEvent.__table__.c[name].type
    assert isinstance(column_type, String), column_type
    assert column_type.length is not None, f"audit_events.{name} must be a sized column"
    return column_type.length


EVENT_TYPE_MAX_LENGTH: int = _column_length("event_type")
SUBJECT_TYPE_MAX_LENGTH: int = _column_length("subject_type")
SUBJECT_ID_MAX_LENGTH: int = _column_length("subject_id")
ACTOR_REF_MAX_LENGTH: int = _column_length("actor_ref")

_MIN_SEQ = 1


class SearchAuditInput(BaseModel):
    """Every filter optional. Each text cap is the column's length."""

    model_config = _IGNORE_EXTRAS

    event_type: str | None = Field(default=None, max_length=EVENT_TYPE_MAX_LENGTH)
    subject_type: str | None = Field(default=None, max_length=SUBJECT_TYPE_MAX_LENGTH)
    subject_id: str | None = Field(default=None, max_length=SUBJECT_ID_MAX_LENGTH)
    actor_ref: str | None = Field(default=None, max_length=ACTOR_REF_MAX_LENGTH)
    occurred_from: datetime | None = None
    occurred_to: datetime | None = None
    before_seq: int | None = Field(default=None, ge=_MIN_SEQ)
    limit: int = Field(default=SEARCH_LIMIT_DEFAULT, ge=SEARCH_LIMIT_MIN, le=SEARCH_LIMIT_MAX)


class AuditEventRow(BaseModel):
    """One stored event. The payload is included, since that is what an auditor reads."""

    seq: int
    occurred_at: datetime
    actor_kind: str
    actor_ref: str | None
    event_type: str
    subject_type: str
    subject_id: str | None
    subject_number: str | None
    """What the page shows in place of `subject_id`, like `REQ-0107`.

    Set only for an `access_request` subject found in the caller's company. It
    is looked up on read, so the stored, hashed row never changes.
    """
    payload: dict[str, Any]
    hash: str


class SearchAuditOutput(BaseModel):
    events: list[AuditEventRow]
    next_before_seq: int | None = None
    """Where the next page starts, or None at the end. Pages can be short, so follow it."""


@dataclass(frozen=True)
class _Reach:
    """What one audit subject resolves to, for the scope comparison."""

    department_id: uuid.UUID | None
    system_owner_user_id: uuid.UUID | None


_UNRESOLVED = _Reach(None, None)
"""A subject with no department or owner: hidden from narrow callers, shown to wide ones."""

_USER_SUBJECT = "user"
_GRANT_SUBJECT = "grant"
_REQUEST_SUBJECT = "access_request"
_REVIEW_SUBJECT = "access_review"
_REVIEW_ITEM_SUBJECT = "review_item"

_RESOLVED_SUBJECT_TYPES = frozenset(
    {_USER_SUBJECT, _GRANT_SUBJECT, _REQUEST_SUBJECT, _REVIEW_SUBJECT, _REVIEW_ITEM_SUBJECT}
)


def _as_uuid(value: str | None) -> uuid.UUID | None:
    """A subject id as a uuid, or None. Some subjects, like a tool name, are not ids."""
    if value is None:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


async def _resolve_subjects(
    session: AsyncSession, events: Sequence[AuditEvent]
) -> dict[tuple[str, str], _Reach]:
    """Map each event's subject to a department and system owner, in batched reads.

    Every read is tenant-filtered, so a foreign id comes back missing and its
    row stays hidden from a narrow caller.
    """
    # Keyed by the subject id AS STORED, not by the parsed uuid's canonical
    # string: `subject_id` is a free text column, and a row written in a
    # different case would otherwise resolve to nothing and be withheld from
    # every narrow caller.
    wanted: dict[tuple[str, str], uuid.UUID] = {
        (row.subject_type, row.subject_id): subject_uuid
        for row in events
        if row.subject_type in _RESOLVED_SUBJECT_TYPES
        and row.subject_id is not None
        and (subject_uuid := _as_uuid(row.subject_id)) is not None
    }
    if not wanted:
        return {}

    def ids_of(subject_type: str) -> set[uuid.UUID]:
        return {value for (kind, _), value in wanted.items() if kind == subject_type}

    # Stage 1: the two indirections, resolved to the rows they point at.
    items = await Repository(session, ReviewItem).get_many(ids_of(_REVIEW_ITEM_SUBJECT))
    review_ids = ids_of(_REVIEW_SUBJECT) | {item.review_id for item in items.values()}
    reviews = await Repository(session, AccessReview).get_many(review_ids)
    grants = await Repository(session, Grant).get_many(ids_of(_GRANT_SUBJECT))
    requests = await Repository(session, AccessRequest).get_many(ids_of(_REQUEST_SUBJECT))

    # Stage 2: the directory rows those all bottom out in.
    user_ids = (
        ids_of(_USER_SUBJECT)
        | {grant.user_id for grant in grants.values()}
        | {request.target_user_id for request in requests.values()}
    )
    users = await Repository(session, User).get_many(user_ids)
    role_ids = {grant.role_id for grant in grants.values()} | {
        request.role_id for request in requests.values()
    }
    roles = await Repository(session, Role).get_many(role_ids)
    system_ids = {role.system_id for role in roles.values()} | {
        review.system_id for review in reviews.values() if review.system_id is not None
    }
    systems = await Repository(session, System).get_many(system_ids)

    def department_of(user_id: uuid.UUID | None) -> uuid.UUID | None:
        user = None if user_id is None else users.get(user_id)
        return None if user is None else user.department_id

    def owner_of_role(role_id: uuid.UUID | None) -> uuid.UUID | None:
        role = None if role_id is None else roles.get(role_id)
        system = None if role is None else systems.get(role.system_id)
        return None if system is None else system.owner_user_id

    def reach_of_review(review: AccessReview | None) -> _Reach:
        if review is None:
            return _UNRESOLVED
        system = None if review.system_id is None else systems.get(review.system_id)
        return _Reach(
            department_id=review.department_id,
            system_owner_user_id=None if system is None else system.owner_user_id,
        )

    resolved: dict[tuple[str, str], _Reach] = {}
    for key, subject_uuid in wanted.items():
        subject_type = key[0]
        if subject_type == _USER_SUBJECT:
            resolved[key] = _Reach(department_of(subject_uuid), None)
        elif subject_type == _GRANT_SUBJECT:
            grant = grants.get(subject_uuid)
            resolved[key] = (
                _UNRESOLVED
                if grant is None
                else _Reach(department_of(grant.user_id), owner_of_role(grant.role_id))
            )
        elif subject_type == _REQUEST_SUBJECT:
            request = requests.get(subject_uuid)
            resolved[key] = (
                _UNRESOLVED
                if request is None
                else _Reach(department_of(request.target_user_id), owner_of_role(request.role_id))
            )
        elif subject_type == _REVIEW_SUBJECT:
            resolved[key] = reach_of_review(reviews.get(subject_uuid))
        else:
            item = items.get(subject_uuid)
            resolved[key] = (
                _UNRESOLVED if item is None else reach_of_review(reviews.get(item.review_id))
            )
    return resolved


async def _request_numbers(session: AsyncSession, events: Sequence[AuditEvent]) -> dict[str, str]:
    """Each request subject's shown number, keyed by the subject id as stored.

    One tenant-filtered read for the page, so a foreign id comes back missing
    and gets no number.
    """
    wanted = {
        row.subject_id: subject_uuid
        for row in events
        if row.subject_type == _REQUEST_SUBJECT
        and row.subject_id is not None
        and (subject_uuid := _as_uuid(row.subject_id)) is not None
    }
    if not wanted:
        return {}
    requests = await Repository(session, AccessRequest).get_many(set(wanted.values()))
    return {
        subject_id: format_number(NumberKind.ACCESS_REQUEST, request.number)
        for subject_id, subject_uuid in wanted.items()
        if (request := requests.get(subject_uuid)) is not None
    }


def _audit_row(row: AuditEvent, numbers: dict[str, str]) -> AuditEventRow:
    return AuditEventRow(
        seq=row.seq,
        occurred_at=row.occurred_at,
        actor_kind=row.actor_kind,
        actor_ref=row.actor_ref,
        event_type=row.event_type,
        subject_type=row.subject_type,
        subject_id=row.subject_id,
        subject_number=(
            numbers.get(row.subject_id)
            if row.subject_type == _REQUEST_SUBJECT and row.subject_id is not None
            else None
        ),
        payload=dict(row.payload),
        hash=row.hash,
    )


async def search_audit(ctx: ToolContext, args: SearchAuditInput) -> SearchAuditOutput:
    """The caller's own tenant's audit log, newest first, within their scope.

    `authorize` is called again to get every scope; the pipeline keeps only the
    widest. The caller's department comes from the directory, not the token.
    """
    authorization = rbac.authorize(ctx.identity.claims, rbac.Tool.SEARCH_AUDIT)
    page = await search_audit_events(
        ctx.session,
        event_type=args.event_type,
        subject_type=args.subject_type,
        subject_id=args.subject_id,
        actor_ref=args.actor_ref,
        occurred_from=args.occurred_from,
        occurred_to=args.occurred_to,
        before_seq=args.before_seq,
    )

    if Scope.ANY in authorization.scopes:
        visible: list[AuditEvent] = list(page.events)
    else:
        caller = await ctx.directory.lookup_user(ctx.identity.user_id)
        caller_department_id = None if caller is None else caller.department_id
        reach = await _resolve_subjects(ctx.session, page.events)
        visible = []
        for row in page.events:
            subject = _UNRESOLVED
            if row.subject_id is not None:
                subject = reach.get((row.subject_type, row.subject_id), _UNRESOLVED)
            if request_in_scope(
                authorization.scopes,
                approver_user_id=ctx.identity.user_id,
                approver_department_id=caller_department_id,
                target_department_id=subject.department_id,
                system_owner_user_id=subject.system_owner_user_id,
            ):
                visible.append(row)

    # A page truncated by `limit` continues from the last row RETURNED, not
    # from the lowest row scanned: rows the caller has not seen yet must stay
    # reachable. Only a page that both fitted and exhausted the scan ends the
    # walk, which is what a `next_before_seq` of None means.
    if len(visible) > args.limit:
        returned = visible[: args.limit]
        next_before_seq: int | None = returned[-1].seq
    else:
        returned = visible
        next_before_seq = page.lowest_seq if page.has_more else None
    numbers = await _request_numbers(ctx.session, returned)
    return SearchAuditOutput(
        events=[_audit_row(row, numbers) for row in returned], next_before_seq=next_before_seq
    )
