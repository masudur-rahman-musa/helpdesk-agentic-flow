"""The joiner, mover and leaver tools.

Every id is looked up in the token's tenant, and a missing id and another
tenant's id give the same `NOT_FOUND`, so answers reveal nothing across
tenants. No tool takes a tenant argument.
"""

import re
import uuid
from datetime import date

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import String

from helpdesk.mcp_server.tools.base import MutatingToolInput
from helpdesk.mcp_server.tools.context import ToolContext
from helpdesk.shared.access.lifecycle import SkippedRole
from helpdesk.shared.access.offboarding import offboard_user as offboard_user_core
from helpdesk.shared.access.onboarding import onboard_user as onboard_user_core
from helpdesk.shared.access.transfers import transfer_user as transfer_user_core
from helpdesk.shared.core.errors import ErrorCode, NotFoundError
from helpdesk.shared.crud import Repository
from helpdesk.shared.models import Department, User

# Read off the columns, so the input check and the storage never disagree.
_EMAIL_TYPE = User.__table__.c.email.type
assert isinstance(_EMAIL_TYPE, String), _EMAIL_TYPE
assert _EMAIL_TYPE.length is not None, "users.email must be a sized column"
EMAIL_MAX_LENGTH: int = _EMAIL_TYPE.length

_DISPLAY_NAME_TYPE = User.__table__.c.display_name.type
assert isinstance(_DISPLAY_NAME_TYPE, String), _DISPLAY_NAME_TYPE
assert _DISPLAY_NAME_TYPE.length is not None, "users.display_name must be a sized column"
DISPLAY_NAME_MAX_LENGTH: int = _DISPLAY_NAME_TYPE.length

EMAIL_PATTERN = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
r"""Narrow on purpose: one line, one `@`, a dotted domain, no whitespace.

Used with `fullmatch`, so a trailing newline cannot ride into the directory.
"""


class OnboardUserInput(MutatingToolInput):
    """Extras are refused. Principals are not an input, so no caller can mint an `it_admin`."""

    email: str = Field(min_length=1, max_length=EMAIL_MAX_LENGTH)
    display_name: str = Field(min_length=1, max_length=DISPLAY_NAME_MAX_LENGTH)
    department_id: uuid.UUID
    start_date: date

    manager_user_id: uuid.UUID

    @field_validator("email")
    @classmethod
    def _email_is_one_line(cls, value: str) -> str:
        """`fullmatch`, not `pattern=`, which would let a trailing newline through."""
        if EMAIL_PATTERN.fullmatch(value) is None:
            raise ValueError("email must be a single-line address of the form name@domain.tld")
        return value


class SkippedRoleOutput(BaseModel):
    """A template role the engine refused, returned so the caller knows what to fix."""

    role_id: uuid.UUID
    reason_code: ErrorCode
    reason: str


class OnboardUserOutput(BaseModel):
    """The person created, what they hold, and what their template could not give."""

    user_id: uuid.UUID
    applied_role_ids: list[uuid.UUID]
    skipped: list[SkippedRoleOutput]


async def onboard_user(ctx: ToolContext, args: OnboardUserInput) -> OnboardUserOutput:
    """Hire one person into the token's tenant and apply their department template.

    The manager is checked even though no column stores it, because it goes
    into the audit event and must be real.
    """
    if await Repository(ctx.session, Department).get(args.department_id) is None:
        raise NotFoundError("no such department in this tenant")
    if await Repository(ctx.session, User).get(args.manager_user_id) is None:
        raise NotFoundError("no such manager in this tenant")

    result = await onboard_user_core(
        ctx.session,
        ctx.directory,
        email=args.email,
        display_name=args.display_name,
        department_id=args.department_id,
        start_date=args.start_date,
        manager_user_id=args.manager_user_id,
        created_by_user_id=ctx.identity.user_id,
        actor_ref=str(ctx.identity.user_id),
        session_id=ctx.identity.session_id,
        now=ctx.now,
    )
    return OnboardUserOutput(
        user_id=result.user.id,
        applied_role_ids=[grant.role_id for grant in result.applied],
        skipped=[_skipped(record) for record in result.skipped],
    )


class TransferUserInput(MutatingToolInput):
    """No "from" department: it is read off the person's row, never taken from the caller."""

    user_id: uuid.UUID
    new_department_id: uuid.UUID
    effective_date: date


class TransferUserOutput(BaseModel):
    """The diff, plus kept roles the new template covers and ones a review will flag."""

    from_department_id: uuid.UUID | None
    revoked_role_ids: list[uuid.UUID]
    applied_role_ids: list[uuid.UUID]
    skipped: list[SkippedRoleOutput]
    retained_role_ids: list[uuid.UUID]
    non_standard_role_ids: list[uuid.UUID]


async def transfer_user(ctx: ToolContext, args: TransferUserInput) -> TransferUserOutput:
    """Move one person to another department in the token's tenant, and diff."""
    if await Repository(ctx.session, Department).get(args.new_department_id) is None:
        raise NotFoundError("no such department in this tenant")

    result = await transfer_user_core(
        ctx.session,
        ctx.directory,
        user_id=args.user_id,
        new_department_id=args.new_department_id,
        effective_date=args.effective_date,
        transferred_by_user_id=ctx.identity.user_id,
        actor_ref=str(ctx.identity.user_id),
        session_id=ctx.identity.session_id,
        now=ctx.now,
    )
    return TransferUserOutput(
        from_department_id=result.from_department_id,
        revoked_role_ids=list(result.revoked_role_ids),
        applied_role_ids=list(result.applied_role_ids),
        skipped=[_skipped(record) for record in result.skipped],
        retained_role_ids=list(result.retained_role_ids),
        non_standard_role_ids=list(result.non_standard_role_ids),
    )


class OffboardUserInput(MutatingToolInput):
    """Nothing names what to revoke: offboarding takes back everything."""

    user_id: uuid.UUID
    end_date: date


class OffboardUserOutput(BaseModel):
    """Grants are counted as rows too, since one person can hold a role twice."""

    revoked_role_ids: list[uuid.UUID]
    revoked_grant_count: int
    closed_request_ids: list[uuid.UUID]


async def offboard_user(ctx: ToolContext, args: OffboardUserInput) -> OffboardUserOutput:
    """Mark one person in the token's tenant a leaver and take back their access."""
    result = await offboard_user_core(
        ctx.session,
        ctx.directory,
        user_id=args.user_id,
        end_date=args.end_date,
        offboarded_by_user_id=ctx.identity.user_id,
        actor_ref=str(ctx.identity.user_id),
        session_id=ctx.identity.session_id,
        now=ctx.now,
    )
    return OffboardUserOutput(
        revoked_role_ids=list(result.revoked_role_ids),
        revoked_grant_count=result.revoked_grant_count,
        closed_request_ids=list(result.closed_request_ids),
    )


def _skipped(record: SkippedRole) -> SkippedRoleOutput:
    """One engine refusal on the wire, shared so the tools cannot drift."""
    return SkippedRoleOutput(
        role_id=record.role_id, reason_code=record.reason_code, reason=record.reason
    )
