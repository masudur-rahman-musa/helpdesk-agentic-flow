"""An earlier change, sections 1 and 6: `search_audit`, through the transport.

Surface under test (written before it exists):

``mcp_server.tools.read``
    ``SEARCH_LIMIT_MIN = 1``, ``SEARCH_LIMIT_DEFAULT = 50``,
    ``SEARCH_LIMIT_MAX = 200``, and the four filter caps READ OFF the
    ``audit_events`` columns they filter:
    ``EVENT_TYPE_MAX_LENGTH``, ``SUBJECT_TYPE_MAX_LENGTH``,
    ``SUBJECT_ID_MAX_LENGTH``, ``ACTOR_REF_MAX_LENGTH``.
    ``SearchAuditInput`` — decision D's eight optional fields:
    ``event_type``, ``subject_type``, ``subject_id``, ``actor_ref``,
    ``occurred_from``, ``occurred_to``, ``before_seq`` (``ge=1``) and
    ``limit`` (``ge=SEARCH_LIMIT_MIN``, ``le=SEARCH_LIMIT_MAX``, defaulting
    to ``SEARCH_LIMIT_DEFAULT``). No tenant and no user argument: both come
    from the token.
    ``AuditEventRow`` — ``seq``, ``occurred_at``, ``actor_kind``,
    ``actor_ref``, ``event_type``, ``subject_type``, ``subject_id``,
    ``payload`` and ``hash`` (criterion 2, decision E).
    ``SearchAuditOutput`` — ``events`` and ``next_before_seq`` (``None`` when
    the scan reached the end).
    ``search_audit`` — a registered READ tool (``mutating=False``, no
    idempotency key) whose registry ``event_type`` is the generic
    ``tool.succeeded``.

Scope (plan §2): the handler asks `rbac.authorize` for the caller's
`Authorization.scopes`, reads the caller's department from
`SimulatedDirectory` rather than from the token's `department_id` claim, and
keeps a row only if `helpdesk.access.approvals.request_in_scope` reaches the
(department, system owner) pair the row's subject resolves to. `Scope.ANY`
short-circuits the resolution entirely. A subject that resolves to neither —
`subject_type="tool"`, a null `subject_id`, or a type outside plan §2's table
— is withheld from a narrow caller and returned to an ANY caller, which is
the fail-closed direction.

the design's `search_audit` row gives a cell to `manager`, `system_owner`, `hr`,
`it_admin`, `auditor` AND `platform_admin`, so exactly ONE principal holds no
cell: `employee`. (An earlier change §1 says "the five principals"; that numeral is
carried over from an earlier change's `rollback_request` row and is wrong for this
one — `search_audit` is the widest row in the matrix.)

Every scope case below is called by a principal the matrix ADMITS, over rows
in the caller's OWN tenant, so neither the pipeline's RBAC stage nor the
tenant-bound session can produce the refusal: the department/owner comparison
is the only control that can withhold the row. No kill
here is claimed from a watched deletion — nothing was run when this file was
written — so each docstring states the reasoning that
isolates the control rather than asserting a verified deletion.

Fixture cost: everything rides an earlier change's session-scoped
``mcp_fixtures`` seam — one seeded throwaway database, one dedicated Redis
index flushed per test. Per-test isolation inside the shared database is a
namespace: every case plants its OWN departments, systems, roles, people and
grants (uuid4-suffixed) and files its audit events under its OWN
``event_type`` marker, which every search below filters on. The pipeline's
own ``tool.succeeded`` events therefore never appear in a result set.

Every raw-SQL fixture binds `date` and `datetime` OBJECTS, never strings:
asyncpg binds by type and does not coerce (the project notes, an earlier change).

No pytest-asyncio: each case is a sync function driving one ``asyncio.run``.
"""

import asyncio
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import pytest
from helpdesk.models import AuditEvent
from mcp_fixtures import (  # noqa: F401 - fixtures are resolved by name
    SeededMcp,
    SeededUser,
    bearer,
    call_tool,
    error_code,
    mcp_env,
    mcp_pg,
    mcp_redis_base_url,
    mcp_redis_url,
    seeded_mcp_db,
    token_for,
)
from mcp_server.tools import read
from sqlalchemy import String, text
from sqlalchemy.ext.asyncio import create_async_engine

SEARCH = "search_audit"

# The audit contract as literals, never imports: reading these strings from
# the module under test would let a rename pass unnoticed.
SUCCEEDED_EVENT = "tool.succeeded"
DENIED_EVENT = "tool.denied"

# the design's `search_audit` row. Six principals hold a cell and exactly one
# does not; four of the six hold `Scope.ANY`.
ANY_SCOPE_PRINCIPALS = ("hr", "it_admin", "auditor", "platform_admin")
UNCELLED_PRINCIPAL = "employee"

# The scope string the pipeline records on a `Scope.ANY` caller's event.
ANY_SCOPE_VALUE = "any"

# Decision D's numbers, written out here and compared against the module's own
# constants inside the bounds case, so the values are pinned by this test file
# rather than borrowed from the code.
EXPECTED_LIMIT_MIN = 1
EXPECTED_LIMIT_DEFAULT = 50
EXPECTED_LIMIT_MAX = 200


def _column_length(name: str) -> int:
    """One `audit_events` column's declared length, read off the column.

    Never a literal retyped beside the model: the query
    pre-check and the storage it filters must be the same number by
    construction, so widening one without the other fails the boundary case.
    """
    column_type = AuditEvent.__table__.c[name].type
    assert isinstance(column_type, String), column_type
    assert column_type.length is not None, f"audit_events.{name} must be a sized column"
    return column_type.length


EVENT_TYPE_LENGTH = _column_length("event_type")
SUBJECT_TYPE_LENGTH = _column_length("subject_type")
SUBJECT_ID_LENGTH = _column_length("subject_id")
ACTOR_REF_LENGTH = _column_length("actor_ref")

ACTIVE_STATUS = "active"
PLANTED_START = date(2019, 3, 7)
PLANTED_PASSWORD_HASH = "not-a-real-hash-this-user-never-logs-in"
GRANTED_AT = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
FAR_EXPIRY = datetime(2099, 6, 1, 12, 0, tzinfo=UTC)
OCCURRED_AT = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)

# `search_audit` itself is the unresolvable subject the pipeline files its own
# events against (`subject_type="tool"`), which is what plan §2's last row
# describes.
TOOL_SUBJECT_TYPE = "tool"

# The nine fields criterion 2 says every returned row carries.
ROW_FIELDS = frozenset(
    {
        "seq",
        "occurred_at",
        "actor_kind",
        "actor_ref",
        "event_type",
        "subject_type",
        "subject_id",
        "payload",
        "hash",
    }
)


# --- raw-SQL probes (reading and writing around the tool, never through it) -----


def _rows(database_url: str, sql: str, params: dict[str, Any]) -> list[Any]:
    async def scenario() -> list[Any]:
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as conn:
                return list(await conn.execute(text(sql), params))
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _execute(database_url: str, sql: str, params: dict[str, Any]) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as conn:
                await conn.execute(text(sql), params)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def _plant_department(database_url: str, tenant_id: uuid.UUID) -> uuid.UUID:
    department_id = uuid.uuid4()
    _execute(
        database_url,
        "INSERT INTO departments (id, tenant_id, name, manager_user_id, created_at, updated_at)"
        " VALUES (:id, :tenant, :name, NULL, now(), now())",
        {
            "id": department_id,
            "tenant": tenant_id,
            "name": f"Fake Audit Dept {uuid.uuid4().hex[:12]}",
        },
    )
    return department_id


def _plant_user(
    database_url: str, tenant_id: uuid.UUID, department_id: uuid.UUID | None
) -> uuid.UUID:
    user_id = uuid.uuid4()
    _execute(
        database_url,
        "INSERT INTO users (id, tenant_id, department_id, email, display_name, password_hash,"
        " principals, status, start_date, end_date, created_at, updated_at)"
        " VALUES (:id, :tenant, :department, :email, :display_name, :password_hash,"
        " :principals, :status, :start_date, NULL, now(), now())",
        {
            "id": user_id,
            "tenant": tenant_id,
            "department": department_id,
            "email": f"fake.audit.{uuid.uuid4().hex}@fake.invalid",
            "display_name": "Fake Audit Person",
            "password_hash": PLANTED_PASSWORD_HASH,
            "principals": ["employee"],
            "status": ACTIVE_STATUS,
            # A `date` object, never the string: asyncpg binds by type.
            "start_date": PLANTED_START,
        },
    )
    return user_id


def _plant_system(
    database_url: str, tenant_id: uuid.UUID, owner_user_id: uuid.UUID | None
) -> uuid.UUID:
    system_id = uuid.uuid4()
    _execute(
        database_url,
        "INSERT INTO systems (id, tenant_id, slug, name, sensitivity, category, owner_user_id,"
        " created_at, updated_at)"
        " VALUES (:id, :tenant, :slug, :name, 'low', 'other', :owner, now(), now())",
        {
            "id": system_id,
            "tenant": tenant_id,
            "slug": f"fake-sys-{uuid.uuid4().hex[:12]}",
            "name": "Fake Audit System",
            "owner": owner_user_id,
        },
    )
    return system_id


def _plant_role(database_url: str, tenant_id: uuid.UUID, system_id: uuid.UUID) -> uuid.UUID:
    role_id = uuid.uuid4()
    _execute(
        database_url,
        "INSERT INTO roles (id, tenant_id, system_id, slug, name, privilege,"
        " default_expiry_days, created_at, updated_at)"
        " VALUES (:id, :tenant, :system, :slug, :name, 'read', 365, now(), now())",
        {
            "id": role_id,
            "tenant": tenant_id,
            "system": system_id,
            "slug": f"fake-role-{uuid.uuid4().hex[:12]}",
            "name": "Fake Audit Role",
        },
    )
    return role_id


def _plant_grant(
    database_url: str, tenant_id: uuid.UUID, user_id: uuid.UUID, role_id: uuid.UUID
) -> uuid.UUID:
    grant_id = uuid.uuid4()
    _execute(
        database_url,
        "INSERT INTO grants (id, tenant_id, user_id, role_id, request_id, granted_by_user_id,"
        " granted_at, expires_at, revoked_at, revoke_reason, status, created_at, updated_at)"
        " VALUES (:id, :tenant, :user, :role, NULL, NULL, :granted_at, :expires_at, NULL, NULL,"
        " :status, now(), now())",
        {
            "id": grant_id,
            "tenant": tenant_id,
            "user": user_id,
            "role": role_id,
            # `datetime` objects, never strings (same reason as above).
            "granted_at": GRANTED_AT,
            "expires_at": FAR_EXPIRY,
            "status": ACTIVE_STATUS,
        },
    )
    return grant_id


def _plant_event(
    database_url: str,
    tenant_id: uuid.UUID,
    *,
    event_type: str,
    subject_type: str,
    subject_id: str | None,
) -> None:
    """One committed `audit_events` row, written AROUND the tool.

    The chain columns carry obviously fake digests: `search_audit` reads
    columns and never re-hashes, so a real chain would be fixture weight with
    no assertion behind it. `seq` is an `Identity` column and is assigned by
    Postgres in insert order.
    """
    _execute(
        database_url,
        "INSERT INTO audit_events (id, tenant_id, occurred_at, actor_kind, actor_ref,"
        " session_id, trace_id, event_type, subject_type, subject_id, payload, prev_hash, hash,"
        " created_at, updated_at)"
        " VALUES (:id, :tenant, :occurred_at, 'system', NULL, NULL, :trace, :event_type,"
        " :subject_type, :subject_id, CAST(:payload AS jsonb), :prev_hash, :hash, now(), now())",
        {
            "id": uuid.uuid4(),
            "tenant": tenant_id,
            # A `datetime` object, never the string.
            "occurred_at": OCCURRED_AT,
            "trace": f"fake-trace-{uuid.uuid4().hex}",
            "event_type": event_type,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "payload": json.dumps({"fake": True}),
            "prev_hash": uuid.uuid4().hex * 2,
            "hash": uuid.uuid4().hex * 2,
        },
    )


def _audit_event_types(database_url: str, tenant_id: uuid.UUID) -> list[str]:
    rows = _rows(
        database_url,
        "SELECT event_type FROM audit_events WHERE tenant_id = :tenant ORDER BY seq",
        {"tenant": tenant_id},
    )
    return [str(row[0]) for row in rows]


def _last_event(database_url: str, tenant_id: uuid.UUID) -> Any:
    rows = _rows(
        database_url,
        "SELECT event_type, subject_type, subject_id, payload FROM audit_events"
        " WHERE tenant_id = :tenant ORDER BY seq DESC LIMIT 1",
        {"tenant": tenant_id},
    )
    assert len(rows) == 1, f"expected at least one audit event, got {len(rows)}"
    return rows[0]


# --- the realm one case owns ----------------------------------------------------


@dataclass(frozen=True)
class Realm:
    """Three departments, two systems and a cast, all planted for one case.

    Everything is fresh per case, and every audit row the case plants carries
    `marker` as its `event_type`, which every search filters on. So the result
    sets asserted below are exact: no other case's rows and none of the
    pipeline's own `tool.succeeded` events can appear in them.
    """

    tenant_id: uuid.UUID
    marker: str
    department_a: uuid.UUID
    department_b: uuid.UUID
    manager: SeededUser
    """In `department_a`. Holds the `manager` cell in every case that uses it."""
    outsider: SeededUser
    """In a THIRD department, owning no system and managing nobody."""
    owner: SeededUser
    """Owns `system_owned` and nothing else."""
    member_a: uuid.UUID
    member_b: uuid.UUID
    system_owned: uuid.UUID
    system_other: uuid.UUID
    grant_owned: uuid.UUID
    """A grant on a role of `system_owned`, held by `member_a`."""
    grant_other: uuid.UUID
    """A grant on a role of `system_other`, held by `member_b`."""


def _handle(
    env: SeededMcp, tenant_id: uuid.UUID, user_id: uuid.UUID, department_id: uuid.UUID
) -> SeededUser:
    """A token-ready handle for a planted user.

    The slug is resolved from the env handle the rows were planted through,
    never from a seed constant.
    """
    slug = (
        env.tenant_a.tenant_slug
        if tenant_id == env.tenant_a.tenant_id
        else env.tenant_b.tenant_slug
    )
    return SeededUser(
        tenant_id=tenant_id,
        tenant_slug=slug,
        user_id=user_id,
        email=f"fake.handle.{user_id}@fake.invalid",
        display_name="Fake Audit Person",
        department_id=department_id,
        principals=("employee",),
        status=ACTIVE_STATUS,
    )


def plant_realm(env: SeededMcp, *, tenant_id: uuid.UUID | None = None) -> Realm:
    """Everything a scope case needs, in one tenant, owned by one test."""
    tenant = env.tenant_a.tenant_id if tenant_id is None else tenant_id
    url = env.database_url
    department_a = _plant_department(url, tenant)
    department_b = _plant_department(url, tenant)
    department_c = _plant_department(url, tenant)

    manager_id = _plant_user(url, tenant, department_a)
    _execute(
        url,
        "UPDATE departments SET manager_user_id = :manager WHERE id = :id",
        {"manager": manager_id, "id": department_a},
    )
    member_a = _plant_user(url, tenant, department_a)
    member_b = _plant_user(url, tenant, department_b)
    owner_id = _plant_user(url, tenant, department_b)
    other_owner_id = _plant_user(url, tenant, department_b)
    outsider_id = _plant_user(url, tenant, department_c)

    system_owned = _plant_system(url, tenant, owner_id)
    system_other = _plant_system(url, tenant, other_owner_id)
    role_owned = _plant_role(url, tenant, system_owned)
    role_other = _plant_role(url, tenant, system_other)

    return Realm(
        tenant_id=tenant,
        marker=f"fake.search.{uuid.uuid4().hex}",
        department_a=department_a,
        department_b=department_b,
        manager=_handle(env, tenant, manager_id, department_a),
        outsider=_handle(env, tenant, outsider_id, department_c),
        owner=_handle(env, tenant, owner_id, department_b),
        member_a=member_a,
        member_b=member_b,
        system_owned=system_owned,
        system_other=system_other,
        grant_owned=_plant_grant(url, tenant, member_a, role_owned),
        grant_other=_plant_grant(url, tenant, member_b, role_other),
    )


# --- calling ---------------------------------------------------------------------


def _structured(result: Any) -> dict[str, Any]:
    assert result.is_error is False, result.content
    assert result.structured_content is not None, "a successful call carries structured content"
    content: dict[str, Any] = result.structured_content
    return content


def _search(env: SeededMcp, caller: SeededUser, principal: str, **arguments: Any) -> Any:
    return call_tool(bearer(token_for(caller, principals=(principal,))), SEARCH, arguments)


def _subjects(payload: dict[str, Any]) -> list[tuple[str, str | None]]:
    """Each returned row as (subject_type, subject_id), newest first."""
    events: list[dict[str, Any]] = payload["events"]
    return [(str(row["subject_type"]), row["subject_id"]) for row in events]


# --- the four `Scope.ANY` principals (criterion 4) -------------------------------


@pytest.mark.parametrize("principal", ANY_SCOPE_PRINCIPALS)
def test_each_any_scope_principal_reads_every_planted_event(
    mcp_env: SeededMcp,  # noqa: F811
    principal: str,
) -> None:
    """Criterion 4's wide half, one the design cell at a time, plus criterion 2's
    row shape.

    KILL (reasoned, not watched): the `Scope.ANY` short-circuit. The caller is
    an `outsider` — a third department, no system owned, nobody managed — so
    the three planted rows resolve to a department they are not in, a system
    they do not own, and nothing at all. A handler that ran plan §2's
    resolution for every caller instead of short-circuiting `Scope.ANY`
    returns an EMPTY page here while still passing both narrow cases below,
    and `auditor`, the principal the design puts on the audit trail, could read
    nothing.

    A second kill for the parametrisation: a registry entry wired to a
    narrower matrix member. `rollback_request` admits `it_admin` and
    `platform_admin` only and the review rows admit neither `auditor` nor
    `employee`, so a member borrowed from either fails one of these four rows
    or the `employee` case below. The token carries exactly ONE principal, so
    nothing but that principal's own cell can admit the call.

    The row shape (criterion 2) is asserted here rather than in its own case:
    this is the only case that sees every planted row, so it is the only one
    that can show `payload` and `hash` present on rows of all three subject
    kinds (decision E — an audit search without the payload answers no
    question an auditor asks).
    """
    realm = plant_realm(mcp_env)
    for subject_type, subject_id in (
        ("user", str(realm.member_a)),
        ("grant", str(realm.grant_owned)),
        (TOOL_SUBJECT_TYPE, SEARCH),
    ):
        _plant_event(
            mcp_env.database_url,
            realm.tenant_id,
            event_type=realm.marker,
            subject_type=subject_type,
            subject_id=subject_id,
        )

    payload = _structured(
        _search(mcp_env, realm.outsider, principal, event_type=realm.marker),
    )

    assert sorted(_subjects(payload)) == sorted(
        [
            ("user", str(realm.member_a)),
            ("grant", str(realm.grant_owned)),
            (TOOL_SUBJECT_TYPE, SEARCH),
        ]
    )
    seqs = [int(row["seq"]) for row in payload["events"]]
    assert seqs == sorted(seqs, reverse=True), f"events are not newest first: {seqs}"
    for row in payload["events"]:
        assert set(row) == ROW_FIELDS, sorted(row)
        assert row["event_type"] == realm.marker
        assert row["payload"] == {"fake": True}
        assert len(str(row["hash"])) == _column_length("hash")


# --- the two narrow scopes (criterion 4, a project rule) -----------------------


def test_a_manager_reads_only_the_events_of_their_own_department(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Criterion 4's `own_department` half.

    KILL (reasoned, not watched): the department comparison inside the scope
    filter. `manager` holds a cell on this row, so the pipeline's RBAC stage
    admits the call; both events are in the caller's OWN tenant, so the
    tenant-bound session cannot hide either; both subjects resolve through
    plan §2's `user` row, so the fail-closed default cannot hide either. The
    only thing left that can tell the two rows apart is whose department the
    subject is in.

    Both directions are asserted in one call: the in-department row comes
    back, so a handler that refuses every manager fails, and the
    out-of-department row does not, so a handler that compares nothing fails.

    The department used in the comparison is read from the DIRECTORY, not the
    token claim; that direction is pinned separately by
    `test_the_managers_department_comes_from_the_directory_not_the_token`,
    because here the claim and the directory agree.
    """
    realm = plant_realm(mcp_env)
    for subject_id in (realm.member_a, realm.member_b):
        _plant_event(
            mcp_env.database_url,
            realm.tenant_id,
            event_type=realm.marker,
            subject_type="user",
            subject_id=str(subject_id),
        )

    payload = _structured(_search(mcp_env, realm.manager, "manager", event_type=realm.marker))

    assert _subjects(payload) == [("user", str(realm.member_a))]


def test_a_system_owner_reads_only_the_events_of_a_system_they_own(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Criterion 4's `own_systems` half, which the department case cannot reach.

    KILL (reasoned, not watched): the system-owner comparison. A handler that
    implemented `own_department` and left `own_systems` answering True for
    every row passes the manager case above and fails here; one that answered
    False for every system passes nothing, because the owned row is asserted
    present in the same call.

    `system_owner` holds a cell on this row, so RBAC admits the call, and both
    grants are in the caller's OWN tenant and resolve cleanly through plan
    §2's `grant` row — so, again, only the owner comparison can separate them.
    The caller's own department is `department_b`, which is where the
    OUT-of-scope grant's holder sits: a handler that compared departments
    instead of owners would return exactly the wrong one of the two rows,
    rather than both or neither.
    """
    realm = plant_realm(mcp_env)
    for subject_id in (realm.grant_owned, realm.grant_other):
        _plant_event(
            mcp_env.database_url,
            realm.tenant_id,
            event_type=realm.marker,
            subject_type="grant",
            subject_id=str(subject_id),
        )

    payload = _structured(_search(mcp_env, realm.owner, "system_owner", event_type=realm.marker))

    assert _subjects(payload) == [("grant", str(realm.grant_owned))]


# --- the unresolvable subject (criterion 6) --------------------------------------


def test_an_unresolvable_subject_is_withheld_from_a_manager_and_returned_to_an_auditor(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Criterion 6, both directions of plan §2's last row.

    KILL (reasoned, not watched), manager half: the fail-closed default. The
    `tool` subject resolves to no department and no system, so a resolution
    that returned "reachable" on a miss — the natural shape once the two
    positive cases above exist — hands every narrow caller every row the
    table does not cover. The manager is given a SECOND event, about a user in
    their own department, which they DO read: without it, a handler that
    refused the manager everything would pass this case for the wrong reason.

    KILL, auditor half: running the resolution for an `Scope.ANY` caller. The
    same unresolvable row must come back, because an `auditor` who cannot see
    `tool.*` events cannot see who called what — which is most of SPEC
    §13.1's log.

    Both events are in the caller's own tenant and both callers hold a cell on
    this row, so no other guard can decide either half.
    """
    realm = plant_realm(mcp_env)
    _plant_event(
        mcp_env.database_url,
        realm.tenant_id,
        event_type=realm.marker,
        subject_type="user",
        subject_id=str(realm.member_a),
    )
    _plant_event(
        mcp_env.database_url,
        realm.tenant_id,
        event_type=realm.marker,
        subject_type=TOOL_SUBJECT_TYPE,
        subject_id=SEARCH,
    )

    narrow = _structured(_search(mcp_env, realm.manager, "manager", event_type=realm.marker))
    wide = _structured(_search(mcp_env, realm.manager, "auditor", event_type=realm.marker))

    assert _subjects(narrow) == [("user", str(realm.member_a))]
    assert sorted(_subjects(wide)) == sorted(
        [("user", str(realm.member_a)), (TOOL_SUBJECT_TYPE, SEARCH)]
    )


# --- the directory, not the token (criterion 5) ----------------------------------


def test_the_managers_department_comes_from_the_directory_not_the_token(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Criterion 5, and a project rule on the department comparison itself.

    KILL (reasoned, not watched): reading `department_id` off the token's
    claims instead of `SimulatedDirectory`. The token is minted BEFORE the
    caller's directory row is moved, so its claim still names
    `department_a` while the directory says `department_b`. A handler reading
    the claim returns `department_a`'s event; one reading the directory
    returns `department_b`'s. Every other scope case in this file passes
    whichever source is used, because there the two agree — only this one
    separates them.

    The caller and both subjects are in the SAME tenant: a
    cross-tenant version would be refused by the tenant-bound session whether
    the department came from the claim or the directory, and would pin
    nothing. A manager transferred after their token was issued searches
    their NEW department, and loses the reach of the one they left.
    """
    realm = plant_realm(mcp_env)
    for subject_id in (realm.member_a, realm.member_b):
        _plant_event(
            mcp_env.database_url,
            realm.tenant_id,
            event_type=realm.marker,
            subject_type="user",
            subject_id=str(subject_id),
        )
    stale_token = token_for(realm.manager, principals=("manager",))

    _execute(
        mcp_env.database_url,
        "UPDATE users SET department_id = :department WHERE id = :id",
        {"department": realm.department_b, "id": realm.manager.user_id},
    )
    payload = _structured(
        call_tool(bearer(stale_token), SEARCH, {"event_type": realm.marker}),
    )

    assert _subjects(payload) == [("user", str(realm.member_b))]


# --- the design's one empty cell (criterion 7) --------------------------------------


def test_the_employee_principal_reads_forbidden_and_no_data(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Criterion 7. the design gives `search_audit` a cell to six of the seven
    principals, so `employee` is the ONLY one with none — an earlier change §1's "five
    principals" is `rollback_request`'s number, not this row's.

    KILL (reasoned, not watched): adding `employee` to the `search_audit` cell
    of the RBAC matrix. Every argument is valid, the filter names events that
    really exist in the caller's OWN tenant, and the caller is a real active
    user, so nothing but a permission decision can produce this answer.

    This does NOT claim the pipeline's RBAC stage alone as its kill (standing
    rule 20): the handler re-runs `rbac.authorize` to obtain
    `Authorization.scopes`, so an `employee` is refused twice by two copies of
    one decision and neither copy can be killed separately.

    `tool.denied` rather than `tool.failed`: a permission decision filed as an
    outage is a false record, and a count-only assertion cannot tell them
    apart. The refusal carries the code and no rows at all (the design).
    """
    realm = plant_realm(mcp_env)
    _plant_event(
        mcp_env.database_url,
        realm.tenant_id,
        event_type=realm.marker,
        subject_type="user",
        subject_id=str(realm.member_a),
    )
    events_before = _audit_event_types(mcp_env.database_url, realm.tenant_id)

    result = _search(mcp_env, realm.manager, UNCELLED_PRINCIPAL, event_type=realm.marker)

    assert error_code(result) == "FORBIDDEN"
    assert result.structured_content is None
    assert _audit_event_types(mcp_env.database_url, realm.tenant_id) == [
        *events_before,
        DENIED_EVENT,
    ]


# --- the other tenant (criterion 8) ----------------------------------------------


def test_another_tenants_subject_id_returns_an_empty_page_not_an_error(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Criterion 8: an empty page, never a row and never an existence signal.

    KILL (reasoned, not watched): a search that escapes the tenant-bound
    session. The subject named is a REAL user with a REAL audit row in the
    other tenant, filtered by an `event_type` that exists only there, so a
    read that reached across tenants would return that row rather than an
    empty page — a random uuid would answer empty whether the binding held or
    not and would pin nothing.

    The caller holds `auditor` (`Scope.ANY`), so the scope filter cannot be
    what empties the page: only the tenant boundary can. The answer is
    asserted as a SUCCESS with `events == []`, not as `NOT_FOUND`: a distinct
    error for "exists elsewhere" would turn any id into a cross-tenant
    existence oracle.
    """
    here = plant_realm(mcp_env)
    there = plant_realm(mcp_env, tenant_id=mcp_env.tenant_b.tenant_id)
    _plant_event(
        mcp_env.database_url,
        there.tenant_id,
        event_type=there.marker,
        subject_type="user",
        subject_id=str(there.member_a),
    )

    payload = _structured(
        _search(
            mcp_env,
            here.outsider,
            "auditor",
            event_type=there.marker,
            subject_id=str(there.member_a),
        )
    )

    assert payload["events"] == []
    assert payload["next_before_seq"] is None


# --- the bounds (criterion 9, a project rule and 2) ----------------------------


def test_the_search_bounds_and_column_caps_are_exactly_their_values() -> None:
    """Decision D's numbers, pinned exactly.

    Catches a widened `limit` (an unbounded response over the wire), a drifted
    default (every caller who omits `limit` silently receives a different
    number of events), and a filter cap RETYPED beside the model instead of
    read off the column it filters. The four caps are asserted against
    `audit_events`' own column lengths AND against their literal values, so
    the pair cannot drift together unnoticed: `_column_length` alone would
    keep passing if both the column and the constant were widened.
    """
    assert read.SEARCH_LIMIT_MIN == EXPECTED_LIMIT_MIN == 1
    assert read.SEARCH_LIMIT_DEFAULT == EXPECTED_LIMIT_DEFAULT == 50
    assert read.SEARCH_LIMIT_MAX == EXPECTED_LIMIT_MAX == 200

    assert (EVENT_TYPE_LENGTH, SUBJECT_TYPE_LENGTH, SUBJECT_ID_LENGTH, ACTOR_REF_LENGTH) == (
        64,
        64,
        256,
        256,
    )
    assert read.EVENT_TYPE_MAX_LENGTH == EVENT_TYPE_LENGTH
    assert read.SUBJECT_TYPE_MAX_LENGTH == SUBJECT_TYPE_LENGTH
    assert read.SUBJECT_ID_MAX_LENGTH == SUBJECT_ID_LENGTH
    assert read.ACTOR_REF_MAX_LENGTH == ACTOR_REF_LENGTH


def test_the_default_limit_is_read_off_the_input_model() -> None:
    """Catches the constant and the model disagreeing: a `Field(default=100)`
    beside a `SEARCH_LIMIT_DEFAULT` of 50 leaves the case above green while
    every real caller who omits `limit` receives a different page size than
    the plan says. All eight fields are optional, so the model constructs with
    no arguments at all — which is also what pins that a filterless search is
    a legal call."""
    assert read.SearchAuditInput().limit == read.SEARCH_LIMIT_DEFAULT


LIMIT_CASES: tuple[tuple[str, int, bool], ...] = (
    ("limit-0-refused", 0, False),
    ("limit-1-ok", EXPECTED_LIMIT_MIN, True),
    ("limit-200-ok", EXPECTED_LIMIT_MAX, True),
    ("limit-201-refused", EXPECTED_LIMIT_MAX + 1, False),
)


@pytest.mark.parametrize(
    ("limit", "accepted"),
    [case[1:] for case in LIMIT_CASES],
    ids=[case[0] for case in LIMIT_CASES],
)
def test_the_limit_boundary_is_inclusive_at_1_and_200(
    mcp_env: SeededMcp,  # noqa: F811
    limit: int,
    accepted: bool,
) -> None:
    """Criterion 9's `limit`, both sides of both ends.

    A one-sided assertion cannot tell an inclusive bound from an exclusive
    one: `gt=1` accepts 200 and refuses 1, and `lt=200` accepts 1 and refuses
    200, so both accepted rows are needed to pin `ge`/`le`. Zero catches a
    missing lower bound, which would let a caller ask for an empty page and
    read it as "this tenant has no such events"; 201 catches a missing upper
    bound, which is an unbounded response over the wire against the one table
    the design never prunes.

    Two events are planted and `limit=1` must return exactly one of them, so
    the bound is proved to be APPLIED and not merely accepted — a model that
    validated `limit` and then ignored it passes every refusal row and fails
    this one.
    """
    realm = plant_realm(mcp_env)
    for subject_id in (realm.member_a, realm.member_b):
        _plant_event(
            mcp_env.database_url,
            realm.tenant_id,
            event_type=realm.marker,
            subject_type="user",
            subject_id=str(subject_id),
        )

    result = _search(
        mcp_env,
        realm.outsider,
        "auditor",
        event_type=realm.marker,
        limit=limit,
    )

    if accepted:
        payload = _structured(result)
        assert len(payload["events"]) == min(limit, 2)
    else:
        assert error_code(result) == "VALIDATION"


BEFORE_SEQ_CASES: tuple[tuple[str, int, bool], ...] = (
    ("before-seq-0-refused", 0, False),
    ("before-seq-1-ok", 1, True),
)


@pytest.mark.parametrize(
    ("before_seq", "accepted"),
    [case[1:] for case in BEFORE_SEQ_CASES],
    ids=[case[0] for case in BEFORE_SEQ_CASES],
)
def test_before_seq_is_accepted_at_1_and_refused_at_0(
    mcp_env: SeededMcp,  # noqa: F811
    before_seq: int,
    accepted: bool,
) -> None:
    """Criterion 9's `before_seq >= 1`, at the boundary and one below it.

    `seq` is a Postgres `Identity` column starting at 1, so 1 is the lowest
    cursor that can ever name a row and 0 is the first value that cannot. A
    missing `ge=1` lets a client send 0 or a negative cursor, which reads as
    "no events before the beginning" — an empty page that looks exactly like
    an empty audit trail. `gt=1` would refuse the only legal first cursor,
    which is why the accepted row is 1 and not 2.
    """
    realm = plant_realm(mcp_env)
    _plant_event(
        mcp_env.database_url,
        realm.tenant_id,
        event_type=realm.marker,
        subject_type="user",
        subject_id=str(realm.member_a),
    )

    result = _search(
        mcp_env,
        realm.outsider,
        "auditor",
        event_type=realm.marker,
        before_seq=before_seq,
    )

    if accepted:
        assert result.is_error is False, result.content
    else:
        assert error_code(result) == "VALIDATION"


STRING_CAP_CASES: tuple[tuple[str, str, int], ...] = (
    ("event-type", "event_type", EVENT_TYPE_LENGTH),
    ("subject-type", "subject_type", SUBJECT_TYPE_LENGTH),
    ("subject-id", "subject_id", SUBJECT_ID_LENGTH),
    ("actor-ref", "actor_ref", ACTOR_REF_LENGTH),
)


@pytest.mark.parametrize(
    ("field", "cap"),
    [case[1:] for case in STRING_CAP_CASES],
    ids=[case[0] for case in STRING_CAP_CASES],
)
@pytest.mark.parametrize("over", [False, True], ids=["at-cap-ok", "over-cap-refused"])
def test_each_string_filter_is_inclusive_at_its_column_cap_and_refused_one_over(
    mcp_env: SeededMcp,  # noqa: F811
    field: str,
    cap: int,
    over: bool,
) -> None:
    """Criterion 9's four filter caps, at the cap and at cap+1, per field.

    Every length here is computed off the `audit_events` column the filter
    reads (`_column_length`), never typed as a literal: a
    column widened without its pre-check, or a pre-check retyped at the wrong
    number, moves both ends of this case together with the column.

    A one-sided assertion cannot tell `max_length=cap` from
    `max_length=cap-1`, so both rows are needed. The at-cap row must be
    ACCEPTED — a value the column can store must be a value the filter can
    ask about, or the widest legal `subject_id` becomes unsearchable — and it
    matches nothing, so the answer is an empty page rather than an error.
    The over-cap row must be `VALIDATION` and not a database error surfacing
    as `INTERNAL`: a filter longer than the column can hold is the caller's
    mistake, and Postgres would otherwise refuse it in words no boundary may
    forward (the design).
    """
    realm = plant_realm(mcp_env)
    _plant_event(
        mcp_env.database_url,
        realm.tenant_id,
        event_type=realm.marker,
        subject_type="user",
        subject_id=str(realm.member_a),
    )

    result = _search(
        mcp_env,
        realm.outsider,
        "auditor",
        **{field: "x" * (cap + 1 if over else cap)},
    )

    if over:
        assert error_code(result) == "VALIDATION"
    else:
        assert _structured(result)["events"] == []


# --- the audit record of the call itself (criterion 10) --------------------------


def test_one_search_writes_exactly_one_tool_succeeded_naming_the_tool(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Criterion 10, as an exact ordered delta rather than a count.

    KILL (reasoned, not watched): a registry entry that declared a
    domain-specific `event_type` for this tool, or a handler that appended an
    event of its own. `search_audit` changes nothing, so the pipeline's
    generic summary is the WHOLE record it may leave; a second event would
    put a read into the log twice, and a search that recorded a domain event
    would claim something happened that did not.

    a project rule: the event's subject and payload keys are asserted, not
    just its type. This event's subject IS the tool — which is exactly why
    plan §2's table gives `subject_type="tool"` no department and no system,
    and why the manager case above cannot see it.

    A second identical search is run first (criterion 10's "a second identical
    search returns the same rows"), so the delta proves both that a read is
    repeatable and that each call files exactly one event, never zero and
    never two.
    """
    realm = plant_realm(mcp_env)
    _plant_event(
        mcp_env.database_url,
        realm.tenant_id,
        event_type=realm.marker,
        subject_type="user",
        subject_id=str(realm.member_a),
    )
    events_before = _audit_event_types(mcp_env.database_url, realm.tenant_id)

    first = _structured(_search(mcp_env, realm.outsider, "auditor", event_type=realm.marker))
    second = _structured(_search(mcp_env, realm.outsider, "auditor", event_type=realm.marker))

    assert first["events"] == second["events"]
    assert _subjects(first) == [("user", str(realm.member_a))]
    assert _audit_event_types(mcp_env.database_url, realm.tenant_id) == [
        *events_before,
        SUCCEEDED_EVENT,
        SUCCEEDED_EVENT,
    ]

    record = _last_event(mcp_env.database_url, realm.tenant_id)
    assert str(record.event_type) == SUCCEEDED_EVENT
    assert str(record.subject_type) == TOOL_SUBJECT_TYPE
    assert str(record.subject_id) == SEARCH
    event_payload = dict(record.payload)
    assert set(event_payload) == {"tool", "scope"}
    assert event_payload["tool"] == SEARCH
    assert event_payload["scope"] == ANY_SCOPE_VALUE
