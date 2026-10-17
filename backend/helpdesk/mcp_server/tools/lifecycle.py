"""The lifecycle tools: the design's joiner, mover and leaver (requirement #15).

`onboard_user` is the first tool that creates a PERSON rather than access.
Everything it decides lives in `helpdesk.access.lifecycle`; what lives here
is the wire contract and the two resolutions the domain cannot make on its
own — `department_id` and `manager_user_id` are looked up in the token's
tenant first, and either absence answers the same `NOT_FOUND` (the same
reasoning `revoke_access` carries for a grant id: an argument that told
"missing" from "another tenant's" apart would be a cross-tenant existence
oracle).

There is no tenant argument and no principals argument. The tenant is the
token's, and a joiner's principals are the fixed
`JOINER_PRINCIPALS` constant, because an HR caller able to name them could
mint an `it_admin` through a lifecycle tool (decision D).

`transfer_user` is the mover, and it resolves the SAME way: the
`user_id` and the `new_department_id` are looked up in the token's tenant and
either absence answers the same `NOT_FOUND`. It takes no tenant argument
either, and no department for the person to come FROM — where they are now is
read off their row, never taken from the caller.

`offboard_user` is the leaver, and it resolves the LEAST: its
only id is the person, and `lock_user` inside the domain call answers the
same `NOT_FOUND` for a missing user and another tenant's, so this module
looks nothing up before calling. It takes no tenant argument either.

The registry declares the GENERIC `tool.succeeded` for all three tools, not
`user.onboarded`, `user.transferred` or `user.offboarded` (12a decision F,
12b decision C, 12d): each handler appends its own the design event naming
the PERSON as the subject, and the pipeline's event lands last as the
summary — the same shape as an earlier change's `grant.revoked`.

No function here opens a session or touches the transport: the pipeline binds
the session to the token's tenant before the handler is called.
"""

import re
import uuid
from datetime import date

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import String

from helpdesk.mcp_server.tools.base import MutatingToolInput
from helpdesk.mcp_server.tools.context import ToolContext
from helpdesk.shared.core.errors import ErrorCode, NotFoundError
from helpdesk.shared.crud import Repository
from helpdesk.shared.models import Department, User
from helpdesk.shared.access.lifecycle import SkippedRole
from helpdesk.shared.access.offboarding import offboard_user as offboard_user_core
from helpdesk.shared.access.onboarding import onboard_user as onboard_user_core
from helpdesk.shared.access.transfers import transfer_user as transfer_user_core

# Read off the columns, never retyped: the check and the
# storage are the same number by construction. Both are asserted inclusively
# at their exact values by `test_mcp_onboard_user.py::
# test_the_input_bounds_are_inclusive_and_reject_before_any_effect`, which
# also compares them against the literals 320 and 256.
_EMAIL_TYPE = User.__table__.c.email.type
assert isinstance(_EMAIL_TYPE, String), _EMAIL_TYPE
assert _EMAIL_TYPE.length is not None, "users.email must be a sized column"
EMAIL_MAX_LENGTH: int = _EMAIL_TYPE.length

_DISPLAY_NAME_TYPE = User.__table__.c.display_name.type
assert isinstance(_DISPLAY_NAME_TYPE, String), _DISPLAY_NAME_TYPE
assert _DISPLAY_NAME_TYPE.length is not None, "users.display_name must be a sized column"
DISPLAY_NAME_MAX_LENGTH: int = _DISPLAY_NAME_TYPE.length

EMAIL_PATTERN = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
r"""What an address may look like, deliberately narrow rather than RFC-complete.

Used with `re.fullmatch` and never `match` or `search`: the
class excludes every whitespace character, so a trailing `"\n"`, `"\r\n"` or
`" "` cannot ride into the directory on the end of an otherwise valid
address, and neither can an inner space. All four rejections plus the
accepted baseline are in the input table of
`test_mcp_onboard_user.py::test_the_input_bounds_are_inclusive_and_reject_before_any_effect`;
a non-anchored match accepts the three trailing cases, so that table is what
tells `fullmatch` from `match` here.

The address is the login identifier and a unique key per tenant, so what it
must guarantee is a single line with one `@` and a dotted domain, not
deliverability — a permissive RFC 5322 pattern would admit exactly the
control characters this exists to keep out.
"""


class OnboardUserInput(MutatingToolInput):
    """the design's signature. `extra="forbid"` comes from `MutatingToolInput`.

    Forbidding extras is the point on a lifecycle mutation: a `tenant_id` in
    the arguments is a caller who believes they are hiring into another
    tenant, and dropping it silently would create the person in the TOKEN's
    tenant under a success the caller reads as something else. Pinned by
    `test_mcp_onboard_user.py::test_an_unknown_field_is_refused_with_no_user`.
    """

    email: str = Field(min_length=1, max_length=EMAIL_MAX_LENGTH)
    display_name: str = Field(min_length=1, max_length=DISPLAY_NAME_MAX_LENGTH)
    department_id: uuid.UUID
    start_date: date
    """A real date, typed rather than a string: `"2026-13-01"` and
    `"not-a-date"` are `VALIDATION` before any row exists, and the joiner's
    tenure is what the engine's new-joiner rule reads."""

    manager_user_id: uuid.UUID

    @field_validator("email")
    @classmethod
    def _email_is_one_line(cls, value: str) -> str:
        """`re.fullmatch`, and deliberately NOT Pydantic's `pattern=`.

        `Field(pattern=...)` is a PARTIAL match: it accepts any string the
        pattern matches somewhere, so `"a@b.c\n"`, `"a@b.c\r\n"` and
        `"a@b.c "` all pass it and the address reaches the directory carrying
        a control character. Those three cases plus an inner space are in
        `test_mcp_onboard_user.py::test_the_input_bounds_are_inclusive_and_reject_before_any_effect`,
        and each one of them fails if this validator is replaced by
        `pattern=EMAIL_PATTERN` — which is how this landed here.
        """
        if EMAIL_PATTERN.fullmatch(value) is None:
            raise ValueError("email must be a single-line address of the form name@domain.tld")
        return value


class SkippedRoleOutput(BaseModel):
    """One template role the engine refused, and the the design code it gave.

    Part of the tool's answer rather than a log line (decision B): a template
    naming two roles that cannot be held together is a data defect somebody
    has to fix, and a caller who never learns which role was withheld cannot
    fix it.
    """

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

    Both ids are resolved through the tenant-bound repository BEFORE any
    write, and both absences answer the identical `NOT_FOUND` — missing, and
    belonging to another tenant, are indistinguishable at this seam, pinned
    for each id separately by
    `test_mcp_onboard_user.py::test_a_missing_and_another_tenants_department_answer_the_same_not_found`
    and `::test_a_missing_and_another_tenants_manager_answer_the_same_not_found`.
    The manager is validated even though `users` has no manager column to
    write it to (decision E): it is carried into the `user.onboarded` payload,
    which is exactly the argument an implementation is most likely to accept
    unchecked and file a manager who does not exist.

    the design's matrix row is the only permission gate on this tool, and it runs
    in the pipeline: onboarding takes no target the caller might or might not
    reach, so there is no second, argument-dependent check of the kind
    `create_access_request` needs. `test_the_other_four_principals_are_denied_and_write_no_user`
    sends otherwise-valid arguments for all four refused principals, so
    deleting that stage is the one change that turns those cases into created
    users.

    Everything runs in the transaction the pipeline opened, so the person,
    their grants and every event commit or roll back together.
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
    """the design's mover signature. `extra="forbid"` comes from `MutatingToolInput`.

    Three arguments and no fourth: there is no tenant and no
    department to move FROM, because where the person is now is read off their
    row — a caller who could name it could diff a template the person was never
    in. A `tenant_id` in the arguments is refused rather than dropped, pinned by
    `test_mcp_transfer_user.py::test_an_unknown_field_is_refused_with_no_department_change`.
    """

    user_id: uuid.UUID
    new_department_id: uuid.UUID
    effective_date: date
    """A real date, typed rather than a string. It reaches the
    `user.transferred` payload and nothing parses it afterwards (decision G:
    there is no column and no scheduler until row 21), so `"2026-13-01"` and
    `"2026-09-31"` typed as `str` would become permanent audit records nobody
    can read. Both are `VALIDATION` before any effect, pinned by
    `test_mcp_transfer_user.py::test_the_input_types_are_refused_before_any_effect`."""


class TransferUserOutput(BaseModel):
    """The four sets of the diff, plus what the engine refused.

    `retained_role_ids` and `non_standard_role_ids` name roles nothing was
    done to, and they are answered rather than left implicit: they are the only
    thing that tells a caller which kept grants their new department's template
    accounts for and which an access review will later flag (decision H).
    """

    from_department_id: uuid.UUID | None
    revoked_role_ids: list[uuid.UUID]
    applied_role_ids: list[uuid.UUID]
    skipped: list[SkippedRoleOutput]
    retained_role_ids: list[uuid.UUID]
    non_standard_role_ids: list[uuid.UUID]


async def transfer_user(ctx: ToolContext, args: TransferUserInput) -> TransferUserOutput:
    """Move one person to another department in the token's tenant, and diff.

    The destination is resolved through the tenant-bound repository BEFORE any
    write, and `lock_user` inside the domain call resolves the person the same
    way, so both absences — missing, and belonging to another tenant — answer
    the identical `NOT_FOUND` at each id. Pinned separately by
    `test_mcp_transfer_user.py::test_a_missing_and_another_tenants_user_answer_the_same_not_found`
    and `::test_a_missing_and_another_tenants_department_answer_the_same_not_found`,
    the second over a REAL foreign department, because a lookup outside the
    guard would write another tenant's department id onto this tenant's user.

    the design's matrix row is the only permission gate on this tool, and it runs
    in the pipeline: all three permitted principals hold `Scope.ANY`, so unlike
    `revoke_access` there is no second, argument-dependent check.
    `test_the_other_four_principals_are_denied_and_move_nobody` sends fully
    valid arguments for all four refused principals, so deleting that stage is
    the one change that turns those cases into completed transfers (standing
    rule 16).

    Everything runs in the transaction the pipeline opened, so the department,
    the revocations, the grants and every event commit or roll back together.
    """
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
    """the design's leaver signature. `extra="forbid"` comes from `MutatingToolInput`.

    Two arguments and no third: there is no tenant and nothing
    naming what to revoke, because an offboard takes back everything the
    person holds — a caller able to name a subset could offboard somebody on
    paper and leave their access alive. A `tenant_id` in the arguments is
    refused rather than dropped, pinned by
    `test_mcp_offboard_user.py::test_an_unknown_field_is_refused_with_no_state_change`.
    """

    user_id: uuid.UUID
    end_date: date
    """A real date, typed rather than a string. It is written straight into
    `users.end_date` and rendered into the `user.offboarded` payload, so
    `"2026-13-01"` and `"2026-09-31"` typed as `str` would reach a column and
    a permanent audit record. Both are `VALIDATION` before any effect, pinned
    by `test_mcp_offboard_user.py::test_the_input_types_are_refused_before_any_effect`."""


class OffboardUserOutput(BaseModel):
    """What the departure took away and what it closed.

    `revoked_grant_count` counts ROWS and `revoked_role_ids` counts ROLES:
    nothing makes a grant unique per (user, role), so a caller told only the
    roles cannot tell how many live grants ended (an earlier change's reason, answered
    here in the response rather than left to the audit log).
    """

    revoked_role_ids: list[uuid.UUID]
    revoked_grant_count: int
    closed_request_ids: list[uuid.UUID]


async def offboard_user(ctx: ToolContext, args: OffboardUserInput) -> OffboardUserOutput:
    """Mark one person in the token's tenant a leaver and take back their access.

    Nothing is resolved here: the only id is the person, and `lock_user`
    inside the domain call reads through the tenant-bound session, so missing
    and belonging to another tenant answer the identical `NOT_FOUND` (pinned
    by `test_mcp_offboard_user.py::
    test_a_missing_and_another_tenants_user_answer_the_same_not_found`, which
    re-reads the foreign user's status and live roles afterwards).

    the design's matrix row is the only permission gate on this tool, and it runs
    in the pipeline: both permitted principals hold `Scope.ANY`, so unlike
    `revoke_access` there is no second, argument-dependent check. The row is
    narrower than the other two lifecycle tools — `it_admin` may onboard and
    transfer but NOT offboard — and
    `test_the_other_five_principals_are_denied_and_offboard_nobody` sends
    fully valid arguments for all five refused principals, so deleting that
    stage is the one change that turns those cases into completed departures.

    Everything runs in the transaction the pipeline opened, so the status, the
    revocations, the closures and every event commit or roll back together.
    """
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
    """One domain refusal on the wire. Shared so the two tools cannot drift."""
    return SkippedRoleOutput(
        role_id=record.role_id, reason_code=record.reason_code, reason=record.reason
    )
