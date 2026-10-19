"""`solve_ticket`, an admin marks one open ticket solved.

Surface under test (written before it exists):

``mcp_server.tools.tickets``
    ``SolveTicketInput(MutatingToolInput)`` with ``ticket_id: uuid.UUID`` and
    nothing else (extra fields refused). ``SolveTicketOutput`` with exactly
    ``ticket_id``, ``ticket_number``, ``status``, ``solved_at``,
    ``solved_by_user_id``. ``solve_ticket(ctx, args)`` is MUTATING, declared
    event ``ticket.solved``, ``it_admin`` and ``platform_admin`` only.

``shared.models.Ticket``
    ``solved_at`` and ``solved_by_user_id``, both nullable. The solver is the
    token's user, never an argument. A solved ticket keeps its FIRST stamp.

Refusals: ``NOT_FOUND`` for another company's id and for an id nobody holds;
``FORBIDDEN`` for the five other principals; ``VALIDATION`` for an extra
field, an id that is not a uuid and an over-long key.

Cost: the cases marked `db` ride the session-scoped ``mcp_env`` seam (one
seeded throwaway database). Per-test isolation is a namespace: every case
plants its OWN admin user and its OWN tickets (fresh ids, random numbers), and
sends a fresh key per call, so no case sees another's rows or audit events.
The clock cannot be faked through the transport, so the solved time is checked
against the wall clock around the call.
"""

import asyncio
import re
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from helpdesk.mcp_server.tools import tickets
from helpdesk.mcp_server.tools.registry import REGISTRY
from helpdesk.shared.models.idempotency import KEY_LENGTH
from tests.support.mcp import (  # noqa: F401 - fixtures are resolved by name
    SeededMcp,
    SeededUser,
    bearer,
    call_tool,
    error_code,
    mcp_env,
    mcp_pg,
    mcp_redis_base_url,
    mcp_redis_url,
    plant_ticket,
    seeded_mcp_db,
    token_for,
)

SOLVE = "solve_ticket"
LIST_OPEN = "list_open_tickets"
LIST_MINE = "list_my_tickets"

# The audit contract as literals, never imports.
SOLVED_EVENT = "ticket.solved"
REPLAYED_EVENT = "tool.replayed"

OUTPUT_KEYS = {"ticket_id", "ticket_number", "status", "solved_at", "solved_by_user_id"}
PLANTED_START = date(2019, 3, 7)
PLANTED_PASSWORD_HASH = "not-a-real-hash-this-user-never-logs-in"
PLANTED_AT = datetime(1980, 3, 1, 9, 0, tzinfo=UTC)


def _key() -> str:
    return f"fake-key-{uuid.uuid4().hex}"


def _number() -> int:
    return uuid.uuid4().int % 1_000_000_000 + 1_000_000


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


def _plant_user(env: SeededMcp, *, tenant: SeededUser) -> SeededUser:
    """One fresh, token-ready user in the named seeded tenant (the namespace)."""
    user_id = uuid.uuid4()
    _execute(
        env.database_url,
        "INSERT INTO users (id, tenant_id, department_id, email, display_name, password_hash,"
        " principals, status, start_date, end_date, created_at, updated_at)"
        " VALUES (:id, :tenant, :department, :email, :display_name, :password_hash,"
        " :principals, 'active', :start_date, NULL, now(), now())",
        {
            "id": user_id,
            "tenant": tenant.tenant_id,
            "department": tenant.department_id,
            "email": f"fake.solver.{uuid.uuid4().hex}@fake.invalid",
            "display_name": "Fake Solver",
            "password_hash": PLANTED_PASSWORD_HASH,
            "principals": ["employee"],
            "start_date": PLANTED_START,
        },
    )
    return SeededUser(
        tenant_id=tenant.tenant_id,
        tenant_slug=tenant.tenant_slug,
        user_id=user_id,
        email=f"fake.solver.{user_id}@fake.invalid",
        display_name="Fake Solver",
        department_id=tenant.department_id,
        principals=("employee",),
        status="active",
    )


def _headers(user: SeededUser, principal: str = "it_admin") -> dict[str, str]:
    return bearer(token_for(user, principals=(principal,)))


def _plant_open(env: SeededMcp, *, opener: SeededUser) -> uuid.UUID:
    return plant_ticket(
        env.database_url,
        tenant_id=opener.tenant_id,
        user_id=opener.user_id,
        number=_number(),
        created_at=PLANTED_AT,
    )


def _stored(env: SeededMcp, ticket_id: uuid.UUID) -> Any:
    rows = _rows(
        env.database_url,
        "SELECT status, solved_at, solved_by_user_id FROM tickets WHERE id = :id",
        {"id": ticket_id},
    )
    assert len(rows) == 1
    return rows[0]


def _events_of(env: SeededMcp, actor: SeededUser) -> list[str]:
    rows = _rows(
        env.database_url,
        "SELECT event_type FROM audit_events WHERE tenant_id = :tenant AND actor_ref = :actor"
        " ORDER BY seq",
        {"tenant": actor.tenant_id, "actor": str(actor.user_id)},
    )
    return [str(row[0]) for row in rows]


def _structured(result: Any) -> dict[str, Any]:
    assert result.is_error is False, result.content
    assert result.structured_content is not None
    content: dict[str, Any] = result.structured_content
    return content


def _solve(admin: SeededUser, ticket_id: uuid.UUID, *, key: str | None = None) -> Any:
    return call_tool(
        _headers(admin),
        SOLVE,
        {"ticket_id": str(ticket_id), "idempotency_key": key or _key()},
    )


@pytest.mark.db
def test_an_it_admin_solves_an_open_ticket(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches a solve that answers without writing (the row stays `open`), one
    that stamps no time or the wrong time, one that records an id other than
    the token's user, and an answer that shows the uuid in place of the `TCK-`
    number. The row is re-read from the store and must match the answer."""
    admin = _plant_user(mcp_env, tenant=mcp_env.tenant_a)
    opener = _plant_user(mcp_env, tenant=mcp_env.tenant_a)
    ticket = _plant_open(mcp_env, opener=opener)
    before = datetime.now(UTC) - timedelta(seconds=5)

    payload = _structured(_solve(admin, ticket))

    after = datetime.now(UTC) + timedelta(seconds=5)
    assert set(payload) == OUTPUT_KEYS
    assert payload["ticket_id"] == str(ticket)
    assert payload["status"] == "solved"
    assert re.fullmatch(r"TCK-\d{4,}", payload["ticket_number"])
    assert payload["solved_by_user_id"] == str(admin.user_id)
    stored = _stored(mcp_env, ticket)
    assert stored[0] == "solved"
    assert stored[2] == admin.user_id
    assert before <= stored[1] <= after
    assert datetime.fromisoformat(str(payload["solved_at"])) == stored[1]


@pytest.mark.db
def test_a_solved_ticket_leaves_the_open_list(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches a solve that stamps the solved columns but leaves `status`
    `open` (the board would keep showing the row). The ticket is proven to be
    on the list first, so an absent row is not an empty answer."""
    admin = _plant_user(mcp_env, tenant=mcp_env.tenant_a)
    ticket = _plant_open(mcp_env, opener=admin)

    def listed() -> list[str]:
        result = call_tool(_headers(admin), LIST_OPEN, {"limit": 200})
        return [row["ticket_id"] for row in _structured(result)["tickets"]]

    assert str(ticket) in listed()

    _structured(_solve(admin, ticket))

    assert str(ticket) not in listed()


@pytest.mark.db
def test_the_opener_sees_it_solved_in_my_tickets(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches a solve that changes a column `list_my_tickets` does not read,
    so the employee would still read `open`. The opener is a different person
    from the admin, so the admin's own list cannot satisfy it."""
    admin = _plant_user(mcp_env, tenant=mcp_env.tenant_a)
    opener = _plant_user(mcp_env, tenant=mcp_env.tenant_a)
    ticket = _plant_open(mcp_env, opener=opener)

    _structured(_solve(admin, ticket))

    mine = _structured(call_tool(_headers(opener, "employee"), LIST_MINE, {"limit": 200}))
    status_of = {row["ticket_id"]: row["status"] for row in mine["tickets"]}
    assert status_of[str(ticket)] == "solved"


@pytest.mark.db
def test_the_solve_writes_one_ticket_solved_event(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches a tool registered with the generic `tool.succeeded` event, one
    that writes no event, and one that writes two. The admin is a fresh user,
    so every event under their name is this call's."""
    admin = _plant_user(mcp_env, tenant=mcp_env.tenant_a)
    ticket = _plant_open(mcp_env, opener=admin)

    _structured(_solve(admin, ticket))

    assert _events_of(mcp_env, admin) == [SOLVED_EVENT]


@pytest.mark.db
def test_the_same_key_twice_solves_once(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches a tool registered as a READ or outside the key guard: the second
    call would run again and write a second `ticket.solved` instead of a
    `tool.replayed`, and the bytes of the answer could differ."""
    admin = _plant_user(mcp_env, tenant=mcp_env.tenant_a)
    ticket = _plant_open(mcp_env, opener=admin)
    key = _key()

    first = _solve(admin, ticket, key=key)
    second = _solve(admin, ticket, key=key)

    assert _structured(first) == _structured(second)
    assert first.content[0].text == second.content[0].text
    assert _events_of(mcp_env, admin) == [SOLVED_EVENT, REPLAYED_EVENT]


@pytest.mark.db
def test_a_solved_ticket_keeps_its_first_stamp(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches a writer that re-stamps on every call (the solved time drifts
    forward and the solver becomes the second admin) and one that answers an
    error for an already solved ticket. The second call uses a NEW key and a
    different admin, so the key guard cannot be what keeps the stamp; both
    calls log `ticket.solved`, so the guard is not what answered either."""
    first_admin = _plant_user(mcp_env, tenant=mcp_env.tenant_a)
    second_admin = _plant_user(mcp_env, tenant=mcp_env.tenant_a)
    ticket = _plant_open(mcp_env, opener=first_admin)

    first = _structured(_solve(first_admin, ticket))
    second = _structured(_solve(second_admin, ticket))

    assert second == first
    assert second["solved_by_user_id"] == str(first_admin.user_id)
    stored = _stored(mcp_env, ticket)
    assert stored[2] == first_admin.user_id
    assert datetime.fromisoformat(str(first["solved_at"])) == stored[1]
    assert _events_of(mcp_env, second_admin) == [SOLVED_EVENT]


@pytest.mark.db
def test_another_tenants_ticket_is_not_found(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches a read by id alone, with no company guard: an admin of one
    company could close another's ticket. The id is a REAL open row in
    company B, so only the guard can refuse; the row is re-read and stays
    `open`, so the refusal came before the write."""
    admin = _plant_user(mcp_env, tenant=mcp_env.tenant_a)
    foreign_opener = _plant_user(mcp_env, tenant=mcp_env.tenant_b)
    foreign = _plant_open(mcp_env, opener=foreign_opener)

    result = _solve(admin, foreign)

    assert error_code(result) == "NOT_FOUND"
    stored = _stored(mcp_env, foreign)
    assert stored[0] == "open"
    assert stored[1] is None
    assert stored[2] is None


@pytest.mark.db
def test_an_unknown_ticket_id_is_not_found(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches a solve of a missing row that answers success or crashes with
    an untyped error instead of the typed `NOT_FOUND`."""
    admin = _plant_user(mcp_env, tenant=mcp_env.tenant_a)

    result = _solve(admin, uuid.uuid4())

    assert error_code(result) == "NOT_FOUND"


@pytest.mark.db
@pytest.mark.parametrize("principal", ["employee", "manager", "system_owner", "hr", "auditor"])
def test_an_employee_is_refused_as_forbidden(
    mcp_env: SeededMcp,  # noqa: F811
    principal: str,
) -> None:
    """Catches a tool registered without its role check (a person closes a
    colleague's ticket). The arguments are valid and the ticket is real and
    open, so only the matrix can refuse; the row stays `open`. Run for the five
    refused principals, not just the employee."""
    caller = _plant_user(mcp_env, tenant=mcp_env.tenant_a)
    ticket = _plant_open(mcp_env, opener=caller)

    result = call_tool(
        _headers(caller, principal),
        SOLVE,
        {"ticket_id": str(ticket), "idempotency_key": _key()},
    )

    assert error_code(result) == "FORBIDDEN"
    assert _stored(mcp_env, ticket)[0] == "open"


@pytest.mark.db
def test_a_user_id_argument_is_refused_as_validation(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches an input model that tolerates extra fields: the solver could
    then be named by the caller, not read from the token. The ticket is real
    and open, so a tool that ignored the extra field would SUCCEED; the row is
    re-read and stays `open`."""
    admin = _plant_user(mcp_env, tenant=mcp_env.tenant_a)
    other = _plant_user(mcp_env, tenant=mcp_env.tenant_a)
    ticket = _plant_open(mcp_env, opener=admin)

    result = call_tool(
        _headers(admin),
        SOLVE,
        {"ticket_id": str(ticket), "idempotency_key": _key(), "user_id": str(other.user_id)},
    )

    assert error_code(result) == "VALIDATION"
    assert _stored(mcp_env, ticket)[0] == "open"


def test_the_input_model_refuses_an_extra_user_id() -> None:
    """Catches an input model without `extra="forbid"`, at the model itself
    (no database): the refusal is the model's own."""
    with pytest.raises(ValidationError):
        tickets.SolveTicketInput.model_validate(
            {"ticket_id": str(uuid.uuid4()), "idempotency_key": "k", "user_id": str(uuid.uuid4())}
        )


@pytest.mark.parametrize("bad_id", ["TCK-0042", "", "not-a-uuid"])
def test_a_ticket_id_that_is_not_a_uuid_is_refused_as_validation(bad_id: str) -> None:
    """Catches an id typed as `str` (the readable number would reach the
    lookup and read as 'not found' or match a column of the wrong type)."""
    with pytest.raises(ValidationError):
        tickets.SolveTicketInput.model_validate({"ticket_id": bad_id, "idempotency_key": "k"})


@pytest.mark.db
def test_a_ticket_number_as_the_id_is_refused_as_validation_over_the_wire(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches a registry entry whose wire schema lets `TCK-0042` through
    where the model alone would refuse it: the typed code must be VALIDATION,
    not NOT_FOUND."""
    admin = _plant_user(mcp_env, tenant=mcp_env.tenant_a)

    result = call_tool(_headers(admin), SOLVE, {"ticket_id": "TCK-0042", "idempotency_key": _key()})

    assert error_code(result) == "VALIDATION"


@pytest.mark.db
@pytest.mark.parametrize(
    ("length", "accepted"),
    [
        pytest.param(KEY_LENGTH, True, id="key-at-128-ok"),
        pytest.param(KEY_LENGTH + 1, False, id="key-at-129-refused"),
    ],
)
def test_the_idempotency_key_boundary_is_inclusive_at_key_length(
    mcp_env: SeededMcp,  # noqa: F811
    length: int,
    accepted: bool,
) -> None:
    """The key lands in the `idempotency_keys` column: 128 passes, 129 is
    refused as VALIDATION before any write. Catches a `>=` check (refuses the
    longest legal key) and no check (the over-long key aborts inside
    Postgres after the stamp: the refused arm re-reads the ticket, `open`)."""
    assert KEY_LENGTH == 128
    admin = _plant_user(mcp_env, tenant=mcp_env.tenant_a)
    ticket = _plant_open(mcp_env, opener=admin)
    key = f"fake-key-{uuid.uuid4().hex}".ljust(length, "k")
    assert len(key) == length

    result = _solve(admin, ticket, key=key)

    if accepted:
        assert result.is_error is False, result.content
        assert _stored(mcp_env, ticket)[0] == "solved"
    else:
        assert error_code(result) == "VALIDATION"
        assert _stored(mcp_env, ticket)[0] == "open"


def test_the_tool_is_registered_as_mutating_with_the_solved_event() -> None:
    """Catches a tool registered as a read (no key owed, no replay), one left
    on the generic `tool.succeeded` event, and one wired to the wrong models."""
    spec = REGISTRY[SOLVE]
    assert spec.mutating is True
    assert spec.event_type == SOLVED_EVENT
    assert spec.input_model is tickets.SolveTicketInput
    assert spec.output_model is tickets.SolveTicketOutput
    assert set(tickets.SolveTicketOutput.model_fields) == OUTPUT_KEYS
