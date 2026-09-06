"""The lifecycle tools: the design's joiner half.

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

The registry declares the GENERIC `tool.succeeded` for this tool, not
`user.onboarded` (decision F): the handler appends the hire itself, naming
the new user as the subject, and the pipeline's event lands last as the
summary — the same shape as an earlier change's `grant.revoked`.

No function here opens a session or touches the transport: the pipeline binds
the session to the token's tenant before the handler is called.
"""

import re
import uuid
from datetime import UTC, date, datetime

from helpdesk.access import SimulatedDirectory
from helpdesk.access import onboard_user as onboard_user_core
from helpdesk.core.repository import Repository
from helpdesk.errors import ErrorCode, NotFoundError
from helpdesk.models import Department, User
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import String
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_server.identity import Identity
from mcp_server.tools.base import MutatingToolInput

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


async def onboard_user(
    session: AsyncSession, identity: Identity, args: OnboardUserInput
) -> OnboardUserOutput:
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
    if await Repository(session, Department).get(args.department_id) is None:
        raise NotFoundError("no such department in this tenant")
    if await Repository(session, User).get(args.manager_user_id) is None:
        raise NotFoundError("no such manager in this tenant")

    result = await onboard_user_core(
        session,
        SimulatedDirectory(session),
        email=args.email,
        display_name=args.display_name,
        department_id=args.department_id,
        start_date=args.start_date,
        manager_user_id=args.manager_user_id,
        created_by_user_id=identity.user_id,
        actor_ref=str(identity.user_id),
        session_id=identity.session_id,
        now=datetime.now(UTC),
    )
    return OnboardUserOutput(
        user_id=result.user.id,
        applied_role_ids=[grant.role_id for grant in result.applied],
        skipped=[
            SkippedRoleOutput(
                role_id=record.role_id, reason_code=record.reason_code, reason=record.reason
            )
            for record in result.skipped
        ],
    )
