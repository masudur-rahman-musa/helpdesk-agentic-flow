"""Tenant-scoped repository helpers: the only DB path for services.

A `Repository` wraps a session produced by `tenant_session` and never takes
a tenant id as an argument: reads are filtered by the session guard in
`helpdesk.core.db`, and `create` stamps the bound tenant. Ids belonging to
another tenant behave as not-found (`get`/`update` return None, `delete`
returns False) — never the row, never a cross-tenant effect.
"""

import uuid
from collections.abc import Collection, Sequence
from datetime import datetime
from typing import Any, cast

from pydantic import BaseModel, ConfigDict
from sqlalchemy import ColumnElement, CursorResult, Delete, delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.core.db import bound_tenant_id
from helpdesk.models import (
    Department,
    Grant,
    IdempotencyKey,
    Message,
    MessageRole,
    Role,
    Session,
    System,
    Tenant,
    TenantScoped,
    User,
)
from helpdesk.models.conversation import SessionConflictError


async def find_tenant_by_slug(session: AsyncSession, slug: str) -> Tenant | None:
    """Resolve a tenant by its slug, or None.

    Tenants are the scoping root, not tenant-scoped rows, so `Repository`
    cannot fetch them; this helper exists so services still never build
    their own queries (login resolves the slug before any tenant is bound).
    """
    result = await session.execute(select(Tenant).where(Tenant.slug == slug))
    return result.scalars().one_or_none()


async def find_tenant_by_id(session: AsyncSession, tenant_id: uuid.UUID) -> Tenant | None:
    """Resolve a tenant by its id, or None.

    The sibling of `find_tenant_by_slug`, and here for the same reason: a
    tenant is the scoping root rather than a tenant-scoped row, so
    `Repository` cannot fetch one and a service would otherwise build the
    statement itself.

    The caller passes the id off a verified token, never off an argument. `search_policy` uses it to turn `identity.tenant_id` into
    the slug its collection name is built from; pinned by
    `test_mcp_search_policy.py::test_the_collection_searched_is_the_tokens_and_an_extra_tenant_id_is_dropped`,
    whose caller supplies the OTHER seeded tenant's id as an argument and
    still searches its own collection.
    """
    result = await session.execute(select(Tenant).where(Tenant.id == tenant_id))
    return result.scalars().one_or_none()


async def list_tenant_ids(session: AsyncSession) -> Sequence[uuid.UUID]:
    """Every tenant id, oldest slug first.

    The third of the tenant-root helpers, here for the same reason as its two
    siblings: a tenant is not a tenant-scoped row, so `Repository` cannot
    reach one and the scheduler would otherwise build the statement itself.
    It is the ONLY read in this codebase that is deliberately not scoped to
    one tenant — the scheduler's tick has no caller and no token, and its
    whole job is to visit every tenant in turn and open a bound session for
    each. Nothing tenant-scoped is read here: the ids alone.

    Ordered by slug so two ticks visit the tenants in the same order, which
    is what makes a failure in the middle of a sweep reproducible.
    """
    result = await session.execute(select(Tenant.id).order_by(Tenant.slug))
    return list(result.scalars().all())


async def find_user_by_email(session: AsyncSession, email: str) -> User | None:
    """The bound tenant's user with this email, or None.

    Takes a `tenant_session`: the session guard injects the tenant filter,
    so a matching email in another tenant behaves as not-found. Lives here
    so login (and later rows) never build their own query.
    """
    result = await session.execute(select(User).where(User.email == email))
    return result.scalars().one_or_none()


async def find_user_by_id(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    """The bound tenant's user with this id, or None.

    A user id belonging to another tenant reads as not-found, because the
    session guard adds the tenant filter: the id is not a capability. Pinned
    by `test_mcp_read_tools.py::test_a_tenant_b_token_cannot_read_a_tenant_a_user`.
    """
    result = await session.execute(select(User).where(User.id == user_id))
    return result.scalars().one_or_none()


async def lock_user(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    """The bound tenant's user, locked for the rest of this transaction.

    `SELECT … FOR UPDATE` through the ORM, so the tenant guard still injects
    its filter: a user id in another tenant reads exactly like one that does
    not exist (both None), and the id is not a cross-tenant existence oracle.

    The lock is what serialises two transfers of the same person (decision D).
    A transfer is a read-compute-write over the person AND their grants: it
    reads the department, diffs two templates against what they hold, then
    writes all three. `lock_live_grants_for_role` covers the revoke half only,
    so without this both callers read the old department and both apply the
    arriving role. The idempotency guard cannot help either, because two
    callers send two different keys. Pinned end to end by
    `test_mcp_transfer_user.py::test_two_callers_racing_the_same_transfer_leave_one_move`,
    whose mover holds nothing and whose old template is empty, so no
    revocation exists that could refuse the loser instead.
    """
    result = await session.execute(select(User).where(User.id == user_id).with_for_update())
    return result.scalars().one_or_none()


async def lock_department(session: AsyncSession, department_id: uuid.UUID) -> Department | None:
    """The bound tenant's department, locked for the rest of this transaction.

    `SELECT … FOR UPDATE` through the ORM, so the tenant guard still injects
    its filter: a department id in another tenant reads exactly like one that
    does not exist (both None), and the id is not a cross-tenant existence
    oracle. Pinned by
    `test_access_reviews.py::test_locking_a_scope_answers_the_same_way_for_another_tenants_row_and_a_missing_one`.

    The lock is what serialises two openings of the same review (decision G):
    opening is a read-compute-write — is a review already open for this
    (scope, period), and if not write one — and the idempotency guard cannot
    help, because two callers send two different keys. The loser waits, then
    re-reads the review the winner committed and is refused
    `ReviewAlreadyOpenError`. Pinned end to end by
    `test_mcp_reviews.py::test_two_callers_racing_the_same_review_leave_exactly_one`.
    """
    result = await session.execute(
        select(Department).where(Department.id == department_id).with_for_update()
    )
    return result.scalars().one_or_none()


async def lock_system(session: AsyncSession, system_id: uuid.UUID) -> System | None:
    """The bound tenant's system, locked for the rest of this transaction.

    The system-scoped twin of `lock_department`, with the same tenant-guard
    and serialisation properties; the two exist separately because a review
    over a department and a review over a system are different rows to hold,
    and one helper taking "whichever id is set" would have to re-decide the
    scope the caller already knows.
    """
    result = await session.execute(select(System).where(System.id == system_id).with_for_update())
    return result.scalars().one_or_none()


ACTIVE_GRANT_STATUS = "active"
"""The one `grants.status` value that can still be a live grant."""

REVOKED_GRANT_STATUS = "revoked"
"""What `grants.status` says once access was taken away.

Beside its sibling rather than in `helpdesk.access.revoke`, which is where
the plan's file list put it: the directory port writes the value and
`access.revoke` imports the port, so defining it there would close an import
cycle. `helpdesk.access.revoke` re-exports it, so the name is importable from
the module that owns the policy. Pinned to the exact string by
`test_access_revoke.py::test_revoking_marks_the_row_revoked_with_the_reason_and_timestamp`."""


class ActiveGrant(BaseModel):
    """One live grant, with the role and system named rather than referenced.

    A Pydantic model rather than a row tuple because this crosses a layer
    boundary (the MCP `list_my_access` tool renders it straight out), and
    ids alone would force every caller to re-read the directory.
    """

    model_config = ConfigDict(frozen=True)

    grant_id: uuid.UUID
    role_slug: str
    role_name: str
    privilege: str
    system_slug: str
    system_name: str
    granted_at: datetime
    expires_at: datetime | None


async def list_active_grants_for_user(
    session: AsyncSession, user_id: uuid.UUID, now: datetime
) -> Sequence[ActiveGrant]:
    """The bound tenant's live grants for one user, by system then role slug.

    The order is alphabetical, not chronological: a stable order a reader can
    scan, pinned by
    `test_mcp_read_tools.py::test_active_grants_come_back_in_system_then_role_order`.
    Nothing here promises "most recent first"; a caller that wants that sorts
    on `granted_at` itself.

    "Live" is three conjuncts, all of them here rather than in the caller:
    the status is `active`, nothing revoked it, and either it never expires
    or `expires_at` is still ahead. `expires_at` names the first instant the
    grant is gone, so the comparison is strict `>`; the microsecond pair in
    `test_mcp_read_tools.py::test_the_expiry_boundary_is_exclusive_at_expires_at`
    pins that, and one hidden fixture row per conjunct means dropping any one
    of the three fails a named case.

    Whole entities are selected, not columns: the tenant guard in
    `helpdesk.core.db` injects its filter per mapped entity, so every one of
    the three tables is scoped by the session rather than by this `where`.
    """
    result = await session.execute(
        select(Grant, Role, System)
        .join(Role, Role.id == Grant.role_id)
        .join(System, System.id == Role.system_id)
        .where(
            Grant.user_id == user_id,
            Grant.status == ACTIVE_GRANT_STATUS,
            Grant.revoked_at.is_(None),
            or_(Grant.expires_at.is_(None), Grant.expires_at > now),
        )
        .order_by(System.slug, Role.slug)
    )
    return [
        ActiveGrant(
            grant_id=grant.id,
            role_slug=role.slug,
            role_name=role.name,
            privilege=role.privilege,
            system_slug=system.slug,
            system_name=system.name,
            granted_at=grant.granted_at,
            expires_at=grant.expires_at,
        )
        for grant, role, system in result.all()
    ]


async def list_granted_role_ids(session: AsyncSession, user_id: uuid.UUID) -> frozenset[uuid.UUID]:
    """Every role this user holds under a review's definition of held.

    The per-holder twin of the two scope listings, and it uses the same
    `_granted_clauses` for the same reason: the SoD question a review asks is
    "should this person ever have held these two together", and a lapsed row
    the sweep has not touched still IS held. Asking the directory port's
    `list_account_role_ids` instead would drop exactly the lapsed halves, so a
    grant that completes a pair would read `non_standard` or `ok` — which is
    the flag a reviewer would then never look at (pinned by
    `test_access_reviews.py::test_a_grant_that_is_all_three_reads_sod_conflict`,
    whose conflicting grant has a past expiry).

    Unscoped by design: a system review sees one half of a pair and the other
    half is nearly always a role of a different system, so a conflict computed
    inside the review's scope would find nothing at all.
    """
    result = await session.execute(
        select(Grant.role_id).where(Grant.user_id == user_id, *_granted_clauses())
    )
    return frozenset(result.scalars().all())


def is_live_grant(grant: Grant, now: datetime) -> bool:
    """Is this already-loaded grant live as of `now`?

    The Python half of the three conjuncts every SQL liveness filter here
    spells out (`list_active_grants_for_user`, `lock_live_grants_for_role`,
    and the directory's `list_account_role_ids`): status `active`, nothing
    revoked it, and either it never expires or `expires_at` — the first
    instant the grant is gone — is still ahead, so the comparison is strict.

    It lives beside them rather than in the caller that needs it
    (`mcp_server.tools.access.revoke_access`, which has to answer for the ONE
    grant a caller named before locking the set) so the two answers cannot
    drift into a tool that refuses `POLICY_BLOCKED` for a grant every read
    still shows as live. That they agree is pinned by
    `test_access_revoke.py::test_the_python_liveness_predicate_agrees_with_the_locked_read`,
    which crosses the four states against `lock_live_grants_for_role` itself.
    """
    return (
        grant.status == ACTIVE_GRANT_STATUS
        and grant.revoked_at is None
        and (grant.expires_at is None or grant.expires_at > now)
    )


def is_active_grant(grant: Grant) -> bool:
    """Is this already-loaded grant ACTIVE in the review family's sense?

    The Python half of `_granted_clauses` below, and deliberately NOT of
    `is_live_grant` above: status `active` and unrevoked, with no expiry test
    at all. A grant whose expiry went by last week is still `active` until row
    21's sweep runs, and that lapsed row is exactly what a review flags
    `stale` and what a reviewer revokes.

    It lives beside its SQL twin for the same reason `is_live_grant` does:
    `helpdesk.access.reviews.decide_item` has to answer for the ONE grant an
    item names — is there anything left to revoke, or is this decision B's
    already-revoked row — and a second definition in that caller could drift
    into a review that reports a revocation it did not perform. Pinned by
    `test_access_reviews.py::test_a_grant_revoked_before_the_decision_is_recorded_and_revokes_nothing`
    (the False case, `revoked_grant_count` 0) and by
    `::test_a_stale_items_grant_is_revoked_although_no_live_set_holds_it`
    (the True case that `is_live_grant` answers the other way).
    """
    return grant.status == ACTIVE_GRANT_STATUS and grant.revoked_at is None


class ScopedGrant(BaseModel):
    """One live grant inside a review's scope, with what flagging it needs.

    A Pydantic model rather than a `Grant` row for the same reason as
    `ActiveGrant`: the flagging pass in `helpdesk.access.reviews` needs the
    holder's DEPARTMENT beside the grant, and returning ORM rows would make
    every caller re-read `users` per grant to get it.

    `expires_at` rides along because an earlier change's `stale` rule is entirely
    about it, and `user_id` because the SoD and template questions are asked
    per holder rather than per grant.
    """

    model_config = ConfigDict(frozen=True)

    grant_id: uuid.UUID
    user_id: uuid.UUID
    role_id: uuid.UUID
    holder_department_id: uuid.UUID | None
    expires_at: datetime | None


def _granted_clauses() -> tuple[Any, ...]:
    """What the design means by an ACTIVE grant: the row is `active` and unrevoked.

    Deliberately WITHOUT the expiry conjunct the liveness filters carry, and
    that is the whole point of the review family below.
    `grants.status` is `active | expired | revoked`, and nothing flips a row
    to `expired` until row 21's sweep runs, so a grant whose `expires_at` went
    by last week is still sitting there as `active`. That row is not "live"
    access to `list_my_access` — every liveness filter drops it — but it is
    exactly the drift a review exists to surface, and the seed plants it
    (`_STALE_EXPIRES_AT`, expired but still active).

    So a review's universe is wider than a liveness read by exactly the lapsed
    rows, and `stale` is the word it gives them. A review that used
    `_live_grant_clauses` instead could never produce a stale item at all
    (pinned by `test_access_reviews.py::test_an_already_past_expiry_is_stale`,
    which is what caught this).
    """
    return (Grant.status == ACTIVE_GRANT_STATUS, Grant.revoked_at.is_(None))


def _live_grant_clauses(now: datetime) -> tuple[Any, ...]:
    """The three conjuncts every liveness filter here spells out, in one place.

    Same as `is_live_grant`'s Python half: status `active`, nothing revoked
    it, and either it never expires or `expires_at` — the first instant the
    grant is gone — is still ahead, so the comparison is strict.
    """
    return (*_granted_clauses(), or_(Grant.expires_at.is_(None), Grant.expires_at > now))


async def count_live_grants(session: AsyncSession, now: datetime) -> int:
    """How many grants the bound tenant holds live as of `now`.

    A COUNT rather than a listing: the caller is
    `helpdesk.access.gauges.refresh_active_grants_gauge`, which wants one
    number every 300 seconds and would otherwise load every grant of every
    tenant into Python to call `len` on it.

    The filter is `_live_grant_clauses`, the same three conjuncts every other
    liveness read here spells out, which is what stops a fourth definition of
    "live" drifting into the number a dashboard shows. Its expiry test is
    strict, the exact complement of `access/expiry.py`'s `expires_at <= now`; the pair is pinned by
    `test_access_gauges.py::test_a_grant_expiring_at_exactly_now_is_not_counted`
    and `::test_a_grant_expiring_one_microsecond_after_now_is_counted`.

    The tenant clause is written OUT rather than left to the session guard.
    The guard injects its criteria through `with_loader_criteria`, which
    applies to the ENTITIES a statement loads, and this statement loads none —
    so relying on it here would be relying on an aggregate's mapper being
    detected. `bound_tenant_id` raises with no tenant bound, so the unbound
    case stays a loud failure rather than a full-table count, and the scoping
    is pinned by
    `test_access_gauges.py::test_another_tenants_live_grants_do_not_move_this_tenants_gauge`.
    """
    result = await session.execute(
        select(func.count(Grant.id)).where(
            Grant.tenant_id == bound_tenant_id(session), *_live_grant_clauses(now)
        )
    )
    return int(result.scalar_one())


async def _list_scoped_grants(
    session: AsyncSession, scope_clause: ColumnElement[bool], *, join_role: bool
) -> Sequence[ScopedGrant]:
    """The shared body of the two scope listings; the clause is the only difference.

    Whole entities are selected, not columns, so the tenant guard in
    `helpdesk.core.db` injects its filter per mapped entity: every table in
    the join is scoped by the session rather than by this `where`, which is
    what makes another tenant's department or system id answer with an empty
    scope instead of its grants.

    Ordered by the holder's address then `granted_at` then `id`, so a review
    reads grouped by person and two openings over the same rows produce the
    same item order.

    The filter is `_granted_clauses`, not `_live_grant_clauses`: a review's
    universe includes the lapsed-but-`active` rows, which is where its whole
    `stale` flag comes from. There is no `now` parameter for that reason —
    nothing here is clock-dependent, and taking one would invite the expiry
    conjunct back.
    """
    statement = select(Grant, User).join(User, User.id == Grant.user_id)
    if join_role:
        statement = statement.join(Role, Role.id == Grant.role_id)
    result = await session.execute(
        statement.where(scope_clause, *_granted_clauses()).order_by(
            User.email, Grant.granted_at, Grant.id
        )
    )
    return [
        ScopedGrant(
            grant_id=grant.id,
            user_id=grant.user_id,
            role_id=grant.role_id,
            holder_department_id=user.department_id,
            expires_at=grant.expires_at,
        )
        for grant, user in result.all()
    ]


async def list_active_grants_in_department(
    session: AsyncSession, department_id: uuid.UUID
) -> Sequence[ScopedGrant]:
    """Live grants held by the members of one department.

    Department scope is about PEOPLE: every grant of every user whose
    `department_id` is this one, whichever system the role belongs to. A user
    who transferred out yesterday is not in it even if the grant predates the
    move, because the review asks a manager about the people they have now.
    """
    return await _list_scoped_grants(session, User.department_id == department_id, join_role=False)


async def list_active_grants_for_system(
    session: AsyncSession, system_id: uuid.UUID
) -> Sequence[ScopedGrant]:
    """Live grants for the roles of one system.

    System scope is about ACCESS: every live grant whose role belongs to this
    system, whoever holds it and whichever department they are in. The two
    listings therefore overlap without either containing the other, which is
    why they are two functions rather than one filter.
    """
    return await _list_scoped_grants(session, Role.system_id == system_id, join_role=True)


async def lock_grant_row(session: AsyncSession, grant_id: uuid.UUID) -> Grant | None:
    """The bound tenant's grant, locked for the rest of this transaction.

    `SELECT … FOR UPDATE` through the ORM, so the tenant guard still injects
    its filter: a grant id in another tenant reads exactly like one that does
    not exist (both None), and the id is not a cross-tenant existence oracle.

    Deliberately UNFILTERED on liveness, unlike `lock_live_grants_for_role`
    below. A review item is one grant row and the most reviewable row in the
    tenant is a lapsed-but-`active` one, which every liveness filter drops; filtering here would make exactly the `stale` item
    the one a reviewer could not act on. Liveness is the CALLER's question
    afterwards, through `is_live_grant`.

    The lock is what serialises two decisions on one review item (decision I):
    deciding is a read-compute-write over the item and its grant, and two
    callers send two different idempotency keys, so the guard cannot. The
    grant row rather than the item row is held on purpose — the grant is what
    both the decision and the revocation touch, and
    `lock_live_grants_for_role` already locks grant rows, so one lock order
    across the two paths keeps a review decision and a concurrent
    `revoke_access` for the same person from deadlocking. Pinned end to end by
    `test_access_reviews.py::test_two_concurrent_deciders_leave_one_decided_item_and_one_refusal`.
    """
    result = await session.execute(select(Grant).where(Grant.id == grant_id).with_for_update())
    return result.scalars().one_or_none()


async def lock_live_grants_for_role(
    session: AsyncSession, user_id: uuid.UUID, role_id: uuid.UUID, now: datetime
) -> Sequence[Grant]:
    """The bound tenant's LIVE grants for one (user, role), locked for this
    transaction.

    "Live" is the same three conjuncts as `list_active_grants_for_user`, and
    the set is the unit rather than one row because nothing makes a grant
    unique per (user, role): two requests under two idempotency keys both
    execute, so revoking the row a caller named would leave access alive
    (pinned by
    `test_access_revoke.py::test_a_second_live_row_for_the_same_role_is_revoked_too`).

    `SELECT … FOR UPDATE` through the ORM, so the tenant guard still injects
    its filter and the rows are held until the caller's transaction ends. The
    lock is what serialises two revocations of the same role: two callers send
    two different idempotency keys, so the guard cannot (pinned by
    `test_mcp_revoke_access.py::test_two_callers_racing_the_same_revocation_write_one_event_per_row`).
    It covers rows that exist when it is taken; a grant committed afterwards is
    a new live grant, not a missed revocation.

    Ordered by `granted_at` then `id` so the revoked set reads oldest first and
    two runs over the same rows produce the same event order, pinned by
    `test_access_revoke.py::test_the_revoked_set_comes_back_oldest_first`
    (planted newest first, so an unordered read fails).
    """
    result = await session.execute(
        select(Grant)
        .where(
            Grant.user_id == user_id,
            Grant.role_id == role_id,
            Grant.status == ACTIVE_GRANT_STATUS,
            Grant.revoked_at.is_(None),
            or_(Grant.expires_at.is_(None), Grant.expires_at > now),
        )
        .order_by(Grant.granted_at, Grant.id)
        .with_for_update()
    )
    return result.scalars().all()


async def lock_live_grants_for_request(
    session: AsyncSession, request_id: uuid.UUID, now: datetime
) -> Sequence[Grant]:
    """The bound tenant's LIVE grants produced by one request, locked for this
    transaction.

    The unit a rollback undoes is a REQUEST, not a (user, role): the design
    says an executed request can be taken back, and one request can leave more
    than one row — an earlier change's onboarding executes a whole template through the
    same `execute_grant`. So the filter is `request_id`, and "live" is the same
    three conjuncts `lock_live_grants_for_role` spells out, from
    `_live_grant_clauses` so the two cannot drift into a rollback that revokes
    a row every liveness read already calls gone.

    `SELECT … FOR UPDATE` through the ORM, so the tenant guard still injects
    its filter and the rows are held until the caller's transaction ends. Two
    rollbacks of one request are already serialised a step earlier by
    `lock_request`; this lock holds the rows against a concurrent
    `revoke_access` on the same (user, role) for the rest of the transaction,
    so the set a rollback revokes cannot change under it between this read and
    the writes. Neither lock has a test that kills it on its own:
    `test_mcp_rollback_request.py::test_two_callers_racing_the_same_rollback_undo_it_once`
    pins the OUTCOME of two racing callers (one undo, one `POLICY_BLOCKED`,
    one event per row) and passes with either `with_for_update()` deleted,
    which its docstring says out loud rather than claiming a kill.

    Ordered by `granted_at` then `id`, so the revoked set reads oldest first
    and `helpdesk.access.rollback.rollback_request` — which takes `grants[0]`
    as the instant its 24h window is measured from — reads the EARLIEST grant
    of the request. Dropping the ORDER BY fails
    `test_mcp_rollback_request.py::test_the_window_is_measured_from_the_earliest_grant_of_the_request`,
    whose two rows are 25 hours and 1 hour old.
    """
    result = await session.execute(
        select(Grant)
        .where(Grant.request_id == request_id, *_live_grant_clauses(now))
        .order_by(Grant.granted_at, Grant.id)
        .with_for_update()
    )
    return result.scalars().all()


# Set by the session/mixin, never by callers: accepting them in **fields
# would let an update move a row across tenants behind the query guard.
_PROTECTED_FIELDS = frozenset({"id", "tenant_id"})


def _reject_protected(fields: dict[str, Any]) -> None:
    protected = _PROTECTED_FIELDS & fields.keys()
    if protected:
        raise ValueError(
            f"protected fields cannot be set through the repository: {sorted(protected)}"
        )


class Repository[M: TenantScoped]:
    def __init__(self, session: AsyncSession, model: type[M]) -> None:
        self._session = session
        self._model = model

    async def get(self, id: uuid.UUID, /) -> M | None:
        result = await self._session.execute(select(self._model).where(self._model.id == id))
        return result.scalars().one_or_none()

    async def get_many(self, ids: Collection[uuid.UUID], /) -> dict[uuid.UUID, M]:
        """The rows behind these ids, keyed by id, in ONE statement.

        The batched half of `get`, added by an earlier change: resolving a page of
        audit subjects one id at a time is a round trip per row against the
        one table the design never prunes. Ids belonging to another tenant
        are simply absent from the result, exactly as `get` returns None for
        one — the session guard filters the SELECT, so a caller cannot learn
        that a foreign id exists by finding it here.

        An empty `ids` runs no statement at all: `IN ()` is not valid SQL and
        a query for nothing has no answer worth a round trip.
        """
        if not ids:
            return {}
        result = await self._session.execute(
            select(self._model).where(self._model.id.in_(list(ids)))
        )
        return {row.id: row for row in result.scalars().all()}

    async def list(self) -> Sequence[M]:
        result = await self._session.execute(select(self._model))
        return result.scalars().all()

    async def create(self, **fields: Any) -> M:
        _reject_protected(fields)
        obj = self._model(tenant_id=bound_tenant_id(self._session), **fields)
        self._session.add(obj)
        await self._session.flush()
        return obj

    async def update(self, id: uuid.UUID, /, **fields: Any) -> M | None:
        _reject_protected(fields)
        obj = await self.get(id)
        if obj is None:
            return None
        for name, value in fields.items():
            setattr(obj, name, value)
        await self._session.flush()
        return obj

    async def delete(self, id: uuid.UUID, /) -> bool:
        obj = await self.get(id)
        if obj is None:
            return False
        await self._session.delete(obj)
        await self._session.flush()
        return True


# --- idempotency keys ---------------------------------------------
#
# `IdempotencyKey` is a `TenantScoped` model like any other, so these could be
# a plain `Repository`; they exist as named helpers because expiry is part of
# what "the key exists" means. A caller that fetched the row generically would
# have to remember to compare `expires_at` itself, and forgetting once means
# replaying a response the system no longer promises to honour.


async def create_idempotency_key(
    session: AsyncSession,
    *,
    key: str,
    tool_name: str,
    request_fingerprint: str,
    response: dict[str, Any],
    expires_at: datetime,
) -> IdempotencyKey:
    """Claim `key` for the bound tenant; the caller's transaction commits it."""
    return await Repository(session, IdempotencyKey).create(
        key=key,
        tool_name=tool_name,
        request_fingerprint=request_fingerprint,
        response=response,
        expires_at=expires_at,
    )


async def find_idempotency_key(
    session: AsyncSession, key: str, now: datetime
) -> IdempotencyKey | None:
    """The bound tenant's live row for `key`, or None.

    Expiry is honoured on read: a row whose `expires_at` has arrived is
    absent even though it is still on disk, so the key is free again before
    any purge has run. `expires_at` names the first instant it is gone.
    """
    result = await session.execute(
        select(IdempotencyKey).where(IdempotencyKey.key == key, IdempotencyKey.expires_at > now)
    )
    return result.scalars().one_or_none()


async def _delete_count(session: AsyncSession, statement: Delete) -> int:
    """Rows removed by an ORM DELETE.

    `AsyncSession.execute` is typed as returning a plain `Result`, which has
    no `rowcount`; every DELETE actually returns a `CursorResult`, so the
    narrowing is safe and lives here rather than at each call site.
    """
    result = cast("CursorResult[Any]", await session.execute(statement))
    return result.rowcount


async def delete_expired_idempotency_key(session: AsyncSession, key: str, now: datetime) -> bool:
    """Drop the bound tenant's row for `key` if it has expired; True if one went.

    The guard needs this before re-claiming an expired key: the row is
    invisible to `find_idempotency_key` but still holds the unique index.
    The `expires_at` condition is in the statement, not in the caller, so
    this can never remove a key that is still being honoured.
    """
    return (
        await _delete_count(
            session,
            delete(IdempotencyKey).where(
                IdempotencyKey.key == key, IdempotencyKey.expires_at <= now
            ),
        )
        > 0
    )


async def purge_expired(session: AsyncSession, now: datetime) -> int:
    """Delete the bound tenant's expired keys; returns how many went.

    Tenant-scoped like every other query here, so a scheduled purge
    loops tenants rather than sweeping the table in one unscoped statement.
    """
    return await _delete_count(
        session, delete(IdempotencyKey).where(IdempotencyKey.expires_at <= now)
    )


# --- the conversation store ---------------------------------------
#
# Named helpers rather than a plain `Repository` for two reasons the generic
# API cannot express: the session row's primary key is supplied by the caller
# (it is the token's `sid`, and `Repository.create` refuses `id` on purpose),
# and the history window has a fixed order and a fixed size that every caller
# must read the same way.

HISTORY_LIMIT = 20
"""How many past messages one turn sees (the design step 1).

Pinned by `test_conversation_models.py::test_max_message_chars_is_exactly_the_planned_value`. It bounds a prompt, so widening it widens every turn's
token bill; `MAX_MESSAGE_CHARS` bounds each row inside it.
"""


async def get_or_create_session(
    session: AsyncSession,
    *,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    now: datetime,
) -> Session:
    """The bound tenant's conversation row for `session_id`, created if absent.

    The id is the token's `sid` (a project rule: nothing here comes from a tool
    argument), so it is written explicitly instead of defaulted — the value
    the gateway sees as `X-Session` and the value this row is stored under
    have to be one. `Repository.create` refuses `id` by design, which is why
    this helper builds the model itself.

    `started_at` is stamped once and never moved; `last_seen_at` moves on
    every turn. Pinned by
    `test_conversation_models.py::test_get_or_create_session_reuses_the_row_and_moves_last_seen_at`,
    which fails both for a helper that inserts a second row and for one that
    loads without touching `last_seen_at`.

    A `session_id` that already names ANOTHER tenant's row is neither loaded
    nor overwritten: the guarded read cannot see it, so the insert collides on
    the primary key and becomes a typed `SessionConflictError`. That is the
    only reachable answer — `sessions.id` is table-wide, so "create a new row
    instead" is not something the schema allows.
    """
    existing = await Repository(session, Session).get(session_id)
    if existing is not None:
        existing.last_seen_at = now
        await session.flush()
        return existing

    row = Session(
        id=session_id,
        tenant_id=bound_tenant_id(session),
        user_id=user_id,
        started_at=now,
        last_seen_at=now,
        session_metadata={},
    )
    session.add(row)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise SessionConflictError("this session id is already in use") from exc
    return row


async def append_message(
    session: AsyncSession,
    *,
    session_id: uuid.UUID,
    role: MessageRole,
    content: str,
    tool_calls: dict[str, Any] | None = None,
    citations: list[Any] | None = None,
) -> Message:
    """Store one message on the bound tenant's conversation.

    The content is NOT checked here: `check_message_content` runs at the edge
    of the turn, before any row at all is written, which is the whole point of
    the pre-check. A caller that reaches this function with
    an oversized string gets the column's own error, and
    `test_agent_turn.py::test_a_message_one_character_over_the_cap_is_refused_before_any_row`
    is what proves no such caller exists on the turn path.

    `ordinal` is left to the database: it is an identity column, and a caller
    that numbered its own rows would race every other turn on the table.
    """
    row = Message(
        tenant_id=bound_tenant_id(session),
        session_id=session_id,
        role=role.value,
        content=content,
        tool_calls=tool_calls,
        citations=citations,
    )
    session.add(row)
    await session.flush()
    return row


async def list_recent_messages(
    session: AsyncSession, session_id: uuid.UUID, limit: int = HISTORY_LIMIT
) -> Sequence[Message]:
    """The last `limit` messages of this conversation, OLDEST FIRST.

    Ordered by `ordinal` and by nothing else. `created_at` cannot order these
    rows: Postgres's `now()` is transaction time, so every message one turn
    writes carries an identical timestamp — pinned by
    `test_conversation_models.py::test_list_recent_messages_returns_the_last_twenty_oldest_first`,
    whose premise assertion proves the twenty-five planted rows share one
    value before it asserts the order.

    The statement takes the TAIL (descending, limited) and reverses it in
    Python, because "the last twenty" cannot be expressed as an ascending
    LIMIT. The session guard filters the SELECT, so another tenant's session
    id reads as an empty window rather than as somebody else's conversation.
    """
    result = await session.execute(
        select(Message)
        .where(Message.session_id == session_id)
        .order_by(Message.ordinal.desc())
        .limit(limit)
    )
    return list(reversed(result.scalars().all()))
