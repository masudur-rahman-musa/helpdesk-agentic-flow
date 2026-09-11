"""An earlier change, sections 1 and 6: the two review tools, through the transport.

Surface under test (written before it exists):

``mcp_server.tools.reviews``
    ``LIMIT_MIN = 1``, ``LIMIT_DEFAULT = 50``, ``LIMIT_MAX = 200`` (decision I).
    ``OpenAccessReviewInput(MutatingToolInput)`` — ``department_id`` and
    ``system_id``, both ``uuid.UUID | None`` with exactly one set, plus
    ``period: str`` validated by a ``field_validator`` calling
    ``re.fullmatch(r"\\d{4}-Q[1-4]")`` against
    ``helpdesk.models.PERIOD_LENGTH`` (decision H), and the idempotency key
    and ``extra="forbid"`` of ``MutatingToolInput``. No tenant argument: the
    tenant is the token's.
    ``OpenAccessReviewOutput`` — ``review_id``, ``department_id``,
    ``system_id``, ``period``, ``reviewer_user_id``, ``item_count`` and
    ``flag_counts``.
    ``ListReviewItemsInput`` — ``review_id: uuid.UUID`` and
    ``limit: int = LIMIT_DEFAULT`` bounded ``ge=LIMIT_MIN, le=LIMIT_MAX``.
    ``ListReviewItemsOutput`` — ``items``, each carrying at least
    ``item_id``, ``grant_id`` and ``flag``.
    ``open_access_review`` (MUTATING, registry ``event_type`` the generic
    ``tool.succeeded``: the handler appends ``review.opened`` itself, naming
    the REVIEW) and ``list_review_items`` (a read).

Refusals: ``FORBIDDEN`` (+ ``tool.denied``) for the two principals the design
gives no cell on either row, ``FORBIDDEN`` for a scope the caller's the design
scope does not reach, one shared ``NOT_FOUND`` for a scope or review id that
is missing OR another tenant's, ``POLICY_BLOCKED`` for a second open review of
one (scope, period), and ``VALIDATION`` for every input the pre-checks refuse
— the design's validation code, which an earlier change §1 calls ``INVALID_INPUT``;
there is no such member of ``helpdesk.errors.ErrorCode``.

Fixture cost: everything rides an earlier change's session-scoped
``mcp_fixtures`` seam — one seeded throwaway database, one dedicated Redis
index flushed per test. Per-test isolation inside the shared database is a
namespace: every case plants its OWN department, system, roles, people and
grants (uuid4-suffixed names), sends a fresh idempotency key per call, and
opens each review under a period no other review of that scope uses.

Every raw-SQL fixture binds `date` and `datetime` OBJECTS, never strings:
asyncpg binds by type and does not coerce (the project notes, an earlier change).

No pytest-asyncio: each case is a sync function driving one ``asyncio.run``.
"""

import asyncio
import itertools
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from helpdesk.models import PERIOD_LENGTH, ReviewFlag
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
from mcp_server.tools import reviews
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from scripts.seed import CUAN, NORDLICHT, TenantRows, generate_tenant

OPEN = "open_access_review"
LIST = "list_review_items"

# The audit contract as literals, never imports: reading these strings from
# the module under test would let a rename pass unnoticed.
REVIEW_OPENED_EVENT = "review.opened"
SUCCEEDED_EVENT = "tool.succeeded"
DENIED_EVENT = "tool.denied"
REPLAYED_EVENT = "tool.replayed"
FAILED_EVENT = "tool.failed"

# the design's two review rows: five principals hold a cell on each, two hold
# none. `hr` DOES hold one here (unlike the approval rows), and `auditor` does
# not (unlike the audit row), so a row copied from either by habit fails.
ALLOWED_PRINCIPALS = ("manager", "system_owner", "hr", "it_admin", "platform_admin")
DENIED_PRINCIPALS = ("employee", "auditor")

# The three principals whose the design cell is `Scope.ANY`.
ANY_SCOPE_PRINCIPALS = ("hr", "it_admin", "platform_admin")

ACTIVE_STATUS = "active"
PLANTED_START = date(2019, 3, 7)
PLANTED_PASSWORD_HASH = "not-a-real-hash-this-user-never-logs-in"
GRANTED_AT = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
FAR_EXPIRY = datetime(2099, 6, 1, 12, 0, tzinfo=UTC)

# Decision H's shape, and the values around it. `2026-Q1` is the accepted
# baseline every rejected row is measured against.
VALID_PERIOD = "2026-Q1"

# Unique periods for cases that open more than one review over one scope: the
# year is what varies, because decision H's shape admits four quarters.
_PERIOD_YEARS = itertools.count(2101)


def _period() -> str:
    return f"{next(_PERIOD_YEARS)}-Q1"


def _key() -> str:
    """A unique idempotency key: half the per-test namespace."""
    return f"fake-key-{uuid.uuid4().hex}"


# --- raw-SQL probes (reading and writing around the tools, never through them) --


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


@dataclass(frozen=True)
class Scope:
    """One freshly planted department, system, cast and grant, owned by a case."""

    tenant_id: uuid.UUID
    department_id: uuid.UUID
    manager: SeededUser
    system_id: uuid.UUID
    owner: SeededUser
    member_id: uuid.UUID
    role_id: uuid.UUID
    grant_id: uuid.UUID


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
            "email": f"fake.reviewer.{uuid.uuid4().hex}@fake.invalid",
            "display_name": "Fake Reviewer",
            "password_hash": PLANTED_PASSWORD_HASH,
            "principals": ["employee"],
            "status": ACTIVE_STATUS,
            # A `date` object, never the string: asyncpg binds by type.
            "start_date": PLANTED_START,
        },
    )
    return user_id


def _plant_department(database_url: str, tenant_id: uuid.UUID) -> uuid.UUID:
    department_id = uuid.uuid4()
    _execute(
        database_url,
        "INSERT INTO departments (id, tenant_id, name, manager_user_id, created_at, updated_at)"
        " VALUES (:id, :tenant, :name, NULL, now(), now())",
        {
            "id": department_id,
            "tenant": tenant_id,
            "name": f"Fake Review Dept {uuid.uuid4().hex[:12]}",
        },
    )
    return department_id


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
            "name": "Fake Review System",
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
            "name": "Fake Review Role",
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


def _handle(
    env: SeededMcp, tenant_id: uuid.UUID, user_id: uuid.UUID, department_id: uuid.UUID
) -> SeededUser:
    """A token-ready handle for a planted user."""
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
        display_name="Fake Reviewer",
        department_id=department_id,
        principals=("employee",),
        status=ACTIVE_STATUS,
    )


def plant_scope(env: SeededMcp, *, tenant_id: uuid.UUID | None = None) -> Scope:
    """A department with a manager, a system with an owner, and one live grant.

    Everything is planted fresh per case, so the only grant a review over this
    scope can collect is this one and the item counts below are exact.
    """
    tenant = env.tenant_a.tenant_id if tenant_id is None else tenant_id
    url = env.database_url
    department_id = _plant_department(url, tenant)
    manager_id = _plant_user(url, tenant, department_id)
    _execute(
        url,
        "UPDATE departments SET manager_user_id = :manager WHERE id = :id",
        {"manager": manager_id, "id": department_id},
    )
    owner_id = _plant_user(url, tenant, department_id)
    system_id = _plant_system(url, tenant, owner_id)
    role_id = _plant_role(url, tenant, system_id)
    member_id = _plant_user(url, tenant, department_id)
    grant_id = _plant_grant(url, tenant, member_id, role_id)
    return Scope(
        tenant_id=tenant,
        department_id=department_id,
        manager=_handle(env, tenant, manager_id, department_id),
        system_id=system_id,
        owner=_handle(env, tenant, owner_id, department_id),
        member_id=member_id,
        role_id=role_id,
        grant_id=grant_id,
    )


def _reviews_of(database_url: str, *, department_id: uuid.UUID) -> list[Any]:
    return _rows(
        database_url,
        "SELECT id, period, status, reviewer_user_id FROM access_reviews"
        " WHERE department_id = :department ORDER BY created_at, id",
        {"department": department_id},
    )


def _reviews_of_system(database_url: str, *, system_id: uuid.UUID) -> list[Any]:
    return _rows(
        database_url,
        "SELECT id, period FROM access_reviews WHERE system_id = :system",
        {"system": system_id},
    )


def _item_count(database_url: str, review_id: uuid.UUID) -> int:
    rows = _rows(
        database_url,
        "SELECT count(*) FROM review_items WHERE review_id = :review",
        {"review": review_id},
    )
    return int(rows[0][0])


def _audit_event_types(database_url: str, tenant_id: uuid.UUID) -> list[str]:
    rows = _rows(
        database_url,
        "SELECT event_type FROM audit_events WHERE tenant_id = :tenant ORDER BY seq",
        {"tenant": tenant_id},
    )
    return [str(row[0]) for row in rows]


def _opened_events(database_url: str, tenant_id: uuid.UUID) -> list[Any]:
    return _rows(
        database_url,
        "SELECT subject_type, subject_id, payload FROM audit_events WHERE tenant_id = :tenant"
        " AND event_type = :event_type ORDER BY seq",
        {"tenant": tenant_id, "event_type": REVIEW_OPENED_EVENT},
    )


def _plant_review(
    database_url: str,
    tenant_id: uuid.UUID,
    *,
    department_id: uuid.UUID,
    reviewer_user_id: uuid.UUID,
) -> uuid.UUID:
    """One committed `access_reviews` row, written around the tools.

    Used for the OTHER tenant only: a review the tools must never reach has to
    be a real row there, or a `NOT_FOUND` would prove nothing.
    """
    review_id = uuid.uuid4()
    _execute(
        database_url,
        "INSERT INTO access_reviews (id, tenant_id, reviewer_user_id, department_id, system_id,"
        " period, status, due_at, completed_at, created_at, updated_at)"
        " VALUES (:id, :tenant, :reviewer, :department, NULL, :period, 'open', NULL, NULL,"
        " now(), now())",
        {
            "id": review_id,
            "tenant": tenant_id,
            "reviewer": reviewer_user_id,
            "department": department_id,
            "period": VALID_PERIOD,
        },
    )
    return review_id


# --- arguments ------------------------------------------------------------------


def _open_args(
    *,
    department_id: uuid.UUID | str | None = None,
    system_id: uuid.UUID | str | None = None,
    period: str | None = None,
    idempotency_key: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "period": _period() if period is None else period,
        "idempotency_key": _key() if idempotency_key is None else idempotency_key,
        **extra,
    }
    if department_id is not None:
        arguments["department_id"] = str(department_id)
    if system_id is not None:
        arguments["system_id"] = str(system_id)
    return arguments


def _structured(result: Any) -> dict[str, Any]:
    assert result.is_error is False, result.content
    assert result.structured_content is not None, "a successful call carries structured content"
    content: dict[str, Any] = result.structured_content
    return content


def _response_text(result: Any) -> str:
    """The single text block of a successful result, for byte-identity checks."""
    assert result.is_error is False, result.content
    assert len(result.content) == 1, result.content
    return str(result.content[0].text)


def _tenant_rows(slug: str) -> TenantRows:
    return generate_tenant({CUAN.slug: CUAN, NORDLICHT.slug: NORDLICHT}[slug])


def _scope_arguments_for(principal: str, scope: Scope) -> dict[str, Any]:
    """The scope a principal's own the design cell can actually reach.

    `system_owner` holds `own_systems` and nothing else, so the only review it
    can open is over a system it owns; `manager` holds `own_department`, so
    only over its own department. The three `any` principals are given the
    department scope.
    """
    if principal == "system_owner":
        return _open_args(system_id=scope.system_id)
    return _open_args(department_id=scope.department_id)


def _caller_for(principal: str, scope: Scope) -> SeededUser:
    """Who calls: the person whose directory row satisfies that principal's cell."""
    return scope.owner if principal == "system_owner" else scope.manager


# --- the bounds ----------------------------------------


def test_the_review_bounds_are_exactly_one_fifty_and_two_hundred() -> None:
    """Decision I, pinned exactly. An unbounded item list is an unbounded
    response over the wire, and a default that drifts changes what every
    caller who omits `limit` receives. `PERIOD_LENGTH` is pinned here too
    because the input model checks the client's string against it, and every
    other case in this file reads it as a symbol."""
    assert reviews.LIMIT_MIN == 1
    assert reviews.LIMIT_DEFAULT == 50
    assert reviews.LIMIT_MAX == 200
    assert PERIOD_LENGTH == 16
    # The pre-check is sized by the COLUMN's own constant, not by a literal
    # retyped beside it: widening one without the other fails here (standing
    # rule 1).
    assert (
        reviews.OpenAccessReviewInput.model_fields["period"].metadata[-1].max_length
        == PERIOD_LENGTH
    )


def test_the_default_limit_is_read_off_the_input_model() -> None:
    """Catches the constant and the model disagreeing: a `Field(default=100)`
    beside a `LIMIT_DEFAULT` of 50 leaves the test above green and every real
    caller receiving a different number of items than the plan says."""
    assert reviews.ListReviewItemsInput(review_id=uuid.uuid4()).limit == reviews.LIMIT_DEFAULT


# --- the design's allow set and its refusal direction (criterion 4) -----------------


@pytest.mark.parametrize("principal", ALLOWED_PRINCIPALS)
def test_each_permitted_principal_opens_a_review_in_their_scope(
    mcp_env: SeededMcp,  # noqa: F811
    principal: str,
) -> None:
    """The allow direction every refusal below depends on, one cell of SPEC
    §7's `open_access_review` row at a time.

    Catches a registry entry wired to the wrong matrix member: `offboard_user`
    admits only `hr` and `platform_admin`, `approve_request` admits neither
    `hr` nor `auditor`, and this row admits five — so a member borrowed from
    either fails here or in the denial case below. The token carries exactly
    ONE principal, so nothing but that principal's own cell can admit the
    call, and each caller is given a scope its own cell reaches, so a
    `system_owner` passing on a department review (or a `manager` on a system
    review) would be a widened scope rather than a passing test.

    The review row is re-read with no tenant filter and asserted in the
    TOKEN's tenant, with no tenant argument anywhere in the input (hard
    rule 6).
    """
    scope = plant_scope(mcp_env)
    caller = _caller_for(principal, scope)
    arguments = _scope_arguments_for(principal, scope)

    payload = _structured(
        call_tool(bearer(token_for(caller, principals=(principal,))), OPEN, arguments)
    )

    assert payload["period"] == arguments["period"]
    assert payload["item_count"] == 1
    review_id = uuid.UUID(str(payload["review_id"]))
    assert _item_count(mcp_env.database_url, review_id) == 1
    if principal == "system_owner":
        assert [
            str(row[0])
            for row in _reviews_of_system(mcp_env.database_url, system_id=scope.system_id)
        ] == [str(review_id)]
    else:
        assert [
            str(row[0])
            for row in _reviews_of(mcp_env.database_url, department_id=scope.department_id)
        ] == [str(review_id)]


@pytest.mark.parametrize("principal", DENIED_PRINCIPALS)
def test_the_two_denied_principals_open_nothing(
    mcp_env: SeededMcp,  # noqa: F811
    principal: str,
) -> None:
    """the design's two empty cells on this row, and the record they leave.

    Every argument is valid and the department resolves in the caller's OWN
    tenant, so neither the `NOT_FOUND` lookup nor the duplicate refusal can
    produce this answer — only a permission decision can. `tool.denied` rather
    than `tool.failed`: a permission decision filed as an outage is a false
    record, and a count-only assertion cannot tell them apart.

    This does NOT claim the pipeline's RBAC stage as its kill (standing rule
    20): the handler re-runs `rbac.authorize` for the caller's scopes, so a
    principal the matrix refuses is refused there too and deleting the
    pipeline stage changes no answer. What it pins is that these two
    principals are refused at all, and that the refusal writes nothing.
    """
    scope = plant_scope(mcp_env)
    caller = scope.manager
    events_before = _audit_event_types(mcp_env.database_url, scope.tenant_id)

    result = call_tool(
        bearer(token_for(caller, principals=(principal,))),
        OPEN,
        _open_args(department_id=scope.department_id),
    )

    assert error_code(result) == "FORBIDDEN"
    assert _reviews_of(mcp_env.database_url, department_id=scope.department_id) == []
    assert _audit_event_types(mcp_env.database_url, scope.tenant_id) == [
        *events_before,
        DENIED_EVENT,
    ]


@pytest.mark.parametrize("principal", ALLOWED_PRINCIPALS)
def test_each_permitted_principal_lists_the_items_of_a_review_in_their_scope(
    mcp_env: SeededMcp,  # noqa: F811
    principal: str,
) -> None:
    """The same allow direction for the read half, which has its own the design
    row and could be wired to the wrong matrix member independently.

    A read wired to `open_access_review`'s member would pass every case in
    this file except a future divergence between the two rows; what this
    actually pins is that all five principals reach the read, and that the
    read answers the ITEMS of the named review — the grant id is asserted, so
    a handler returning an empty list for everybody cannot pass.
    """
    scope = plant_scope(mcp_env)
    caller = _caller_for(principal, scope)
    opened = _structured(
        call_tool(
            bearer(token_for(caller, principals=(principal,))),
            OPEN,
            _scope_arguments_for(principal, scope),
        )
    )

    payload = _structured(
        call_tool(
            bearer(token_for(caller, principals=(principal,))),
            LIST,
            {"review_id": str(opened["review_id"])},
        )
    )

    assert [str(item["grant_id"]) for item in payload["items"]] == [str(scope.grant_id)]
    assert payload["items"][0]["flag"] in {flag.value for flag in ReviewFlag}


@pytest.mark.parametrize("principal", DENIED_PRINCIPALS)
def test_the_two_denied_principals_cannot_list_items(
    mcp_env: SeededMcp,  # noqa: F811
    principal: str,
) -> None:
    """The read's own denial direction: catches `list_review_items` registered
    without a matrix member of its own (or with one of the wide rows), which
    would hand an `employee` the whole worklist of a review naming somebody
    else's access."""
    scope = plant_scope(mcp_env)
    opened = _structured(
        call_tool(
            bearer(token_for(scope.manager, principals=("manager",))),
            OPEN,
            _open_args(department_id=scope.department_id),
        )
    )

    result = call_tool(
        bearer(token_for(scope.manager, principals=(principal,))),
        LIST,
        {"review_id": str(opened["review_id"])},
    )

    assert error_code(result) == "FORBIDDEN"


# --- scope, per principal (criterion 4, a project rule) ------------------------


def test_a_manager_may_not_open_a_review_for_another_department(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """a project rule: the design's `own_department` scope is the ONLY gate this
    case can fail on.

    The caller carries `manager`, which the matrix admits on this row, and the
    department is a real row in the caller's OWN tenant, so neither the matrix
    nor the `NOT_FOUND` lookup can refuse it — deleting the scope comparison
    is the one change that turns this into an opened review. The same caller
    then opens a review over their OWN department in the same test, so a
    handler that refuses every manager passes nothing.

    The department the comparison uses is read from the DIRECTORY, not from
    the token's `department_id` claim; that direction is pinned by the
    directory case below.
    """
    own = plant_scope(mcp_env)
    other = plant_scope(mcp_env)

    refused = call_tool(
        bearer(token_for(own.manager, principals=("manager",))),
        OPEN,
        _open_args(department_id=other.department_id),
    )
    allowed = call_tool(
        bearer(token_for(own.manager, principals=("manager",))),
        OPEN,
        _open_args(department_id=own.department_id),
    )

    assert error_code(refused) == "FORBIDDEN"
    assert _reviews_of(mcp_env.database_url, department_id=other.department_id) == []
    assert allowed.is_error is False, allowed.content
    assert len(_reviews_of(mcp_env.database_url, department_id=own.department_id)) == 1


def test_a_system_owner_may_not_open_a_review_for_a_system_they_do_not_own(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """The `own_systems` half of the same gate, which the department case
    cannot reach.

    A handler that implemented `own_department` and left `own_systems`
    answering True for every system passes the case above and fails here. The
    caller owns a real system in the same tenant and opens a review over it in
    the same test, so the refusal is about WHICH system and not about the
    principal.
    """
    own = plant_scope(mcp_env)
    other = plant_scope(mcp_env)

    refused = call_tool(
        bearer(token_for(own.owner, principals=("system_owner",))),
        OPEN,
        _open_args(system_id=other.system_id),
    )
    allowed = call_tool(
        bearer(token_for(own.owner, principals=("system_owner",))),
        OPEN,
        _open_args(system_id=own.system_id),
    )

    assert error_code(refused) == "FORBIDDEN"
    assert _reviews_of_system(mcp_env.database_url, system_id=other.system_id) == []
    assert allowed.is_error is False, allowed.content
    assert len(_reviews_of_system(mcp_env.database_url, system_id=own.system_id)) == 1


def test_a_manager_may_not_list_the_items_of_another_departments_review(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """The read's own scope gate, which the open path cannot pin.

    A `list_review_items` that checked only "does this review exist in my
    tenant" would hand a manager every grant of a department they have no
    reach over — the worklist is the sensitive part, not the opening. The
    review is opened by an `hr` caller so the row genuinely exists and is
    populated, and the same manager lists their OWN review afterwards, so the
    refusal is about the SCOPE of the review and not about the caller.
    """
    own = plant_scope(mcp_env)
    other = plant_scope(mcp_env)
    hr_caller = other.manager
    foreign = _structured(
        call_tool(
            bearer(token_for(hr_caller, principals=("hr",))),
            OPEN,
            _open_args(department_id=other.department_id),
        )
    )
    mine = _structured(
        call_tool(
            bearer(token_for(own.manager, principals=("manager",))),
            OPEN,
            _open_args(department_id=own.department_id),
        )
    )

    refused = call_tool(
        bearer(token_for(own.manager, principals=("manager",))),
        LIST,
        {"review_id": str(foreign["review_id"])},
    )
    allowed = call_tool(
        bearer(token_for(own.manager, principals=("manager",))),
        LIST,
        {"review_id": str(mine["review_id"])},
    )

    assert error_code(refused) == "FORBIDDEN"
    assert allowed.is_error is False, allowed.content
    assert len(_structured(allowed)["items"]) == 1


@pytest.mark.parametrize("principal", ANY_SCOPE_PRINCIPALS)
def test_an_any_scope_principal_reaches_a_scope_they_have_no_relation_to(
    mcp_env: SeededMcp,  # noqa: F811
    principal: str,
) -> None:
    """The other direction of the scope gate: `Scope.ANY` really is anything
    in the tenant.

    The caller is not the department's manager, not the system's owner and not
    even a member of the department, so a scope check that compared
    departments unconditionally — the natural mistake once the manager case
    above exists — would refuse all three of these principals and leave HR
    unable to open the reviews the design says they run.
    """
    scope = plant_scope(mcp_env)
    stranger = plant_scope(mcp_env)

    opened = call_tool(
        bearer(token_for(stranger.manager, principals=(principal,))),
        OPEN,
        _open_args(department_id=scope.department_id),
    )
    listed = call_tool(
        bearer(token_for(stranger.manager, principals=(principal,))),
        LIST,
        {"review_id": str(_structured(opened)["review_id"])},
    )

    assert _structured(opened)["item_count"] == 1
    assert len(_structured(listed)["items"]) == 1


def test_the_managers_department_comes_from_the_directory_not_the_token(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """a project rule on the department comparison itself.

    The token is minted with a `department_id` claim naming the department
    under review while the caller's DIRECTORY row says otherwise, so a scope
    check reading the claim admits the call and one reading the directory
    refuses it. Every other scope case here passes whichever source is used,
    because both agree; only this one separates them. A manager transferred
    after their token was issued must not keep the reach of the department
    they left.
    """
    own = plant_scope(mcp_env)
    other = plant_scope(mcp_env)
    forged = SeededUser(
        tenant_id=own.manager.tenant_id,
        tenant_slug=own.manager.tenant_slug,
        user_id=own.manager.user_id,
        email=own.manager.email,
        display_name=own.manager.display_name,
        # The claim lies: the directory row still says `own.department_id`.
        department_id=other.department_id,
        principals=own.manager.principals,
        status=own.manager.status,
    )

    result = call_tool(
        bearer(token_for(forged, principals=("manager",))),
        OPEN,
        _open_args(department_id=other.department_id),
    )

    assert error_code(result) == "FORBIDDEN"
    assert _reviews_of(mcp_env.database_url, department_id=other.department_id) == []


# --- the absences (criterion 7) --------------------------------------------------


def test_a_missing_and_another_tenants_scope_answer_the_same_not_found(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches a scope lookup that escapes the tenant guard, and one that
    distinguishes the two absences.

    If another tenant's department answered anything but the unknown id's
    answer, `department_id` would be a cross-tenant existence oracle: an HR
    officer could enumerate which departments exist in a tenant they cannot
    see. The two answers are compared as whole strings, so a code with an
    explanation appended fails. The caller holds `platform_admin`
    (`Scope.ANY`), so the scope gate cannot be what refuses either call — the
    widest principal the design has is still confined to its own tenant.
    """
    caller = plant_scope(mcp_env).manager
    other_tenant = mcp_env.tenant_b
    foreign_department = _tenant_rows(other_tenant.tenant_slug).department_ids["Engineering"]
    foreign_system = _tenant_rows(other_tenant.tenant_slug).system_ids["vpn"]

    missing_department = call_tool(
        bearer(token_for(caller, principals=("platform_admin",))),
        OPEN,
        _open_args(department_id=uuid.uuid4()),
    )
    foreign_department_result = call_tool(
        bearer(token_for(caller, principals=("platform_admin",))),
        OPEN,
        _open_args(department_id=foreign_department),
    )
    missing_system = call_tool(
        bearer(token_for(caller, principals=("platform_admin",))),
        OPEN,
        _open_args(system_id=uuid.uuid4()),
    )
    foreign_system_result = call_tool(
        bearer(token_for(caller, principals=("platform_admin",))),
        OPEN,
        _open_args(system_id=foreign_system),
    )

    assert error_code(missing_department) == "NOT_FOUND"
    assert error_code(foreign_department_result) == error_code(missing_department)
    assert error_code(missing_system) == error_code(missing_department)
    assert error_code(foreign_system_result) == error_code(missing_department)
    assert (
        _rows(
            mcp_env.database_url,
            "SELECT count(*) FROM access_reviews WHERE tenant_id = :tenant",
            {"tenant": other_tenant.tenant_id},
        )[0][0]
        == 0
    ), "nothing may be written into the other tenant"


def test_a_missing_and_another_tenants_review_answer_the_same_not_found(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """The same two absences for `list_review_items`.

    The other tenant's review is a REAL row there, so the refusal has to come
    from the tenant guard rather than from an id nobody could resolve anyway —
    a random uuid would answer `NOT_FOUND` whether the guard exists or not.
    Without this pair a review id is an existence oracle across tenants, and
    the items themselves are the access of people the caller cannot see.
    """
    caller = plant_scope(mcp_env).manager
    other_tenant = mcp_env.tenant_b
    foreign_review = _plant_review(
        mcp_env.database_url,
        other_tenant.tenant_id,
        department_id=other_tenant.department_id,
        reviewer_user_id=other_tenant.user_id,
    )

    missing = call_tool(
        bearer(token_for(caller, principals=("platform_admin",))),
        LIST,
        {"review_id": str(uuid.uuid4())},
    )
    foreign = call_tool(
        bearer(token_for(caller, principals=("platform_admin",))),
        LIST,
        {"review_id": str(foreign_review)},
    )

    assert error_code(missing) == "NOT_FOUND"
    assert error_code(foreign) == error_code(missing)


# --- decision G through the wire (criterion 6) -----------------------------------


def test_a_second_review_of_the_same_scope_and_period_is_refused_policy_blocked(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Decision G at the boundary, with two DIFFERENT idempotency keys so the
    guard cannot be what refuses the second call.

    Catches the "open a second one anyway" behaviour, which splits a
    reviewer's worklist in two and lets one grant be decided twice in opposite
    directions. `POLICY_BLOCKED` and not `VALIDATION`: the arguments are
    perfectly well formed, and calling this a schema problem sends the caller
    to fix the wrong thing. The audit delta is exactly `tool.failed`, so a
    refusal that still wrote a `review.opened` fails here.
    """
    scope = plant_scope(mcp_env)
    repeated = _period()
    first = call_tool(
        bearer(token_for(scope.manager, principals=("manager",))),
        OPEN,
        _open_args(department_id=scope.department_id, period=repeated),
    )
    events_between = _audit_event_types(mcp_env.database_url, scope.tenant_id)

    second = call_tool(
        bearer(token_for(scope.manager, principals=("manager",))),
        OPEN,
        _open_args(department_id=scope.department_id, period=repeated),
    )

    assert first.is_error is False, first.content
    assert error_code(second) == "POLICY_BLOCKED"
    assert len(_reviews_of(mcp_env.database_url, department_id=scope.department_id)) == 1
    assert _audit_event_types(mcp_env.database_url, scope.tenant_id) == [
        *events_between,
        FAILED_EVENT,
    ]


# --- the input surface (criterion 5 and criterion 8) -----------------------------


def test_neither_scope_id_and_both_scope_ids_are_refused_before_any_write(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Criterion 5, both halves.

    Neither id set is a call with no subject at all; both set is a caller who
    believes they are reviewing a department AND a system, and whichever one
    the handler silently picked would be the wrong answer half the time. The
    table's own check constraint would eventually refuse both, but only as an
    `IntegrityError` mid-transaction — a free-text database error across a
    boundary whose contract is the eight typed the design codes — so this must
    be refused by the input model first, which is what the "no row written"
    assertion pins. `VALIDATION` is the design's code for this (an earlier change §1
    calls it `INVALID_INPUT`; `helpdesk.errors.ErrorCode` has no such member).
    """
    scope = plant_scope(mcp_env)
    caller = bearer(token_for(scope.manager, principals=("manager",)))

    neither = call_tool(caller, OPEN, _open_args())
    both = call_tool(
        caller, OPEN, _open_args(department_id=scope.department_id, system_id=scope.system_id)
    )

    assert error_code(neither) == "VALIDATION"
    assert error_code(both) == "VALIDATION"
    assert _reviews_of(mcp_env.database_url, department_id=scope.department_id) == []
    assert _reviews_of_system(mcp_env.database_url, system_id=scope.system_id) == []


PERIOD_CASES: list[tuple[str, str, bool]] = [
    ("accepted", VALID_PERIOD, True),
    ("trailing newline", VALID_PERIOD + "\n", False),
    ("trailing CRLF", VALID_PERIOD + "\r\n", False),
    ("trailing space", VALID_PERIOD + " ", False),
    ("leading newline", "\n" + VALID_PERIOD, False),
    ("quarter zero", "2026-Q0", False),
    ("quarter five", "2026-Q5", False),
    ("lowercase quarter", "2026-q1", False),
    ("three-digit year", "226-Q1", False),
    ("empty", "", False),
    ("at the column width, wrong shape", "2" * PERIOD_LENGTH, False),
    ("one over the column width", "2" * (PERIOD_LENGTH + 1), False),
]


@pytest.mark.parametrize(
    ("period_value", "accepted"),
    [case[1:] for case in PERIOD_CASES],
    ids=[case[0] for case in PERIOD_CASES],
)
def test_the_period_bounds_are_inclusive_and_reject_before_any_write(
    mcp_env: SeededMcp,  # noqa: F811
    period_value: str,
    accepted: bool,
) -> None:
    """Decision H, and a project rule and 4.

    The three trailing rows are the whole point: `Field(pattern=...)` matches
    a SUBSTRING, so a period ending in a newline, a CRLF or a space is
    accepted by it and lands in the column — a label no query for `2026-Q1`
    ever matches again, and an audit payload nobody can join on. Only a
    `field_validator` calling `re.fullmatch` refuses them, and nothing else in
    this file can tell the two apart.

    The two width rows pin the relationship between the check and the store: `PERIOD_LENGTH` is the column's own size, and the shape
    check is strictly tighter than it, so the value at the width and the value
    one over it are BOTH refused by the shape long before Postgres could
    truncate anything. There is therefore no accepted 16-character period —
    an earlier change §6 asks for one, and decision H's `\\d{4}-Q[1-4]` makes every
    accepted value exactly seven characters.

    The accepted row is what stops the table passing vacuously: it proves the
    same call minus the one defect goes through. Every refused row re-reads
    the department's reviews, so the check has to run BEFORE any write.
    """
    scope = plant_scope(mcp_env)

    result = call_tool(
        bearer(token_for(scope.manager, principals=("manager",))),
        OPEN,
        _open_args(department_id=scope.department_id, period=period_value),
    )

    rows = _reviews_of(mcp_env.database_url, department_id=scope.department_id)
    if accepted:
        assert result.is_error is False, result.content
        assert len(rows) == 1
        assert str(rows[0][1]) == period_value
        assert len(period_value) <= PERIOD_LENGTH
    else:
        assert error_code(result) == "VALIDATION"
        assert rows == []


LIMIT_CASES: list[tuple[str, int, bool]] = [
    ("one", 1, True),
    ("two hundred", 200, True),
    ("zero", 0, False),
    ("two hundred and one", 201, False),
    ("negative", -1, False),
]


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
    """Decision I's bound, both sides of both ends.

    A one-sided assertion cannot tell an inclusive bound from an exclusive
    one: `gt=1` accepts 200 and refuses 1, and `lt=200` accepts 1 and refuses
    200, so both accepted rows are needed to pin `ge`/`le`. Zero is the row
    that catches a missing lower bound, which would let a caller ask for an
    empty page and read it as "this review has no items"; 201 catches a
    missing upper bound, which is an unbounded response over the wire.
    """
    scope = plant_scope(mcp_env)
    opened = _structured(
        call_tool(
            bearer(token_for(scope.manager, principals=("manager",))),
            OPEN,
            _open_args(department_id=scope.department_id),
        )
    )

    result = call_tool(
        bearer(token_for(scope.manager, principals=("manager",))),
        LIST,
        {"review_id": str(opened["review_id"]), "limit": limit},
    )

    if accepted:
        assert result.is_error is False, result.content
        assert len(_structured(result)["items"]) == 1
    else:
        assert error_code(result) == "VALIDATION"


# --- replay (criterion 6) --------------------------------------------------------


def test_a_replayed_key_returns_the_same_response_and_opens_no_second_review(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches a retry that re-runs the body.

    The second call under the same key must not reach decision G's duplicate
    check: by then an open review for that (scope, period) exists, so a re-run
    answers `POLICY_BLOCKED` for a call the caller already saw succeed. Byte
    identity is asserted, not just equality of meaning, so a response rebuilt
    rather than replayed fails; the audit delta is exactly `tool.replayed`, so
    a second `review.opened` — a false record of a review that was never
    opened twice — fails too.
    """
    scope = plant_scope(mcp_env)
    arguments = _open_args(department_id=scope.department_id, idempotency_key=_key())
    caller = bearer(token_for(scope.manager, principals=("manager",)))

    first = call_tool(caller, OPEN, arguments)
    events_between = _audit_event_types(mcp_env.database_url, scope.tenant_id)
    second = call_tool(caller, OPEN, arguments)

    assert _response_text(second) == _response_text(first)
    assert second.structured_content == first.structured_content
    assert len(_reviews_of(mcp_env.database_url, department_id=scope.department_id)) == 1
    assert _audit_event_types(mcp_env.database_url, scope.tenant_id) == [
        *events_between,
        REPLAYED_EVENT,
    ]


# --- the audit sequence (criterion 3, a project rule) --------------------------


def test_the_audit_sequence_is_review_opened_then_tool_succeeded(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Criterion 3 as an exact ordered list, which is the only place the order
    of the two events is observable at all.

    Catches the registry declaring `review.opened` as this tool's event
    instead of the generic `tool.succeeded`: the pipeline writes its event
    with the TOOL as subject and a fixed payload, so the log would record that
    somebody called a tool and not what was reviewed, and
    the review's own event would be missing entirely. Catches a summary event
    written first, which would have to name an `item_count` nothing had
    computed yet.

    The subject and the seven payload keys are asserted here as well as in the
    core test, because only the transport can show that the event the PIPELINE
    adds did not overwrite or duplicate the one the handler wrote.
    """
    scope = plant_scope(mcp_env)
    used_period = _period()
    events_before = _audit_event_types(mcp_env.database_url, scope.tenant_id)

    payload = _structured(
        call_tool(
            bearer(token_for(scope.manager, principals=("manager",))),
            OPEN,
            _open_args(department_id=scope.department_id, period=used_period),
        )
    )

    assert _audit_event_types(mcp_env.database_url, scope.tenant_id) == [
        *events_before,
        REVIEW_OPENED_EVENT,
        SUCCEEDED_EVENT,
    ]
    record = _opened_events(mcp_env.database_url, scope.tenant_id)[-1]
    assert str(record.subject_type) == "access_review"
    assert str(record.subject_id) == str(payload["review_id"])
    event_payload = dict(record.payload)
    assert set(event_payload) == {
        "review_id",
        "department_id",
        "system_id",
        "period",
        "reviewer_user_id",
        "item_count",
        "flag_counts",
    }
    assert event_payload["review_id"] == str(payload["review_id"])
    assert event_payload["department_id"] == str(scope.department_id)
    assert event_payload["system_id"] is None
    assert event_payload["period"] == used_period
    assert event_payload["reviewer_user_id"] == str(scope.manager.user_id)
    assert event_payload["item_count"] == 1


# =================================================================================
# An earlier change, sections 1 and 6: `decide_review_item`, through the transport.
#
# Surface under test (written before it exists):
#
# ``mcp_server.tools.reviews``
#     ``DecideReviewItemInput(MutatingToolInput)`` — ``item_id: uuid.UUID``
#     and ``decision: ReviewDecision`` (the StrEnum, so no client string can
#     reach the column), plus the idempotency key and ``extra="forbid"`` of
#     ``MutatingToolInput``. No tenant and no user argument: both come from
#     the token.
#     ``DecideReviewItemOutput`` — ``item_id``, ``review_id``, ``decision``,
#     ``decided_at``, ``revoked_grant_count`` and ``review_complete``.
#     ``decide_review_item`` (MUTATING, registry ``event_type`` the generic
#     ``tool.succeeded``: the handler's own ``review.item_decided`` names the
#     ITEM and lands first).
#
# Scope is checked BEFORE the item's existence (decision H), the same order
# as 12c's two tools, so an item id is neither an existence nor a state
# oracle for a caller whose the design row does not reach it.
# =================================================================================

DECIDE = "decide_review_item"

# The audit contract as literals, never imports (same reason as above).
REVIEW_ITEM_DECIDED_EVENT = "review.item_decided"
GRANT_REVOKED_EVENT = "grant.revoked"

KEEP = "keep"
REVOKE = "revoke"


def _decide_args(
    item_id: uuid.UUID | str,
    decision: str,
    *,
    idempotency_key: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "item_id": str(item_id),
        "decision": decision,
        "idempotency_key": _key() if idempotency_key is None else idempotency_key,
        **extra,
    }


def _item_rows(database_url: str, review_id: uuid.UUID) -> list[Any]:
    return _rows(
        database_url,
        "SELECT id, grant_id, flag, decision, decided_at FROM review_items"
        " WHERE review_id = :review ORDER BY created_at, id",
        {"review": review_id},
    )


def _item_row(database_url: str, item_id: uuid.UUID | str) -> Any:
    rows = _rows(
        database_url,
        "SELECT id, grant_id, decision, decided_at FROM review_items WHERE id = :item",
        {"item": uuid.UUID(str(item_id))},
    )
    assert len(rows) == 1, f"expected exactly one item row, got {len(rows)}"
    return rows[0]


def _grant_row(database_url: str, grant_id: uuid.UUID | str) -> Any:
    rows = _rows(
        database_url,
        "SELECT status, revoked_at, revoke_reason FROM grants WHERE id = :grant",
        {"grant": uuid.UUID(str(grant_id))},
    )
    assert len(rows) == 1, f"expected exactly one grant row, got {len(rows)}"
    return rows[0]


def _review_row(database_url: str, review_id: uuid.UUID | str) -> Any:
    rows = _rows(
        database_url,
        "SELECT status, completed_at FROM access_reviews WHERE id = :review",
        {"review": uuid.UUID(str(review_id))},
    )
    assert len(rows) == 1, f"expected exactly one review row, got {len(rows)}"
    return rows[0]


def _decided_events(database_url: str, tenant_id: uuid.UUID) -> list[Any]:
    return _rows(
        database_url,
        "SELECT subject_type, subject_id, payload FROM audit_events WHERE tenant_id = :tenant"
        " AND event_type = :event_type ORDER BY seq",
        {"tenant": tenant_id, "event_type": REVIEW_ITEM_DECIDED_EVENT},
    )


def _revoked_event_count(database_url: str, tenant_id: uuid.UUID) -> int:
    return len(
        _rows(
            database_url,
            "SELECT seq FROM audit_events WHERE tenant_id = :tenant AND event_type = :event_type",
            {"tenant": tenant_id, "event_type": GRANT_REVOKED_EVENT},
        )
    )


def _plant_item(
    database_url: str,
    tenant_id: uuid.UUID,
    *,
    review_id: uuid.UUID,
    grant_id: uuid.UUID,
) -> uuid.UUID:
    """One committed `review_items` row, written around the tools.

    Used for the OTHER tenant only: an item the tools must never reach has to
    be a real row there, or a `NOT_FOUND` would prove nothing.
    """
    item_id = uuid.uuid4()
    _execute(
        database_url,
        "INSERT INTO review_items (id, tenant_id, review_id, grant_id, flag, decision,"
        " decided_at, created_at, updated_at)"
        " VALUES (:id, :tenant, :review, :grant, :flag, NULL, NULL, now(), now())",
        {
            "id": item_id,
            "tenant": tenant_id,
            "review": review_id,
            "grant": grant_id,
            "flag": ReviewFlag.OK.value,
        },
    )
    return item_id


def _open_with_item(
    env: SeededMcp, scope: Scope, *, principal: str = "manager"
) -> tuple[uuid.UUID, uuid.UUID]:
    """Open a review over `scope` and return (review_id, its one item id).

    Driven through the two 12c tools rather than by planting rows, so the
    item under decision is exactly the item a reviewer would be shown.
    """
    token = bearer(token_for(_caller_for(principal, scope), principals=(principal,)))
    opened = _structured(call_tool(token, OPEN, _scope_arguments_for(principal, scope)))
    listed = _structured(call_tool(token, LIST, {"review_id": str(opened["review_id"])}))
    assert len(listed["items"]) == 1, "premise: the planted scope holds exactly one grant"
    return uuid.UUID(str(opened["review_id"])), uuid.UUID(str(listed["items"][0]["item_id"]))


# --- the design's allow set and its refusal direction (criterion 10) ----------------


@pytest.mark.parametrize("principal", ALLOWED_PRINCIPALS)
def test_each_permitted_principal_decides_an_item_in_their_scope(
    mcp_env: SeededMcp,  # noqa: F811
    principal: str,
) -> None:
    """The allow direction every refusal below depends on, one cell of SPEC
    §7's `decide_review_item` row at a time.

    Catches a registry entry wired to the wrong matrix member: `offboard_user`
    admits only `hr` and `platform_admin` and `approve_request` admits neither
    `hr` nor `auditor`, while this row admits five — so a member borrowed from
    either fails here or in the denial case below. The token carries exactly
    ONE principal, so nothing but that principal's own cell can admit the
    call, and each caller is given a review its own cell reaches, so a
    `system_owner` passing on a department review would be a widened scope
    rather than a passing test.

    The item row is re-read with no tenant filter, so the decision is proven
    to have landed on the row and not only in the response.
    """
    scope = plant_scope(mcp_env)
    caller = _caller_for(principal, scope)
    _review_id, item_id = _open_with_item(mcp_env, scope, principal=principal)

    payload = _structured(
        call_tool(
            bearer(token_for(caller, principals=(principal,))),
            DECIDE,
            _decide_args(item_id, KEEP),
        )
    )

    assert payload["decision"] == KEEP
    assert str(payload["item_id"]) == str(item_id)
    assert payload["revoked_grant_count"] == 0
    stored = _item_row(mcp_env.database_url, item_id)
    assert str(stored.decision) == KEEP
    assert stored.decided_at is not None


@pytest.mark.parametrize("principal", DENIED_PRINCIPALS)
def test_the_two_denied_principals_decide_nothing(
    mcp_env: SeededMcp,  # noqa: F811
    principal: str,
) -> None:
    """the design's two empty cells on this row, and the record they leave.

    The item is a real row the SAME caller's manager token just opened, in
    the caller's own tenant and own department, so neither the `NOT_FOUND`
    lookup nor the already-decided refusal can produce this answer — only a
    permission decision can. `tool.denied` rather than `tool.failed`: a
    permission decision filed as an outage is a false record.

    This does NOT claim the pipeline's RBAC stage as its kill (standing rule
    20): the handler re-runs `rbac.authorize` through `_in_scope`, so
    deleting the pipeline stage changes no answer here.
    """
    scope = plant_scope(mcp_env)
    _review_id, item_id = _open_with_item(mcp_env, scope)
    events_before = _audit_event_types(mcp_env.database_url, scope.tenant_id)

    result = call_tool(
        bearer(token_for(scope.manager, principals=(principal,))),
        DECIDE,
        _decide_args(item_id, REVOKE),
    )

    assert error_code(result) == "FORBIDDEN"
    assert _item_row(mcp_env.database_url, item_id).decision is None
    assert str(_grant_row(mcp_env.database_url, scope.grant_id).status) == ACTIVE_STATUS
    assert _audit_event_types(mcp_env.database_url, scope.tenant_id) == [
        *events_before,
        DENIED_EVENT,
    ]


# --- scope, per principal (criterion 10, a project rule) -----------------------


def test_a_manager_may_not_decide_an_item_of_another_departments_review(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """a project rule: the design's `own_department` scope is the ONLY gate
    this case can fail on.

    The caller carries `manager`, which the matrix admits on this row, and the
    item is a real undecided row in the caller's OWN tenant, so neither the
    matrix nor the `NOT_FOUND` lookup nor decision C can refuse it — deleting
    the scope comparison is the one change that turns this into a revoked
    grant in a department the caller has no reach over. The same caller then
    decides an item of their OWN review, so a handler that refuses every
    manager passes nothing.
    """
    own = plant_scope(mcp_env)
    other = plant_scope(mcp_env)
    _foreign_review, foreign_item = _open_with_item(mcp_env, other, principal="hr")
    _own_review, own_item = _open_with_item(mcp_env, own)

    refused = call_tool(
        bearer(token_for(own.manager, principals=("manager",))),
        DECIDE,
        _decide_args(foreign_item, REVOKE),
    )
    allowed = call_tool(
        bearer(token_for(own.manager, principals=("manager",))),
        DECIDE,
        _decide_args(own_item, REVOKE),
    )

    assert error_code(refused) == "FORBIDDEN"
    assert _item_row(mcp_env.database_url, foreign_item).decision is None
    assert str(_grant_row(mcp_env.database_url, other.grant_id).status) == ACTIVE_STATUS
    assert allowed.is_error is False, allowed.content
    assert str(_item_row(mcp_env.database_url, own_item).decision) == REVOKE


def test_a_system_owner_may_not_decide_an_item_of_a_system_they_do_not_own(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """The `own_systems` half of the same gate, which the department case
    cannot reach.

    A handler that implemented `own_department` and left `own_systems`
    answering True for every system passes the case above and fails here. The
    caller owns a real system in the same tenant and decides an item of their
    own system's review in the same test, so the refusal is about WHICH
    system and not about the principal.
    """
    own = plant_scope(mcp_env)
    other = plant_scope(mcp_env)
    _foreign_review, foreign_item = _open_with_item(mcp_env, other, principal="system_owner")
    _own_review, own_item = _open_with_item(mcp_env, own, principal="system_owner")

    refused = call_tool(
        bearer(token_for(own.owner, principals=("system_owner",))),
        DECIDE,
        _decide_args(foreign_item, REVOKE),
    )
    allowed = call_tool(
        bearer(token_for(own.owner, principals=("system_owner",))),
        DECIDE,
        _decide_args(own_item, REVOKE),
    )

    assert error_code(refused) == "FORBIDDEN"
    assert _item_row(mcp_env.database_url, foreign_item).decision is None
    assert allowed.is_error is False, allowed.content
    assert str(_item_row(mcp_env.database_url, own_item).decision) == REVOKE


def test_the_deciders_department_comes_from_the_directory_not_the_token(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """a project rule on the department comparison of the deciding tool itself.

    The token is minted with a `department_id` claim naming the department
    whose review holds the item, while the caller's DIRECTORY row says
    otherwise: a scope check reading the CLAIM admits the call and revokes
    somebody's access, one reading the directory refuses it. Every other
    scope case here passes whichever source is used, because both agree; only
    this one separates them. a project rule is satisfied by keeping both
    departments in ONE tenant, so the tenant guard cannot refuse this on the
    scope rule's behalf.

    A manager transferred after their token was issued must not keep the
    reach of the department they left.
    """
    own = plant_scope(mcp_env)
    other = plant_scope(mcp_env)
    _review_id, item_id = _open_with_item(mcp_env, other, principal="hr")
    forged = SeededUser(
        tenant_id=own.manager.tenant_id,
        tenant_slug=own.manager.tenant_slug,
        user_id=own.manager.user_id,
        email=own.manager.email,
        display_name=own.manager.display_name,
        # The claim lies: the directory row still says `own.department_id`.
        department_id=other.department_id,
        principals=own.manager.principals,
        status=own.manager.status,
    )

    result = call_tool(
        bearer(token_for(forged, principals=("manager",))),
        DECIDE,
        _decide_args(item_id, REVOKE),
    )

    assert error_code(result) == "FORBIDDEN"
    assert _item_row(mcp_env.database_url, item_id).decision is None
    assert str(_grant_row(mcp_env.database_url, other.grant_id).status) == ACTIVE_STATUS


# --- the absences, and the order they are answered in (criterion 8, decision H) ---


def test_a_missing_and_another_tenants_item_answer_the_same_not_found(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches an item lookup that escapes the tenant guard, and one that
    distinguishes the two absences.

    The other tenant's item is a REAL row there, on a real review over a real
    grant, so the refusal has to come from the tenant guard rather than from
    an id nobody could resolve anyway — a random uuid answers `NOT_FOUND`
    whether the guard exists or not. If the two answers differed, an item id
    would be a cross-tenant existence oracle, and a decided one would be a
    lever on access in a tenant the caller cannot see. The answers are
    compared as whole strings, so a code with an explanation appended fails.
    The caller holds `platform_admin` (`Scope.ANY`), so the scope gate cannot
    be what refuses either call.
    """
    caller = plant_scope(mcp_env).manager
    foreign_scope = plant_scope(mcp_env, tenant_id=mcp_env.tenant_b.tenant_id)
    foreign_review = _plant_review(
        mcp_env.database_url,
        mcp_env.tenant_b.tenant_id,
        department_id=foreign_scope.department_id,
        reviewer_user_id=foreign_scope.manager.user_id,
    )
    foreign_item = _plant_item(
        mcp_env.database_url,
        mcp_env.tenant_b.tenant_id,
        review_id=foreign_review,
        grant_id=foreign_scope.grant_id,
    )

    missing = call_tool(
        bearer(token_for(caller, principals=("platform_admin",))),
        DECIDE,
        _decide_args(uuid.uuid4(), REVOKE),
    )
    foreign = call_tool(
        bearer(token_for(caller, principals=("platform_admin",))),
        DECIDE,
        _decide_args(foreign_item, REVOKE),
    )

    assert error_code(missing) == "NOT_FOUND"
    assert error_code(foreign) == error_code(missing)
    assert _item_row(mcp_env.database_url, foreign_item).decision is None
    assert str(_grant_row(mcp_env.database_url, foreign_scope.grant_id).status) == ACTIVE_STATUS


def test_an_out_of_scope_item_is_refused_before_its_existence_is_tested(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Decision H, and the reason this tool joins `SCOPE_BEFORE_EXISTENCE_TOOLS`
    rather than the rbac sweep's two older buckets.

    ONE id that resolves nowhere is sent by two callers the matrix admits
    equally: the scoped `manager` must be refused `FORBIDDEN` — the scope
    question is answered from "no scope at all" before any lookup runs — and
    the `Scope.ANY` `platform_admin` must reach the `NOT_FOUND` behind it.
    Swapping the two stages is the single change that flips this: with the
    lookup first, BOTH answers become `NOT_FOUND` and the manager learns
    which item ids exist in departments they cannot reach. Neither answer
    alone pins the order, which is why both are asserted against the same id.
    """
    scope = plant_scope(mcp_env)
    unknown = uuid.uuid4()

    scoped = call_tool(
        bearer(token_for(scope.manager, principals=("manager",))),
        DECIDE,
        _decide_args(unknown, REVOKE),
    )
    wide = call_tool(
        bearer(token_for(scope.manager, principals=("platform_admin",))),
        DECIDE,
        _decide_args(unknown, REVOKE),
    )

    assert error_code(scoped) == "FORBIDDEN"
    assert error_code(wide) == "NOT_FOUND"


# --- the input surface (criterion 9) ---------------------------------------------


DECISION_CASES: list[tuple[str, str, bool]] = [
    ("keep", KEEP, True),
    ("revoke", REVOKE, True),
    ("unknown word", "delete", False),
    ("empty", "", False),
    ("trailing newline", REVOKE + "\n", False),
    ("trailing space", KEEP + " ", False),
    ("uppercase member", "KEEP", False),
]


@pytest.mark.parametrize(
    ("decision", "accepted"),
    [case[1:] for case in DECISION_CASES],
    ids=[case[0] for case in DECISION_CASES],
)
def test_the_decision_word_is_an_exact_enum_member_or_refused_before_any_write(
    mcp_env: SeededMcp,  # noqa: F811
    decision: str,
    accepted: bool,
) -> None:
    """Criterion 9. Catches `decision` typed as a bare `str`, which would let
    any client word land in the `review_items.decision` column — a value no
    query for `keep` or `revoke` ever matches again, and a decision an
    auditor cannot read.

    The trailing-newline and trailing-space rows are what separate the enum
    from a `Field(pattern=...)`, which matches a SUBSTRING and accepts both; the uppercase row catches a case-insensitive coercion.
    Both accepted rows are here so the table cannot pass vacuously, and
    because they are the only two words the column may ever hold. Every
    refused row re-reads the item and the grant, so the refusal has to happen
    BEFORE any write.
    """
    scope = plant_scope(mcp_env)
    _review_id, item_id = _open_with_item(mcp_env, scope)

    result = call_tool(
        bearer(token_for(scope.manager, principals=("manager",))),
        DECIDE,
        _decide_args(item_id, decision),
    )

    stored = _item_row(mcp_env.database_url, item_id)
    if accepted:
        assert result.is_error is False, result.content
        assert str(stored.decision) == decision
    else:
        assert error_code(result) == "VALIDATION"
        assert stored.decision is None
        assert str(_grant_row(mcp_env.database_url, scope.grant_id).status) == ACTIVE_STATUS


def test_an_extra_argument_is_refused_with_nothing_written(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """`extra="forbid"` on the mutating input, and the reason it is not
    `ignore`: an unexpected `user_id` or `tenant_id` in the arguments is a
    caller who believes they are choosing something they are not, and
    dropping it silently would end somebody's access under a success the
    caller reads as something else. Every other argument here is valid, so
    only the extras rule can refuse this call.
    """
    scope = plant_scope(mcp_env)
    _review_id, item_id = _open_with_item(mcp_env, scope)

    result = call_tool(
        bearer(token_for(scope.manager, principals=("manager",))),
        DECIDE,
        _decide_args(item_id, REVOKE, tenant_id=str(scope.tenant_id)),
    )

    assert error_code(result) == "VALIDATION"
    assert _item_row(mcp_env.database_url, item_id).decision is None
    assert str(_grant_row(mcp_env.database_url, scope.grant_id).status) == ACTIVE_STATUS


# --- what a `revoke` decision does end to end (criteria 2, 4 and 6) --------------


def test_a_revoke_decision_ends_the_grant_and_completes_the_review(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """The whole path in one call: the only route from a review flag to a
    change in access.

    Catches a handler that records the decision and never calls the domain —
    the item would read `revoke` while the access stayed live, which is the
    failure this whole plan exists to prevent. `revoked_grant_count` is
    asserted as 1 AND the grant row is re-read `revoked`, so a count computed
    without a revocation cannot pass. The review is the item's last, so
    `review_complete` and the stored `completed_at` are asserted together: a
    status flipped without the timestamp records a completion that happened
    at no time.
    """
    scope = plant_scope(mcp_env)
    review_id, item_id = _open_with_item(mcp_env, scope)

    payload = _structured(
        call_tool(
            bearer(token_for(scope.manager, principals=("manager",))),
            DECIDE,
            _decide_args(item_id, REVOKE),
        )
    )

    assert payload["decision"] == REVOKE
    assert payload["revoked_grant_count"] == 1
    assert payload["review_complete"] is True
    grant = _grant_row(mcp_env.database_url, scope.grant_id)
    assert str(grant.status) == "revoked"
    assert grant.revoked_at is not None
    assert grant.revoke_reason is not None
    review = _review_row(mcp_env.database_url, review_id)
    assert str(review.status) == "completed"
    assert review.completed_at is not None


def test_a_second_decision_on_the_same_item_is_refused_policy_blocked(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Decision C at the boundary, with two DIFFERENT idempotency keys so the
    guard cannot be what refuses the second call.

    Catches an overwrite: a review is a record of what a named person decided
    at a named instant, and a second call landing on the same row destroys
    the only thing the table is for. `POLICY_BLOCKED` and not `VALIDATION`:
    the arguments are perfectly well formed. The audit delta is exactly
    `tool.failed`, so a refusal that still wrote a `review.item_decided`
    fails here, and the stored word is re-read as the FIRST decision.
    """
    scope = plant_scope(mcp_env)
    _review_id, item_id = _open_with_item(mcp_env, scope)
    first = call_tool(
        bearer(token_for(scope.manager, principals=("manager",))),
        DECIDE,
        _decide_args(item_id, KEEP),
    )
    events_between = _audit_event_types(mcp_env.database_url, scope.tenant_id)

    second = call_tool(
        bearer(token_for(scope.manager, principals=("manager",))),
        DECIDE,
        _decide_args(item_id, REVOKE),
    )

    assert first.is_error is False, first.content
    assert error_code(second) == "POLICY_BLOCKED"
    assert str(_item_row(mcp_env.database_url, item_id).decision) == KEEP
    assert str(_grant_row(mcp_env.database_url, scope.grant_id).status) == ACTIVE_STATUS
    assert _audit_event_types(mcp_env.database_url, scope.tenant_id) == [
        *events_between,
        FAILED_EVENT,
    ]


# --- replay (criterion 6) --------------------------------------------------------


def test_a_replayed_key_returns_the_same_response_and_revokes_nothing_twice(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches a retry that re-runs the body.

    The second call under the same key must not reach decision C's refusal:
    by then the item carries a decision, so a re-run answers `POLICY_BLOCKED`
    for a call the caller already saw succeed. Byte identity is asserted, not
    just equality of meaning, so a response rebuilt rather than replayed
    fails; the `grant.revoked` count is unchanged, so a second revocation
    rewriting `revoked_at` fails; and the audit delta is exactly
    `tool.replayed`, so a second `review.item_decided` — a false record of a
    decision nobody made twice — fails too.
    """
    scope = plant_scope(mcp_env)
    _review_id, item_id = _open_with_item(mcp_env, scope)
    arguments = _decide_args(item_id, REVOKE, idempotency_key=_key())
    caller = bearer(token_for(scope.manager, principals=("manager",)))

    first = call_tool(caller, DECIDE, arguments)
    events_between = _audit_event_types(mcp_env.database_url, scope.tenant_id)
    revocations_between = _revoked_event_count(mcp_env.database_url, scope.tenant_id)
    second = call_tool(caller, DECIDE, arguments)

    assert _response_text(second) == _response_text(first)
    assert second.structured_content == first.structured_content
    assert _revoked_event_count(mcp_env.database_url, scope.tenant_id) == revocations_between
    assert _audit_event_types(mcp_env.database_url, scope.tenant_id) == [
        *events_between,
        REPLAYED_EVENT,
    ]


# --- the audit sequence (criterion 5, decision G, a project rule) --------------


def test_the_audit_sequence_is_review_item_decided_then_tool_succeeded(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Criterion 5 as an exact ordered list, which is the only place the order
    of the two events is observable at all.

    Catches the registry declaring `review.item_decided` as this tool's event
    instead of the generic `tool.succeeded`: the pipeline writes its event
    with the TOOL as subject and a fixed payload, so the log would record that
    somebody called a tool and not which grant was ended and why (standing
    rule 19), and the item's own event would be missing entirely.

    The subject is the ITEM and the payload is decision G's exact key set,
    asserted here as well as in the core suite because only the transport can
    show that the event the PIPELINE adds did not overwrite or duplicate the
    one the handler wrote. `review_complete` carries the completion: there is
    no `review.completed` event, so if this key is dropped the audit log has
    no record that the review closed at all.
    """
    scope = plant_scope(mcp_env)
    review_id, item_id = _open_with_item(mcp_env, scope)
    events_before = _audit_event_types(mcp_env.database_url, scope.tenant_id)

    payload = _structured(
        call_tool(
            bearer(token_for(scope.manager, principals=("manager",))),
            DECIDE,
            _decide_args(item_id, REVOKE),
        )
    )

    assert _audit_event_types(mcp_env.database_url, scope.tenant_id) == [
        *events_before,
        # The revocation's own event first, because `review.item_decided`
        # reports `revoked_grant_count` and cannot be written before the
        # revocation it counts; the pipeline's summary last (criterion 5).
        GRANT_REVOKED_EVENT,
        REVIEW_ITEM_DECIDED_EVENT,
        SUCCEEDED_EVENT,
    ]
    record = _decided_events(mcp_env.database_url, scope.tenant_id)[-1]
    assert str(record.subject_type) == "review_item"
    assert str(record.subject_id) == str(item_id)
    event_payload = dict(record.payload)
    assert set(event_payload) == {
        "review_id",
        "item_id",
        "grant_id",
        "user_id",
        "role_id",
        "flag",
        "decision",
        "revoked_grant_count",
        "review_complete",
    }
    assert event_payload["review_id"] == str(review_id)
    assert event_payload["item_id"] == str(item_id)
    assert event_payload["grant_id"] == str(scope.grant_id)
    assert event_payload["user_id"] == str(scope.member_id)
    assert event_payload["role_id"] == str(scope.role_id)
    assert event_payload["flag"] in {flag.value for flag in ReviewFlag}
    assert event_payload["decision"] == REVOKE
    assert event_payload["revoked_grant_count"] == 1
    assert event_payload["review_complete"] is True
    assert payload["review_complete"] is True


# =================================================================================
# An earlier change, sections 1 and 6: the review QUEUE, and the item read widened with
# the grant's context.
#
# Surface under test (written before it exists):
#
# ``mcp_server.tools.reviews``
#     ``ListReviewsInput`` — ``limit: int = LIMIT_DEFAULT`` bounded
#     ``ge=LIMIT_MIN, le=LIMIT_MAX`` (the same three constants the item list
#     reads, never a second copy), and ``status: ReviewStatus | None = None``,
#     the enum itself so the member IS the pre-check (decision E). No user and
#     no tenant argument at all: both are the token's.
#     ``ListReviewsOutput`` — ``reviews``, each row carrying exactly the
#     thirteen keys in ``QUEUE_ROW_KEYS`` below.
#     ``list_reviews`` — a READ (no idempotency key, extras ignored), declaring
#     the generic ``tool.succeeded``, registered under ``rbac.Tool.LIST_REVIEWS``
#     whose the design cell is the one the other three review rows carry.
#     ``ListReviewItemsOutput``'s items grow six context fields:
#     ``target_display_name``, ``role_name``, ``system_name``, ``privilege``,
#     ``granted_at`` and ``expires_at`` (decision B) — eleven keys per item.
#
# Fixture cost: the same session-scoped ``mcp_env`` seam and
# the same per-case namespace (a freshly planted department, system, cast and
# grant). Every review a case has to FIND in an `any`-scope caller's queue is
# planted with an explicit ``due_at``, so it sorts ahead of the whole
# NULL-``due_at`` tail the rest of this file opens through the tool and the
# case cannot depend on how many reviews the suite happened to write first.
# =================================================================================

LIST_REVIEWS = "list_reviews"

# The output model's keys as a literal, never read off the
# model under test: criterion 3's row, exactly thirteen of them. `item_count`
# and `decided_count` are the progress the design asks for; the two id/name pairs
# are what makes a scope legible, and exactly one of them is non-null on any
# row because the table's check constraint says so.
QUEUE_ROW_KEYS = {
    "review_id",
    "period",
    "status",
    "due_at",
    "completed_at",
    "department_id",
    "department_name",
    "system_id",
    "system_name",
    "reviewer_user_id",
    "reviewer_display_name",
    "item_count",
    "decided_count",
}

# One item as a reviewer sees it after decision B: the five 12c keys plus the
# six the grant answers. Written out rather than derived, so a field dropped on
# both sides at once is still a red row here.
ITEM_KEYS = {
    "item_id",
    "grant_id",
    "flag",
    "decision",
    "decided_at",
    "target_display_name",
    "role_name",
    "system_name",
    "privilege",
    "granted_at",
    "expires_at",
}

OPEN_STATUS = "open"
COMPLETED_STATUS = "completed"
COMPLETED_AT = datetime(2026, 5, 5, 9, 0, tzinfo=UTC)
GRANT_HOLDER_NAME = "Fake Queue Grant Holder"

# A `due_at` no other planted review shares, early enough that every row this
# plan plants sorts ahead of the NULL-`due_at` reviews the rest of the file
# opens. The MINUTE is what varies, so the sequence stays total.
_DUE_MINUTES = itertools.count(0)


def _due() -> datetime:
    return datetime(2026, 4, 1, 9, 0, tzinfo=UTC) + timedelta(minutes=next(_DUE_MINUTES))


def _plant_queue_review(
    database_url: str,
    tenant_id: uuid.UUID,
    *,
    reviewer_user_id: uuid.UUID,
    department_id: uuid.UUID | None = None,
    system_id: uuid.UUID | None = None,
    status: str = OPEN_STATUS,
    due_at: datetime | None = None,
    completed_at: datetime | None = None,
) -> uuid.UUID:
    """One committed `access_reviews` row over a chosen scope.

    Written around the tools because the queue's contract is about `due_at`,
    `status` and the counts — three columns no tool sets today.
    """
    review_id = uuid.uuid4()
    _execute(
        database_url,
        "INSERT INTO access_reviews (id, tenant_id, reviewer_user_id, department_id, system_id,"
        " period, status, due_at, completed_at, created_at, updated_at)"
        " VALUES (:id, :tenant, :reviewer, :department, :system, :period, :status, :due_at,"
        " :completed_at, now(), now())",
        {
            "id": review_id,
            "tenant": tenant_id,
            "reviewer": reviewer_user_id,
            "department": department_id,
            "system": system_id,
            "period": _period(),
            "status": status,
            # `datetime` objects, never strings: asyncpg binds by type.
            "due_at": _due() if due_at is None else due_at,
            "completed_at": completed_at,
        },
    )
    return review_id


def _plant_queue_item(
    database_url: str,
    tenant_id: uuid.UUID,
    *,
    review_id: uuid.UUID,
    grant_id: uuid.UUID,
    decision: str | None = None,
) -> uuid.UUID:
    """One committed `review_items` row under a planted review."""
    item_id = uuid.uuid4()
    _execute(
        database_url,
        "INSERT INTO review_items (id, tenant_id, review_id, grant_id, flag, decision,"
        " decided_at, created_at, updated_at)"
        " VALUES (:id, :tenant, :review, :grant, :flag, :decision, :decided_at, now(), now())",
        {
            "id": item_id,
            "tenant": tenant_id,
            "review": review_id,
            "grant": grant_id,
            "flag": ReviewFlag.OK.value,
            "decision": decision,
            "decided_at": None if decision is None else GRANTED_AT,
        },
    )
    return item_id


def _plant_named_user(
    database_url: str, tenant_id: uuid.UUID, department_id: uuid.UUID, display_name: str
) -> uuid.UUID:
    """A planted person whose display name no other row in this file carries."""
    user_id = _plant_user(database_url, tenant_id, department_id)
    _execute(
        database_url,
        "UPDATE users SET display_name = :name WHERE id = :id",
        {"name": display_name, "id": user_id},
    )
    return user_id


def _name_of(database_url: str, table: str, row_id: uuid.UUID) -> str:
    """One planted row's `name`, read around the tools."""
    rows = _rows(
        database_url,
        # `table` is one of two literals written here, never a client string.
        f"SELECT name FROM {table} WHERE id = :id",
        {"id": row_id},
    )
    assert rows, f"premise: the planted {table} row exists"
    return str(rows[0][0])


def _queue(result: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = _structured(result)["reviews"]
    return rows


def _row_for(result: Any, review_id: uuid.UUID) -> dict[str, Any]:
    matches = [row for row in _queue(result) if str(row["review_id"]) == str(review_id)]
    assert len(matches) == 1, f"the planted review must appear exactly once: {matches!r}"
    return matches[0]


def _when(value: Any) -> datetime:
    return datetime.fromisoformat(str(value))


def _queue_scope_for(principal: str, scope: Scope, env: SeededMcp) -> uuid.UUID:
    """One planted review in the scope this principal's own the design cell reaches.

    `system_owner` holds `own_systems` and nothing else, so the only review it
    can see is over a system it owns; `manager` holds `own_department`. The
    three `any` principals are given the department review.
    """
    if principal == "system_owner":
        return _plant_queue_review(
            env.database_url,
            scope.tenant_id,
            reviewer_user_id=scope.owner.user_id,
            system_id=scope.system_id,
        )
    return _plant_queue_review(
        env.database_url,
        scope.tenant_id,
        reviewer_user_id=scope.manager.user_id,
        department_id=scope.department_id,
    )


# --- the queue's own bound ------------------------------


def test_the_queue_reads_the_same_three_limit_constants_as_the_item_list() -> None:
    """Criterion 5: the bound is READ from `LIMIT_MIN`/`LIMIT_DEFAULT`/`LIMIT_MAX`
    rather than copied as three fresh numbers beside them.

    Catches a second set of literals in the new input model: the boundary case
    below would still pass with `le=500` written as a literal while every other
    caller's page size came from the constant, and the two bounds would then
    drift apart silently. The default is read off the MODEL, so a
    `Field(default=100)` beside a `LIMIT_DEFAULT` of 50 fails here.
    """
    assert reviews.LIMIT_DEFAULT == 50
    assert reviews.ListReviewsInput().limit == reviews.LIMIT_DEFAULT
    bounds = reviews.ListReviewsInput.model_fields["limit"].metadata
    assert {getattr(entry, "ge", None) for entry in bounds} - {None} == {reviews.LIMIT_MIN}
    assert {getattr(entry, "le", None) for entry in bounds} - {None} == {reviews.LIMIT_MAX}


# --- the design's allow set and the row it answers (criteria 1, 2, 3 and 4) ---------


@pytest.mark.parametrize("principal", ALLOWED_PRINCIPALS)
def test_each_permitted_principal_reads_the_queue_of_their_own_scope(
    mcp_env: SeededMcp,  # noqa: F811
    principal: str,
) -> None:
    """The allow direction of `list_reviews`' own the design row, one cell at a
    time, and criterion 3's row shape as a literal.

    Catches a registry entry wired to a matrix member this row is not:
    `offboard_user` admits two principals and `search_audit` six, so a member
    borrowed from either fails here or in the denial case below. The token
    carries exactly ONE principal, so nothing but that principal's own cell can
    admit the call, and each caller is handed a review its own cell reaches — a
    `system_owner` seeing a department review would be a widened scope rather
    than a passing test.

    The thirteen keys are asserted as an exact set, so a row short of
    `decided_count` (the progress half of the design) or carrying a spare
    `tenant_id` fails; the two counts are asserted as numbers, so a row that
    answers the review and leaves the aggregate at zero fails too.
    """
    scope = plant_scope(mcp_env)
    caller = _caller_for(principal, scope)
    review_id = _queue_scope_for(principal, scope, mcp_env)
    _plant_queue_item(
        mcp_env.database_url, scope.tenant_id, review_id=review_id, grant_id=scope.grant_id
    )

    result = call_tool(
        bearer(token_for(caller, principals=(principal,))), LIST_REVIEWS, {"limit": 200}
    )

    row = _row_for(result, review_id)
    assert set(row) == QUEUE_ROW_KEYS
    assert row["status"] == OPEN_STATUS
    assert row["completed_at"] is None
    assert row["item_count"] == 1
    assert row["decided_count"] == 0
    assert str(row["reviewer_user_id"]) == str(caller.user_id)
    assert row["reviewer_display_name"] == "Fake Reviewer"
    if principal == "system_owner":
        assert str(row["system_id"]) == str(scope.system_id)
        assert row["system_name"] == _name_of(mcp_env.database_url, "systems", scope.system_id)
        assert row["department_id"] is None
        assert row["department_name"] is None
    else:
        assert str(row["department_id"]) == str(scope.department_id)
        assert row["department_name"] == _name_of(
            mcp_env.database_url, "departments", scope.department_id
        )
        assert row["system_id"] is None
        assert row["system_name"] is None


@pytest.mark.parametrize("principal", DENIED_PRINCIPALS)
def test_the_two_denied_principals_read_no_queue(
    mcp_env: SeededMcp,  # noqa: F811
    principal: str,
) -> None:
    """The queue's denial direction: catches `list_reviews` registered without a
    matrix member of its own (or with one of the wide rows), which would hand an
    `employee` or an `auditor` every review in the tenant — the scopes under
    review, who reviews them, and how far along each one is.

    The same review is proved visible to a `manager` in the case above, so a
    handler that refuses everybody cannot pass instead.
    """
    scope = plant_scope(mcp_env)
    _plant_queue_review(
        mcp_env.database_url,
        scope.tenant_id,
        reviewer_user_id=scope.manager.user_id,
        department_id=scope.department_id,
    )

    result = call_tool(bearer(token_for(scope.manager, principals=(principal,))), LIST_REVIEWS, {})

    assert error_code(result) == "FORBIDDEN"


# --- scope, per principal (criterion 10, a project rule) -----------------------


def test_a_manager_reads_no_review_of_another_department(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """a project rule: the design's `own_department` scope is the ONLY gate that
    can hide the other department's review here.

    The caller carries `manager`, which the matrix admits on this row, and the
    other review is a real `open` row in the caller's OWN tenant, so neither the
    matrix, the tenant guard nor the status filter can remove it — deleting the
    department predicate is the one change that puts it in the answer. The
    caller's own review is asserted present in the same call, so a handler that
    answers an empty queue to every manager passes nothing.
    """
    own = plant_scope(mcp_env)
    other = plant_scope(mcp_env)
    mine = _plant_queue_review(
        mcp_env.database_url,
        own.tenant_id,
        reviewer_user_id=own.manager.user_id,
        department_id=own.department_id,
    )
    theirs = _plant_queue_review(
        mcp_env.database_url,
        other.tenant_id,
        reviewer_user_id=other.manager.user_id,
        department_id=other.department_id,
    )

    result = call_tool(
        bearer(token_for(own.manager, principals=("manager",))), LIST_REVIEWS, {"limit": 200}
    )

    answered = [str(row["review_id"]) for row in _queue(result)]
    assert str(mine) in answered
    assert str(theirs) not in answered


def test_a_system_owner_reads_no_review_of_a_system_they_do_not_own(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """The `own_systems` half of the same gate, which the department case cannot
    reach: a handler that implemented `own_department` and left `own_systems`
    answering True for every system passes the case above and fails here.

    The caller owns a real system in the same tenant and sees its review in the
    same call, so the refusal is about WHICH system and not about the principal.
    """
    own = plant_scope(mcp_env)
    other = plant_scope(mcp_env)
    mine = _plant_queue_review(
        mcp_env.database_url,
        own.tenant_id,
        reviewer_user_id=own.owner.user_id,
        system_id=own.system_id,
    )
    theirs = _plant_queue_review(
        mcp_env.database_url,
        other.tenant_id,
        reviewer_user_id=other.owner.user_id,
        system_id=other.system_id,
    )

    result = call_tool(
        bearer(token_for(own.owner, principals=("system_owner",))), LIST_REVIEWS, {"limit": 200}
    )

    answered = [str(row["review_id"]) for row in _queue(result)]
    assert str(mine) in answered
    assert str(theirs) not in answered


def test_a_scope_that_reaches_nothing_answers_an_empty_queue_and_never_not_found(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Criterion 11. Catches a handler that treats "no rows" as an absence and
    raises `NOT_FOUND`, which is what the other three review tools do for an id
    they cannot resolve.

    The queue takes no id, so nothing can be missing: an empty department is a
    fact about the tenant, and a `/reviews` page that reads it as an error shows
    a manager a failure where the truth is "nothing is waiting on you". The
    caller is a `manager` the matrix admits, over a freshly planted department
    holding no review at all, so neither RBAC nor the scope gate can refuse the
    call and the answer is decided by the empty-result branch alone.
    """
    scope = plant_scope(mcp_env)

    result = call_tool(bearer(token_for(scope.manager, principals=("manager",))), LIST_REVIEWS, {})

    assert result.is_error is False, result.content
    assert _structured(result) == {"reviews": []}


def test_another_tenants_review_is_invisible_to_an_any_scope_caller(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Criterion 10's cross-tenant half, which the matrix sweep cannot reach:
    `list_reviews` takes no id, so it is not on the cross-tenant axis at all.

    The foreign review is a REAL row in tenant B with a real reviewer, planted
    with an early `due_at` so it would sort near the FRONT of an unguarded
    queue rather than off the end of the page. `platform_admin` is the caller
    that matters: the widest scope the design has is still confined to its own
    tenant, and a read that resolves `any` to "no tenant predicate" answers it.
    The caller's own review is asserted present in the same call, so an empty
    answer cannot pass.
    """
    scope = plant_scope(mcp_env)
    mine = _plant_queue_review(
        mcp_env.database_url,
        scope.tenant_id,
        reviewer_user_id=scope.manager.user_id,
        department_id=scope.department_id,
    )
    other = mcp_env.tenant_b
    foreign = _plant_queue_review(
        mcp_env.database_url,
        other.tenant_id,
        reviewer_user_id=other.user_id,
        department_id=other.department_id,
    )

    result = call_tool(
        bearer(token_for(scope.manager, principals=("platform_admin",))),
        LIST_REVIEWS,
        {"limit": 200},
    )

    answered = [str(row["review_id"]) for row in _queue(result)]
    assert str(mine) in answered
    assert str(foreign) not in answered


# --- the input surface (criterion 5 and criterion 6) -----------------------------


QUEUE_LIMIT_CASES: list[tuple[str, int, bool]] = [
    ("one", 1, True),
    ("two hundred", 200, True),
    ("zero", 0, False),
    ("two hundred and one", 201, False),
    ("negative", -1, False),
]


@pytest.mark.parametrize(
    ("limit", "accepted"),
    [case[1:] for case in QUEUE_LIMIT_CASES],
    ids=[case[0] for case in QUEUE_LIMIT_CASES],
)
def test_the_queue_limit_boundary_is_inclusive_at_1_and_200(
    mcp_env: SeededMcp,  # noqa: F811
    limit: int,
    accepted: bool,
) -> None:
    """Criterion 5's bound, both sides of both ends.

    A one-sided assertion cannot tell an inclusive bound from an exclusive one:
    `gt=1` accepts 200 and refuses 1, and `lt=200` accepts 1 and refuses 200, so
    both accepted rows are needed to pin `ge`/`le`. Zero catches a missing lower
    bound, which lets a caller ask for an empty page and read it as "no review
    is waiting on me"; 201 catches a missing upper bound, which is an unbounded
    response over the wire. The caller's department holds exactly two reviews,
    so `limit=1` answering one row proves the bound is applied to the QUERY and
    not only accepted by the model.
    """
    scope = plant_scope(mcp_env)
    for _ in range(2):
        _plant_queue_review(
            mcp_env.database_url,
            scope.tenant_id,
            reviewer_user_id=scope.manager.user_id,
            department_id=scope.department_id,
        )

    result = call_tool(
        bearer(token_for(scope.manager, principals=("manager",))),
        LIST_REVIEWS,
        {"limit": limit},
    )

    if accepted:
        assert result.is_error is False, result.content
        assert len(_queue(result)) == min(limit, 2)
    else:
        assert error_code(result) == "VALIDATION"


STATUS_WORD_CASES: list[tuple[str, str, bool]] = [
    ("open", OPEN_STATUS, True),
    ("completed", COMPLETED_STATUS, True),
    ("uppercase member", "OPEN", False),
    ("trailing newline", OPEN_STATUS + "\n", False),
    ("trailing space", OPEN_STATUS + " ", False),
    ("unknown word", "archived", False),
]


@pytest.mark.parametrize(
    ("status", "accepted"),
    [case[1:] for case in STATUS_WORD_CASES],
    ids=[case[0] for case in STATUS_WORD_CASES],
)
def test_the_status_filter_is_an_exact_enum_member_or_refused_before_any_read(
    mcp_env: SeededMcp,  # noqa: F811
    status: str,
    accepted: bool,
) -> None:
    """Decision E. Catches `status` typed as a bare `str`, which would send a
    client word straight into the WHERE clause: `"OPEN"` then answers an empty
    queue that a reviewer reads as "nothing is waiting on me", and `"archived"`
    names a status the `ReviewStatus` enum has never had.

    The trailing-newline and trailing-space rows are what separate the enum from
    a `Field(pattern=...)`, which matches a SUBSTRING and accepts both (standing
    rule 4); the uppercase row catches a case-insensitive coercion. Both
    accepted rows are here so the table cannot pass vacuously, and because they
    are the only two words `access_reviews.status` may ever hold.
    """
    scope = plant_scope(mcp_env)
    _plant_queue_review(
        mcp_env.database_url,
        scope.tenant_id,
        reviewer_user_id=scope.manager.user_id,
        department_id=scope.department_id,
    )

    result = call_tool(
        bearer(token_for(scope.manager, principals=("manager",))),
        LIST_REVIEWS,
        {"status": status},
    )

    if accepted:
        assert result.is_error is False, result.content
    else:
        assert error_code(result) == "VALIDATION"


def test_each_status_answers_only_its_own_rows_and_an_absent_status_answers_both(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Decision E's filtering half, which the word table above cannot pin: a
    handler that ACCEPTS the member and then ignores it passes every row there.

    Both reviews are in the caller's own department, so the scope gate answers
    identically for the two and only the status predicate can separate them. The
    absent-filter call asserts BOTH ids, so a handler that defaults to `open`
    (the status a reviewer usually wants) fails rather than looking correct.
    """
    scope = plant_scope(mcp_env)
    caller = bearer(token_for(scope.manager, principals=("manager",)))
    open_review_id = _plant_queue_review(
        mcp_env.database_url,
        scope.tenant_id,
        reviewer_user_id=scope.manager.user_id,
        department_id=scope.department_id,
    )
    completed_review_id = _plant_queue_review(
        mcp_env.database_url,
        scope.tenant_id,
        reviewer_user_id=scope.manager.user_id,
        department_id=scope.department_id,
        status=COMPLETED_STATUS,
        completed_at=COMPLETED_AT,
    )

    only_open = call_tool(caller, LIST_REVIEWS, {"status": OPEN_STATUS})
    only_completed = call_tool(caller, LIST_REVIEWS, {"status": COMPLETED_STATUS})
    everything = call_tool(caller, LIST_REVIEWS, {})

    assert [str(row["review_id"]) for row in _queue(only_open)] == [str(open_review_id)]
    assert [str(row["review_id"]) for row in _queue(only_completed)] == [str(completed_review_id)]
    assert {str(row["review_id"]) for row in _queue(everything)} == {
        str(open_review_id),
        str(completed_review_id),
    }
    assert _when(_row_for(only_completed, completed_review_id)["completed_at"]) == COMPLETED_AT


# --- the audit record (criterion 12, a project rule) ---------------------------


def test_the_queue_writes_exactly_one_audit_event_of_type_tool_succeeded(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Criterion 12 as an exact delta. Catches a read that appends an event of
    its own beside the pipeline's — a `review.opened` or a `review.listed` for a
    call that reviewed nothing — and one registered with a declared event type
    other than the generic `tool.succeeded`, which would file a read of the
    queue under a name an auditor reads as a change.

    The delta is asserted over the whole tenant's log rather than filtered by
    type, so a second event of ANY name fails.
    """
    scope = plant_scope(mcp_env)
    _plant_queue_review(
        mcp_env.database_url,
        scope.tenant_id,
        reviewer_user_id=scope.manager.user_id,
        department_id=scope.department_id,
    )
    events_before = _audit_event_types(mcp_env.database_url, scope.tenant_id)

    result = call_tool(bearer(token_for(scope.manager, principals=("manager",))), LIST_REVIEWS, {})

    assert result.is_error is False, result.content
    assert _audit_event_types(mcp_env.database_url, scope.tenant_id) == [
        *events_before,
        SUCCEEDED_EVENT,
    ]


# --- the widened item shape (criterion 7, decision B) ----------------------------


def test_list_review_items_answers_eleven_keys_naming_the_grant_behind_the_item(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Decision B. Catches the item list left at 12c's five keys, and a widening
    that echoes the item's own columns under context names.

    A reviewer cannot decide a `grant_id`: the six new keys name the PERSON, the
    role, the system, the privilege and the two dates, each read from the row
    its id points at. The grant holder is planted with a display name no other
    row in this file carries, so a join to the wrong person (the review's
    reviewer, whose name every planted user shares) answers a different string
    rather than a plausible one. The two dates are compared as instants, so a
    field wired to `created_at` rather than to the grant's own columns fails.
    """
    scope = plant_scope(mcp_env)
    holder = _plant_named_user(
        mcp_env.database_url, scope.tenant_id, scope.department_id, GRANT_HOLDER_NAME
    )
    holder_grant = _plant_grant(mcp_env.database_url, scope.tenant_id, holder, scope.role_id)
    opened = _structured(
        call_tool(
            bearer(token_for(scope.manager, principals=("manager",))),
            OPEN,
            _open_args(department_id=scope.department_id),
        )
    )

    payload = _structured(
        call_tool(
            bearer(token_for(scope.manager, principals=("manager",))),
            LIST,
            {"review_id": str(opened["review_id"])},
        )
    )

    items = {str(item["grant_id"]): item for item in payload["items"]}
    assert set(items) == {str(scope.grant_id), str(holder_grant)}
    item = items[str(holder_grant)]
    assert set(item) == ITEM_KEYS
    assert item["target_display_name"] == GRANT_HOLDER_NAME
    assert item["role_name"] == "Fake Review Role"
    assert item["system_name"] == "Fake Review System"
    assert item["privilege"] == "read"
    assert _when(item["granted_at"]) == GRANTED_AT
    assert _when(item["expires_at"]) == FAR_EXPIRY
    assert item["decision"] is None
