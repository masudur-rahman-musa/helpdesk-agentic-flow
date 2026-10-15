"""The read tools: `get_me`, `list_my_access` and `search_audit` (13b).

The first two are self-scoped. The user read is the one on the token, never one named
in the arguments: the input models declare no fields at all and
ignore extras, so a `user_id` argument is dropped rather than honoured and
rather than rejected. Both
`test_mcp_read_tools.py::test_get_me_ignores_user_id_and_tenant_id_in_its_arguments`
and its `list_my_access` sibling name a real user in the CALLER'S OWN tenant,
so honouring the argument returns a colleague's row rather than being caught
by tenant scoping.

`search_audit` is the exception to "self-scoped": the design gives it six cells,
and the two narrow ones are enforced here rather than in the matrix, because
an audit row carries no department and no system of its own. Its identity
handling is the same as the other two — the caller is the token's user, and
their department is read from the directory, never from a claim.

No function here opens a session or touches the transport: the pipeline binds
the session to the token's tenant before any of them is called.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import String
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.mcp_server.core.identity import Identity
from helpdesk.shared.auth import rbac
from helpdesk.shared.auth.rbac import Scope
from helpdesk.shared.core.errors import NotFoundError
from helpdesk.shared.crud import Repository
from helpdesk.shared.crud.grants import ActiveGrant, list_active_grants_for_user
from helpdesk.shared.crud.users import find_user_by_id
from helpdesk.shared.models import (
    AccessRequest,
    AccessReview,
    AuditEvent,
    Grant,
    ReviewItem,
    Role,
    System,
    User,
)
from helpdesk.shared.services.access.approvals import request_in_scope
from helpdesk.shared.services.access.directory import SimulatedDirectory
from helpdesk.shared.services.audit import search_audit_events

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


# --- search_audit ---------------------------------------------------
#
# The one read tool that is not self-scoped: the design gives `search_audit` six
# cells, four of them `Scope.ANY` and two narrow. An audit row carries no
# department and no system of its own, so the two narrow scopes are enforced
# by resolving each row's SUBJECT to a (department, system owner) pair and
# asking `request_in_scope` — the same authority the approval queue uses —
# whether the caller reaches it.

SEARCH_LIMIT_MIN = 1
SEARCH_LIMIT_DEFAULT = 50
SEARCH_LIMIT_MAX = 200
"""How many events one search may return.

Bounds on the RESPONSE, distinct from `audit.search.SCAN_CAP`, which bounds
the READ: the scope filter runs between the two, so a page can hold fewer
than `limit` rows while more matching events exist further back. All three
are pinned by
`test_mcp_search_audit.py::test_the_search_bounds_and_column_caps_are_exactly_their_values`,
the default again through the model by `::test_the_default_limit_is_read_off_the_input_model`,
and the inclusive ends by `::test_the_limit_boundary_is_inclusive_at_1_and_200`."""


def _column_length(name: str) -> int:
    """One `audit_events` column's declared length, read off the column.

    Never retyped: the pre-check and the storage it filters
    are the same number by construction, so a widened column cannot leave a
    stale cap behind. Pinned by
    `test_mcp_search_audit.py::test_each_string_filter_is_inclusive_at_its_column_cap_and_refused_one_over`,
    which computes both ends of every boundary off the column itself.
    """
    column_type = AuditEvent.__table__.c[name].type
    assert isinstance(column_type, String), column_type
    assert column_type.length is not None, f"audit_events.{name} must be a sized column"
    return column_type.length


EVENT_TYPE_MAX_LENGTH: int = _column_length("event_type")
SUBJECT_TYPE_MAX_LENGTH: int = _column_length("subject_type")
SUBJECT_ID_MAX_LENGTH: int = _column_length("subject_id")
ACTOR_REF_MAX_LENGTH: int = _column_length("actor_ref")

_MIN_SEQ = 1
"""`audit_events.seq` is a Postgres `Identity` starting at 1, so this is the
lowest cursor that can name a row. Pinned by
`test_mcp_search_audit.py::test_before_seq_is_accepted_at_1_and_refused_at_0`."""


class SearchAuditInput(BaseModel):
    """Decision D's eight filters, every one optional.

    No tenant and no user field: both come from the token. Each
    string cap is the column's own length, so a filter the store could never
    match is refused as `VALIDATION` before any read rather than by Postgres
    in words the boundary may not forward (the design).
    """

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
    """One stored event as the tool returns it (criterion 2).

    `payload` is returned rather than omitted (decision E): the design
    restricts telemetry, not the audit log, and the `auditor` principal
    exists to read exactly this. `hash` travels with the row so a reader can
    check it against `GET /admin/audit/verify` without a second query.
    """

    seq: int
    occurred_at: datetime
    actor_kind: str
    actor_ref: str | None
    event_type: str
    subject_type: str
    subject_id: str | None
    payload: dict[str, Any]
    hash: str


class SearchAuditOutput(BaseModel):
    events: list[AuditEventRow]
    next_before_seq: int | None = None
    """Where the next page starts, or None when this page reached the end.

    Not decoration: the scope filter runs after the read, so a narrow
    caller's page can be short or empty while matching events exist further
    back. This cursor is the only way to reach them.
    """


@dataclass(frozen=True)
class _Reach:
    """What one audit subject resolves to, for the scope comparison."""

    department_id: uuid.UUID | None
    system_owner_user_id: uuid.UUID | None


_UNRESOLVED = _Reach(None, None)
"""A subject that resolves to neither a department nor a system owner.

`request_in_scope` reaches neither with `own_department` nor `own_systems`,
so such a row is withheld from a narrow caller and returned to a `Scope.ANY`
one — the fail-closed direction an earlier change §2 states for `subject_type="tool"`,
a null `subject_id` and any subject type a later plan adds without extending
the table below. Pinned by
`test_mcp_search_audit.py::test_an_unresolvable_subject_is_withheld_from_a_manager_and_returned_to_an_auditor`.
"""

_USER_SUBJECT = "user"
_GRANT_SUBJECT = "grant"
_REQUEST_SUBJECT = "access_request"
_REVIEW_SUBJECT = "access_review"
_REVIEW_ITEM_SUBJECT = "review_item"

_RESOLVED_SUBJECT_TYPES = frozenset(
    {_USER_SUBJECT, _GRANT_SUBJECT, _REQUEST_SUBJECT, _REVIEW_SUBJECT, _REVIEW_ITEM_SUBJECT}
)
"""An earlier change §2's table, as the allowlist it is. Everything outside it is
`_UNRESOLVED`."""


def _as_uuid(value: str | None) -> uuid.UUID | None:
    """A subject id as a uuid, or None for a null or non-uuid one.

    `subject_id` is a free string column, so a subject that never was an id
    (the tool name the pipeline files its own events against) must resolve to
    nothing rather than raise.
    """
    if value is None:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


async def _resolve_subjects(
    session: AsyncSession, events: Sequence[AuditEvent]
) -> dict[tuple[str, str], _Reach]:
    """An earlier change §2's table, applied to a whole page in batched reads.

    Seven statements at most, each skipped when its id set is empty, rather
    than one round trip per row: the read above is bounded by `SCAN_CAP`, so
    a per-row lookup would be up to that many queries for a single narrow
    caller's page.

    Every read goes through `Repository`, so the tenant-bound session filters
    each one; a subject id belonging to another tenant is simply absent from
    the result and its row resolves to `_UNRESOLVED`, which withholds it from
    a narrow caller.
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


def _audit_row(row: AuditEvent) -> AuditEventRow:
    return AuditEventRow(
        seq=row.seq,
        occurred_at=row.occurred_at,
        actor_kind=row.actor_kind,
        actor_ref=row.actor_ref,
        event_type=row.event_type,
        subject_type=row.subject_type,
        subject_id=row.subject_id,
        payload=dict(row.payload),
        hash=row.hash,
    )


async def search_audit(
    session: AsyncSession, identity: Identity, args: SearchAuditInput
) -> SearchAuditOutput:
    """The caller's own tenant's audit log, newest first, within their scope.

    The `authorize` call here is not a second permission gate — the pipeline
    already ran the matrix and a caller with no cell never reaches this
    function (a project rule: the two copies of that decision cannot be
    killed separately). It is how the handler gets `scopes`, the set the
    pipeline's own check collapses to its widest member, which for a caller
    who is both a `manager` and a `system_owner` would drop the owned systems
    entirely.

    `Scope.ANY` short-circuits the whole resolution, so the four wide
    principals pay nothing for the two narrow ones. Every other caller has
    their department read from the DIRECTORY, never from the token's
    `department_id` claim — a manager transferred after their token was
    issued searches their new department, which
    `test_mcp_search_audit.py::test_the_managers_department_comes_from_the_directory_not_the_token`
    pins by minting the token before the move.
    """
    authorization = rbac.authorize(identity.claims, rbac.Tool.SEARCH_AUDIT)
    page = await search_audit_events(
        session,
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
        caller = await SimulatedDirectory(session).lookup_user(identity.user_id)
        caller_department_id = None if caller is None else caller.department_id
        reach = await _resolve_subjects(session, page.events)
        visible = []
        for row in page.events:
            subject = _UNRESOLVED
            if row.subject_id is not None:
                subject = reach.get((row.subject_type, row.subject_id), _UNRESOLVED)
            if request_in_scope(
                authorization.scopes,
                approver_user_id=identity.user_id,
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
    return SearchAuditOutput(
        events=[_audit_row(row) for row in returned], next_before_seq=next_before_seq
    )
