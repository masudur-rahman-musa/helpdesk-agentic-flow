"""An earlier change, sections 1 and 6: `onboard_user`, through the transport.

Surface under test (written before it exists):

``mcp_server.tools.lifecycle``
    ``OnboardUserInput(MutatingToolInput)`` — ``email`` (``<= 320``, the
    ``users.email`` column's own length, matched with ``re.fullmatch`` against
    ``EMAIL_PATTERN``), ``display_name`` (``1..256``, the column's own length),
    ``department_id``, ``start_date`` (a real date), ``manager_user_id``, plus
    the idempotency key and ``extra="forbid"`` from ``MutatingToolInput``.
    ``OnboardUserOutput`` — ``user_id``, ``applied_role_ids`` and ``skipped``.
    ``onboard_user`` — a registered MUTATING tool under the wire name
    ``onboard_user``, whose registry ``event_type`` is the generic
    ``tool.succeeded`` (decision F): the handler appends ``user.onboarded``
    itself, naming the NEW USER, so the pipeline's event lands last as the
    summary.

Refusals: ``FORBIDDEN`` (+ ``tool.denied``) for the four principals the design
gives no cell on this row, one shared ``NOT_FOUND`` for a `department_id` or a
`manager_user_id` that is missing OR another tenant's, ``POLICY_BLOCKED`` for
an address already taken in this tenant, and ``VALIDATION`` for every input
the pre-checks refuse — all before any row exists.

Audit deltas are asserted as exact ordered event-type lists against a before
snapshot, never as counts: a missing domain event, a duplicated one, or a
denial filed as an outage is invisible to a count.

Fixture cost: everything rides an earlier change's session-scoped
``mcp_fixtures`` seam — one seeded throwaway database, one dedicated Redis
index flushed per test. Per-test isolation inside the shared database is a
namespace: each case creates its own department (a fresh uuid name) with its
own template rows, and sends a fresh joiner address and a fresh idempotency
key per call. Seeded identities are recomputed from
``scripts.seed.generate_tenant`` — deterministic uuid5 ids, never hardcoded
UUIDs.

No pytest-asyncio: each case is a sync function driving one ``asyncio.run``.
"""

import asyncio
import uuid
from datetime import date
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from scripts.seed import CUAN, NORDLICHT, UserRow, generate_tenant
from tests.support.mcp import (  # noqa: F401 - fixtures are resolved by name
    TEST_JWT_SIGNING_PHRASE,
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

ONBOARD = "onboard_user"

# the design's own name for the joiner event, appended by the handler
# (decision F), and the pipeline's own events. Literals rather than imports:
# these strings are the audit contract, and reading them from the module under
# test would let a rename pass unnoticed.
USER_ONBOARDED_EVENT = "user.onboarded"
GRANT_EXECUTED_EVENT = "grant.executed"
SUCCEEDED_EVENT = "tool.succeeded"
DENIED_EVENT = "tool.denied"
REPLAYED_EVENT = "tool.replayed"

# The two bounds the wire model must carry, written out here and compared
# against the module's own constants inside the boundary case, so the numbers
# are pinned in the test rather than borrowed from the code.
EMAIL_MAX_LENGTH = 320
DISPLAY_NAME_MAX_LENGTH = 256

ROWS = generate_tenant(NORDLICHT)
OTHER_TENANT_ROWS = generate_tenant(CUAN)

# the design's `onboard_user` row: three principals may call it, four may not.
ALLOWED_PRINCIPALS = ("hr", "it_admin", "platform_admin")
DENIED_PRINCIPALS = ("employee", "manager", "system_owner", "auditor")

# The callers. Read only — no case writes to these rows — so they cannot
# collide with another module's namespace. The principal is always forced on
# the token, so the case pins the the design cell rather than whatever the seed
# happened to give this user.
CALLER = ROWS.users_by_department["HR"][3]
NEW_MANAGER = ROWS.users_by_department["HR"][4]

# The template every planted department gets: two low-sensitivity read roles,
# so a clean onboard writes exactly two grants and the audit sequence below
# has a known length. Neither is named by any seeded SoD rule, so nothing here
# can be skipped by accident.
TEMPLATE_ROLES = (ROWS.role_ids[("grafana", "viewer")], ROWS.role_ids[("wiki", "viewer")])

# The second seeded tenant: all these ids have to be is real somewhere else.
OTHER_TENANT_DEPARTMENT = OTHER_TENANT_ROWS.department_ids["Operations"]
OTHER_TENANT_MANAGER = OTHER_TENANT_ROWS.users_by_department["Operations"][0]

START_DATE = "2026-09-01"


def _seeded(user: UserRow) -> SeededUser:
    """A token-ready handle for a deterministic seeded user."""
    return SeededUser(
        tenant_id=ROWS.tenant_id,
        tenant_slug=NORDLICHT.slug,
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        department_id=ROWS.department_ids[user.department],
        principals=tuple(sorted(user.principals)),
        status="active",
    )


def _key() -> str:
    """A unique idempotency key: half the per-test namespace."""
    return f"fake-key-{uuid.uuid4().hex}"


def _email() -> str:
    """A fresh joiner address: the other half, obviously fake."""
    return f"fake.joiner.{uuid.uuid4().hex}@fake.invalid"


def _args(
    department_id: uuid.UUID | str,
    *,
    email: str | None = None,
    display_name: str = "Fake Joiner",
    start_date: str = START_DATE,
    manager_user_id: uuid.UUID | str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Minimally valid arguments for one joiner."""
    return {
        "email": _email() if email is None else email,
        "display_name": display_name,
        "department_id": str(department_id),
        "start_date": start_date,
        "manager_user_id": str(NEW_MANAGER.id if manager_user_id is None else manager_user_id),
        "idempotency_key": _key(),
        **extra,
    }


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


# --- raw-SQL probes (reading and writing around the tool, never through it) -----


def _rows(database_url: str, sql: str, params: dict[str, Any]) -> list[Any]:
    async def scenario() -> list[Any]:
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as conn:
                result = await conn.execute(text(sql), params)
                return list(result)
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _execute(database_url: str, sql: str, params: dict[str, Any]) -> None:
    """One committed statement written around the tool."""

    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as conn:
                await conn.execute(text(sql), params)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def _plant_department(
    database_url: str, *, role_ids: tuple[uuid.UUID, ...] = TEMPLATE_ROLES
) -> uuid.UUID:
    """One committed department in the seeded tenant, owning `role_ids`."""
    department_id = uuid.uuid4()
    _execute(
        database_url,
        "INSERT INTO departments (id, tenant_id, name, manager_user_id, created_at, updated_at)"
        " VALUES (:id, :tenant, :name, :manager, now(), now())",
        {
            "id": department_id,
            "tenant": ROWS.tenant_id,
            "name": f"Fake Onboarding Dept {uuid.uuid4().hex[:12]}",
            "manager": NEW_MANAGER.id,
        },
    )
    for role_id in role_ids:
        _execute(
            database_url,
            "INSERT INTO role_templates (id, tenant_id, department_id, role_id,"
            " created_at, updated_at)"
            " VALUES (:id, :tenant, :department, :role, now(), now())",
            {
                "id": uuid.uuid4(),
                "tenant": ROWS.tenant_id,
                "department": department_id,
                "role": role_id,
            },
        )
    return department_id


def _user_rows(database_url: str, email: str) -> list[Any]:
    """Every `users` row with this address, in EVERY tenant."""
    return _rows(
        database_url,
        "SELECT id, tenant_id, department_id, email, display_name, principals, status,"
        " start_date, end_date, password_hash FROM users WHERE email = :email",
        {"email": email},
    )


def _grant_role_ids(database_url: str, user_id: uuid.UUID) -> list[uuid.UUID]:
    rows = _rows(
        database_url,
        "SELECT role_id FROM grants WHERE user_id = :user AND status = 'active'"
        " AND revoked_at IS NULL",
        {"user": user_id},
    )
    return [uuid.UUID(str(row[0])) for row in rows]


def _audit_event_types(database_url: str, tenant_id: uuid.UUID) -> list[str]:
    rows = _rows(
        database_url,
        "SELECT event_type FROM audit_events WHERE tenant_id = :tenant ORDER BY seq",
        {"tenant": tenant_id},
    )
    return [str(row[0]) for row in rows]


def _onboarded_events(database_url: str, tenant_id: uuid.UUID) -> list[Any]:
    """Every (subject_type, subject_id, payload) written as `user.onboarded`."""
    return _rows(
        database_url,
        "SELECT subject_type, subject_id, payload FROM audit_events WHERE tenant_id = :tenant"
        " AND event_type = :event_type ORDER BY seq",
        {"tenant": tenant_id, "event_type": USER_ONBOARDED_EVENT},
    )


# --- the happy path and the design's allow set (criteria 1, 2 and 7) ---------------


@pytest.mark.parametrize("principal", ALLOWED_PRINCIPALS)
def test_the_three_permitted_principals_each_onboard_a_joiner(
    mcp_env: SeededMcp,  # noqa: F811
    principal: str,
) -> None:
    """The allow direction every refusal below depends on, one row of the design's
    `onboard_user` cell at a time.

    Catches a registry entry wired to the wrong matrix member (say
    `REVOKE_ACCESS`, whose row admits `manager` and `system_owner` instead):
    the three principals here would then be refused for a tool the spec gives
    them, and the denial case below — which sends the OTHER four — would still
    pass, because that wrong row refuses `employee` and `auditor` too. Only
    running both directions over the same registered tool pins the cell.

    The token carries exactly one principal, so nothing but that principal's
    own cell can admit the call. The created row is asserted to be in the
    TOKEN's tenant with no tenant argument anywhere in the input,
    carrying the fixed joiner principals rather than the caller's.
    """
    caller = _seeded(CALLER)
    department_id = _plant_department(mcp_env.database_url)
    email = _email()

    payload = _structured(
        call_tool(
            bearer(token_for(caller, principals=(principal,))),
            ONBOARD,
            _args(department_id, email=email, display_name="Fake Joiner Allowed"),
        )
    )

    rows = _user_rows(mcp_env.database_url, email)
    assert len(rows) == 1, "exactly one users row, and only one"
    row = rows[0]
    assert uuid.UUID(str(row.id)) == uuid.UUID(str(payload["user_id"]))
    assert uuid.UUID(str(row.tenant_id)) == caller.tenant_id
    assert uuid.UUID(str(row.department_id)) == department_id
    assert row.display_name == "Fake Joiner Allowed"
    assert list(row.principals) == ["employee"], "never the caller's principals (decision D)"
    assert row.status == "active"
    assert row.end_date is None
    assert str(row.start_date) == START_DATE
    assert {uuid.UUID(str(role_id)) for role_id in payload["applied_role_ids"]} == set(
        TEMPLATE_ROLES
    )
    assert payload["skipped"] == []
    assert set(_grant_role_ids(mcp_env.database_url, uuid.UUID(str(row.id)))) == set(TEMPLATE_ROLES)


@pytest.mark.parametrize("principal", DENIED_PRINCIPALS)
def test_the_other_four_principals_are_denied_and_write_no_user(
    mcp_env: SeededMcp,  # noqa: F811
    principal: str,
) -> None:
    """a project rule: the design's matrix row is the ONLY gate on this tool.

    Every argument is valid and every id resolves in the caller's own tenant,
    so neither the input pre-checks, the duplicate-email check nor the two
    `NOT_FOUND` lookups can produce this refusal — deleting the pipeline's RBAC
    stage is the one change that flips these four cases from `FORBIDDEN` to a
    created user. (If the handler ends up re-running `rbac.authorize` for
    itself, the two copies of one guard cannot be killed separately; the
    docstring on that guard then has to say so, a project rule.)

    `tool.denied` rather than `tool.failed`: a permission decision filed as an
    outage is a false record. The address is re-read across every tenant, so a
    refusal that still created the row somewhere fails here.
    """
    caller = _seeded(CALLER)
    department_id = _plant_department(mcp_env.database_url)
    email = _email()
    events_before = _audit_event_types(mcp_env.database_url, caller.tenant_id)

    result = call_tool(
        bearer(token_for(caller, principals=(principal,))),
        ONBOARD,
        _args(department_id, email=email),
    )

    assert error_code(result) == "FORBIDDEN"
    assert _user_rows(mcp_env.database_url, email) == []
    events_after = _audit_event_types(mcp_env.database_url, caller.tenant_id)
    assert events_after == [*events_before, DENIED_EVENT]


def test_the_audit_sequence_is_onboarded_then_the_grants_then_tool_succeeded(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Criterion 3 and decision F, as an exact ordered list.

    Catches the registry declaring `user.onboarded` as this tool's event
    instead of the generic `tool.succeeded`: the pipeline writes its event with
    the TOOL as subject and a fixed payload, so the log would record that
    somebody called a tool and not who was hired, and the
    joiner's own event would be missing entirely. Catches the ordering too — an
    `user.onboarded` appended after the grants reads as access given to a
    person who did not exist yet — and a summary event that is not last.

    The subject and the payload keys are asserted here as well as in the core
    test, because only the transport can show that the event the PIPELINE adds
    did not overwrite or duplicate the one the handler wrote.
    """
    caller = _seeded(CALLER)
    department_id = _plant_department(mcp_env.database_url)
    email = _email()
    events_before = _audit_event_types(mcp_env.database_url, caller.tenant_id)

    payload = _structured(
        call_tool(
            bearer(token_for(caller, principals=("hr",))),
            ONBOARD,
            _args(department_id, email=email),
        )
    )

    events_after = _audit_event_types(mcp_env.database_url, caller.tenant_id)
    assert events_after == [
        *events_before,
        USER_ONBOARDED_EVENT,
        GRANT_EXECUTED_EVENT,
        GRANT_EXECUTED_EVENT,
        SUCCEEDED_EVENT,
    ]

    record = _onboarded_events(mcp_env.database_url, caller.tenant_id)[-1]
    assert str(record.subject_type) == "user"
    assert str(record.subject_id) == str(payload["user_id"])
    assert set(dict(record.payload)) == {
        "user_id",
        "email",
        "department_id",
        "manager_user_id",
        "applied_role_ids",
        "skipped_role_ids",
    }


# --- the two absences, twice over (criterion 6) ---------------------------------


def test_a_missing_and_another_tenants_department_answer_the_same_not_found(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches a department lookup that escapes the tenant guard, and one that
    distinguishes the two absences.

    If another tenant's department answered anything other than the unknown
    id's answer, the argument would be a cross-tenant existence oracle: an HR
    officer could enumerate which department ids exist in a tenant they cannot
    see. The other tenant's department is re-read afterwards, and both
    addresses are checked in every tenant, so a cross-tenant onboarding that
    answered `NOT_FOUND` and still created the user fails here.
    """
    caller = _seeded(CALLER)
    missing_email = _email()
    cross_tenant_email = _email()

    missing = call_tool(
        bearer(token_for(caller, principals=("hr",))),
        ONBOARD,
        _args(uuid.uuid4(), email=missing_email),
    )
    cross_tenant = call_tool(
        bearer(token_for(caller, principals=("hr",))),
        ONBOARD,
        _args(OTHER_TENANT_DEPARTMENT, email=cross_tenant_email),
    )

    assert error_code(missing) == "NOT_FOUND"
    assert error_code(cross_tenant) == "NOT_FOUND"
    assert _user_rows(mcp_env.database_url, missing_email) == []
    assert _user_rows(mcp_env.database_url, cross_tenant_email) == []


def test_a_missing_and_another_tenants_manager_answer_the_same_not_found(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Decision E's other half, and the same oracle argument for the manager.

    `users` has no manager column, so `manager_user_id` is only ever carried
    into the `user.onboarded` payload — which makes it exactly the argument an
    implementation is most likely to accept unvalidated, recording a manager
    who does not exist or who belongs to another tenant. The department is
    valid in both calls, so only the manager lookup can refuse, and its answer
    must be the SAME `NOT_FOUND` for both absences.
    """
    caller = _seeded(CALLER)
    department_id = _plant_department(mcp_env.database_url)
    missing_email = _email()
    cross_tenant_email = _email()

    missing = call_tool(
        bearer(token_for(caller, principals=("hr",))),
        ONBOARD,
        _args(department_id, email=missing_email, manager_user_id=uuid.uuid4()),
    )
    cross_tenant = call_tool(
        bearer(token_for(caller, principals=("hr",))),
        ONBOARD,
        _args(department_id, email=cross_tenant_email, manager_user_id=OTHER_TENANT_MANAGER.id),
    )

    assert error_code(missing) == "NOT_FOUND"
    assert error_code(cross_tenant) == "NOT_FOUND"
    assert _user_rows(mcp_env.database_url, missing_email) == []
    assert _user_rows(mcp_env.database_url, cross_tenant_email) == []


def test_an_address_already_in_this_tenant_is_refused_policy_blocked(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Decision G at the boundary: catches the unique index being left to
    raise, which the pipeline maps to `INTERNAL`.

    A caller-caused condition reported as a server fault is both the wrong
    the design code and an unusable answer — "try again" is exactly what must
    not happen. The address belongs to a seeded user in this tenant, so it
    genuinely exists; the row count for it is asserted unchanged, so a second
    row or a mangled variant cannot hide behind the code assertion.
    """
    caller = _seeded(CALLER)
    department_id = _plant_department(mcp_env.database_url)
    taken = _seeded(ROWS.users_by_department["Sales"][5]).email
    assert len(_user_rows(mcp_env.database_url, taken)) == 1, "premise: the address is in use"

    result = call_tool(
        bearer(token_for(caller, principals=("hr",))), ONBOARD, _args(department_id, email=taken)
    )

    assert error_code(result) == "POLICY_BLOCKED"
    assert len(_user_rows(mcp_env.database_url, taken)) == 1


# --- the input surface (criterion 8, a project rule, 2 and 4) -----------------


def _address_of_length(length: int) -> str:
    """A syntactically valid, unique address of exactly `length` characters."""
    domain = "@fake.invalid"
    local = f"fake.joiner.{uuid.uuid4().hex}".ljust(length - len(domain), "a")[
        : length - len(domain)
    ]
    address = f"{local}{domain}"
    assert len(address) == length
    return address


@pytest.mark.parametrize(
    ("email", "display_name", "start_date", "accepted", "expected_code"),
    [
        pytest.param(None, "Fake Joiner", START_DATE, True, "", id="baseline-ok"),
        pytest.param(EMAIL_MAX_LENGTH, "Fake Joiner", START_DATE, True, "", id="email-320-ok"),
        pytest.param(
            EMAIL_MAX_LENGTH + 1, "Fake Joiner", START_DATE, False, "VALIDATION", id="email-321"
        ),
        pytest.param("a@b.c\n", "Fake Joiner", START_DATE, False, "VALIDATION", id="email-newline"),
        pytest.param("a@b.c\r\n", "Fake Joiner", START_DATE, False, "VALIDATION", id="email-crlf"),
        pytest.param("a@b.c ", "Fake Joiner", START_DATE, False, "VALIDATION", id="email-space"),
        pytest.param("a b@fake.invalid", "F", START_DATE, False, "VALIDATION", id="email-inner"),
        pytest.param("", "Fake Joiner", START_DATE, False, "VALIDATION", id="email-empty"),
        pytest.param(
            None, "d" * DISPLAY_NAME_MAX_LENGTH, START_DATE, True, "", id="display-256-ok"
        ),
        pytest.param(
            None,
            "d" * (DISPLAY_NAME_MAX_LENGTH + 1),
            START_DATE,
            False,
            "VALIDATION",
            id="display-257",
        ),
        pytest.param(None, "", START_DATE, False, "VALIDATION", id="display-empty"),
        pytest.param(None, "Fake Joiner", "2026-13-01", False, "VALIDATION", id="start-date-13"),
        pytest.param(None, "Fake Joiner", "not-a-date", False, "VALIDATION", id="start-date-text"),
    ],
)
def test_the_input_bounds_are_inclusive_and_reject_before_any_effect(
    mcp_env: SeededMcp,  # noqa: F811
    email: str | int | None,
    display_name: str,
    start_date: str,
    accepted: bool,
    expected_code: str,
) -> None:
    """Both ends of both bounds, both directions, against the exact numbers,
    plus the three trailing-whitespace rejections a project rule requires.

    The columns are `String(320)` and `String(256)`, so an unbounded input is a
    post-effect database error rather than a typed refusal;
    321 characters must be `VALIDATION` and 320 must go through, which is what
    tells an inclusive bound from an exclusive one. `"a@b.c\\n"`, `"a@b.c\\r\\n"`
    and `"a@b.c "` catch `re.match` or `re.search` used where `re.fullmatch`
    belongs — each is a prefix that matches, so all three are accepted by a
    non-anchored pattern and the address that lands in the directory carries a
    newline. The empty display name catches a `max_length` written without its
    `min_length`, and the two bad dates catch `start_date` typed as `str`.

    Every refused row is re-read across every tenant: the check has to run
    BEFORE any effect, not report a failure after writing the user.
    """
    from helpdesk.mcp_server.tools.lifecycle import (
        DISPLAY_NAME_MAX_LENGTH as WIRED_DISPLAY_NAME_MAX,
    )
    from helpdesk.mcp_server.tools.lifecycle import (
        EMAIL_MAX_LENGTH as WIRED_EMAIL_MAX,
    )

    assert WIRED_EMAIL_MAX == EMAIL_MAX_LENGTH == 320
    assert WIRED_DISPLAY_NAME_MAX == DISPLAY_NAME_MAX_LENGTH == 256

    caller = _seeded(CALLER)
    department_id = _plant_department(mcp_env.database_url)
    if email is None:
        address = _email()
    elif isinstance(email, int):
        address = _address_of_length(email)
    else:
        address = email

    result = call_tool(
        bearer(token_for(caller, principals=("hr",))),
        ONBOARD,
        _args(department_id, email=address, display_name=display_name, start_date=start_date),
    )

    if accepted:
        payload = _structured(result)
        rows = _user_rows(mcp_env.database_url, address)
        assert len(rows) == 1
        assert str(rows[0].id) == str(payload["user_id"])
        assert rows[0].display_name == display_name
        assert rows[0].email == address
    else:
        assert error_code(result) == expected_code
        assert _user_rows(mcp_env.database_url, address) == []


def test_an_unknown_field_is_refused_with_no_user(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """`MutatingToolInput` forbids extras, and this is the argument that proves
    why: a `tenant_id` in the arguments is a caller who believes they are
    hiring into another tenant.

    Dropping it silently would create the user in the TOKEN's tenant while the
    caller reads success as having placed them elsewhere — the one mistake
    a project rule exists to make impossible to make quietly. Catches an input
    model that copied `tools/read.py`'s `extra="ignore"` contract: nothing else
    in this call can refuse it, since the caller is permitted and every other
    argument is valid.

    (Plan §1's input line says extras are "dropped, never refused", which is
    the READ tools' contract; the plan's own §3 puts this input on
    `MutatingToolInput`, whose contract is the opposite. This case pins the
    base class's behaviour.)
    """
    caller = _seeded(CALLER)
    department_id = _plant_department(mcp_env.database_url)
    email = _email()

    result = call_tool(
        bearer(token_for(caller, principals=("hr",))),
        ONBOARD,
        _args(department_id, email=email, tenant_id=str(OTHER_TENANT_ROWS.tenant_id)),
    )

    assert error_code(result) == "VALIDATION"
    assert _user_rows(mcp_env.database_url, email) == []


# --- replay (criterion 9) -------------------------------------------------------


def test_a_replay_returns_the_stored_response_and_creates_no_second_user(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches a retry that re-runs the body.

    The second call under the same key must not reach the duplicate-email
    check: by then the address exists, so a re-run answers `POLICY_BLOCKED` for
    a call the caller already saw succeed. Byte identity is asserted, not just
    equality of meaning, so a response rebuilt rather than replayed fails; the
    audit delta is exactly `tool.replayed`, so a second `user.onboarded` — a
    false record of a hire that never happened — fails as well, and the users
    table is counted so no second row can exist under a different address.
    """
    caller = _seeded(CALLER)
    department_id = _plant_department(mcp_env.database_url)
    email = _email()
    arguments = _args(department_id, email=email)

    first = call_tool(bearer(token_for(caller, principals=("hr",))), ONBOARD, arguments)
    events_between = _audit_event_types(mcp_env.database_url, caller.tenant_id)
    second = call_tool(bearer(token_for(caller, principals=("hr",))), ONBOARD, arguments)

    assert _structured(first)["user_id"]
    assert _response_text(second) == _response_text(first)
    assert second.structured_content == first.structured_content
    assert len(_user_rows(mcp_env.database_url, email)) == 1
    events_after = _audit_event_types(mcp_env.database_url, caller.tenant_id)
    assert events_after == [*events_between, REPLAYED_EVENT]


# --- the sentinel, end to end (decision C) --------------------------------------


def _post_login(tenant_slug: str, email: str, password: str) -> httpx.Response:
    """One `POST /auth/login` against the agent-api app, on this test database.

    `mcp_env` has already pointed `DATABASE_URL` and `JWT_SECRET` at the seeded
    temp database, and the route calls `load_settings()` per request, so no
    second fixture is needed (a project rule: nothing is rebuilt here).
    """

    async def scenario() -> httpx.Response:
        from helpdesk.agent_api.api.router import app

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                "/auth/login",
                json={"tenant_slug": tenant_slug, "email": email, "password": password},
            )

    return asyncio.run(scenario())


def test_login_refuses_an_onboarded_user_exactly_like_an_unknown_one(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Decision C's second pin: catches a joiner created with a USABLE
    credential.

    `verify_password` refusing the sentinel is a unit fact; this is the one
    that matters — an account created by a lifecycle tool, before its owner has
    any credential, must not be loggable into. Three passwords are tried,
    including the sentinel string itself (the value an implementation that
    "hashed the placeholder" would accept), and each answer is compared byte
    for byte with the refusal an entirely unknown address gets, so a joiner
    account cannot be distinguished from a non-existent one either.

    Assert-driven premise: the onboard must have succeeded first, otherwise a
    404-shaped refusal would pass this case for the wrong reason.
    """
    assert TEST_JWT_SIGNING_PHRASE, "premise: mcp_env has set a JWT secret for the login route"
    caller = _seeded(CALLER)
    department_id = _plant_department(mcp_env.database_url)
    email = _email()

    # The control answer first, so the comparison below is against a refusal
    # this suite has actually seen rather than a remembered status code.
    unknown = _post_login(NORDLICHT.slug, _email(), "Whatever-Password-1!")
    assert unknown.status_code == 401, unknown.text

    created = call_tool(
        bearer(token_for(caller, principals=("hr",))), ONBOARD, _args(department_id, email=email)
    )
    assert created.is_error is False, created.content
    assert len(_user_rows(mcp_env.database_url, email)) == 1

    for password in ("", "Whatever-Password-1!", "unusable-password-no-login"):
        response = _post_login(NORDLICHT.slug, email, password)
        assert response.status_code == unknown.status_code, password
        assert response.json() == unknown.json(), password


def test_the_start_date_reaches_the_row_as_given(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches `start_date` being ignored and defaulted to "today".

    Every other case here sends the same date and asserts around it, so a
    handler writing `date.today()` would pass them all on any day the suite
    happens to run near that date — and a joiner's tenure is what the policy
    engine's NEW_JOINER bump reads, so a wrong start date silently changes what
    access they get. A date far from the suite's wall clock is sent, and the
    stored value is compared to it exactly.
    """
    caller = _seeded(CALLER)
    department_id = _plant_department(mcp_env.database_url)
    email = _email()
    far_off = "2019-03-07"
    assert far_off != date.today().isoformat()

    call_tool(
        bearer(token_for(caller, principals=("hr",))),
        ONBOARD,
        _args(department_id, email=email, start_date=far_off),
    )

    rows = _user_rows(mcp_env.database_url, email)
    assert len(rows) == 1
    assert str(rows[0].start_date) == far_off
