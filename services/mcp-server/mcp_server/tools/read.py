"""The read tools an earlier change registers: `get_me` and `list_my_access`.

Both are self-scoped. The user read is the one on the token, never one named
in the arguments: the input models declare no fields at all and
ignore extras, so a `user_id` argument is dropped rather than honoured and
rather than rejected. Both
`test_mcp_read_tools.py::test_get_me_ignores_user_id_and_tenant_id_in_its_arguments`
and its `list_my_access` sibling name a real user in the CALLER'S OWN tenant,
so honouring the argument returns a colleague's row rather than being caught
by tenant scoping.

Neither function opens a session or touches the transport: the pipeline binds
the session to the token's tenant before either is called.
"""

import uuid
from datetime import UTC, datetime

from helpdesk.core.repository import ActiveGrant, find_user_by_id, list_active_grants_for_user
from helpdesk.errors import NotFoundError
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_server.identity import Identity

_IGNORE_EXTRAS = ConfigDict(extra="ignore")
"""Extra arguments are dropped, never refused.

Identity arrives on the token, so an argument naming a user is data to
discard, not a reason to fail the call. Pinned by
`test_mcp_pipeline.py::test_extra_arguments_are_ignored_not_rejected`."""


class GetMeInput(BaseModel):
    """No fields: everything `get_me` needs is on the token."""

    model_config = _IGNORE_EXTRAS


class GetMeOutput(BaseModel):
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    email: str
    display_name: str
    department_id: uuid.UUID | None
    principals: list[str]
    status: str


class ListMyAccessInput(BaseModel):
    """No fields: the grants listed are the token user's."""

    model_config = _IGNORE_EXTRAS


class ListMyAccessOutput(BaseModel):
    grants: list[ActiveGrant]


async def get_me(session: AsyncSession, identity: Identity, args: GetMeInput) -> GetMeOutput:
    """The token user's own directory record.

    A `sub` naming a user in another tenant reads as not-found, because the
    session is bound to the token's tenant: same answer as a user that does
    not exist, so not-found cannot be used as a cross-tenant oracle.
    """
    user = await find_user_by_id(session, identity.user_id)
    if user is None:
        raise NotFoundError("no such user in this tenant")
    return GetMeOutput(
        user_id=user.id,
        tenant_id=user.tenant_id,
        email=user.email,
        display_name=user.display_name,
        department_id=user.department_id,
        principals=list(user.principals),
        status=user.status,
    )


async def list_my_access(
    session: AsyncSession, identity: Identity, args: ListMyAccessInput
) -> ListMyAccessOutput:
    """Every grant the token user holds right now.

    "Right now" is read here rather than passed in, so a caller cannot ask
    what they held last year (or will hold next year) through this tool.
    """
    grants = await list_active_grants_for_user(session, identity.user_id, datetime.now(UTC))
    return ListMyAccessOutput(grants=list(grants))
