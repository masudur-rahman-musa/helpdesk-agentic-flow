"""`list_open_tickets`, every open ticket of the company, oldest first.

Surface under test (written before it exists):

``mcp_server.tools.tickets`` (new)
    ``ListOpenTicketsInput(limit)`` with ``limit`` 1 to 200, default 50.
    ``OpenTicketRow`` with exactly ``ticket_id``, ``ticket_number``, ``kind``,
    ``title``, ``opened_by``, ``created_at``. ``ListOpenTicketsOutput(tickets)``.
    ``list_open_tickets(ctx, args)`` answers every OPEN ticket of the token's
    company, oldest first (ties by number, lower first), cut at ``limit``.
    ``opened_by`` is the opener's display name.

``mcp_server.tools.registry``
    ``list_open_tickets`` registered as a read.

``shared.auth.rbac``
    ``Tool.LIST_OPEN_TICKETS``, ``it_admin`` and ``platform_admin`` only. The
    matrix itself is pinned in ``test_rbac.py`` and ``test_mcp_rbac_matrix.py``;
    this file pins one refused caller end to end.

Cost: the cases marked `db` ride the session-scoped ``mcp_env`` seam (one
seeded throwaway database). The tool stamps ``now()`` and the tests need a
known order, so rows are planted with ``plant_ticket`` (``tests.support.mcp``)
at fixed times far in the past; no case builds an engine. Each case plants in
its own year, and reads back only the ids it planted (the shared database also
holds other cases' tickets), except the limit case, whose year is the oldest of
all so its rows are the oldest in the company.
"""

import re
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from helpdesk.mcp_server.tools import tickets
from helpdesk.mcp_server.tools.registry import REGISTRY
from helpdesk.mcp_server.tools.self_service import TicketKind
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

LIST_OPEN_TICKETS = "list_open_tickets"

# The exact bounds the plan names; pinned as values, not as a band.
LIMIT_MIN = 1
LIMIT_DEFAULT = 50
LIMIT_MAX = 200

SIX_FIELDS = {"ticket_id", "ticket_number", "kind", "title", "opened_by", "created_at"}

ADMIN_PRINCIPALS = ("it_admin",)

# One year per case, so planted rows of one case never sit among another's.
ORDER_YEAR = 1990
TIE_YEAR = 1985
LIMIT_YEAR = 1970  # the oldest of all: the limit case's rows are the oldest in the company


def _at(year: int, day: int = 1) -> datetime:
    return datetime(year, 3, day, 9, 0, tzinfo=UTC)


def _number() -> int:
    """A ticket number no other row in the shared database is likely to hold."""
    return uuid.uuid4().int % 1_000_000_000 + 1_000_000


def _admin_of(env: SeededMcp) -> SeededUser:
    """The seeded user of company A; the token gives them the `it_admin` principal."""
    return env.tenant_a


def _listed(env: SeededMcp, arguments: dict[str, Any]) -> list[dict[str, Any]]:
    token = token_for(_admin_of(env), principals=ADMIN_PRINCIPALS)
    result = call_tool(bearer(token), LIST_OPEN_TICKETS, arguments)
    assert result.is_error is False, result.content
    assert result.structured_content is not None, "a successful call carries structured content"
    rows: list[dict[str, Any]] = result.structured_content["tickets"]
    return rows


@pytest.mark.db
def test_the_list_holds_every_open_ticket_of_the_tenant_oldest_first(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches a query that drops the open filter (the solved ticket shows),
    one that drops the company guard (the other company's open ticket shows),
    one that keeps only the caller's own tickets, one that sorts newest first
    or by insert order, and one that shows the id instead of a `TCK-` number.
    The NEWER ticket is written FIRST, so a list in insert order and a list
    oldest first give different answers."""
    colleague = mcp_env.probe
    admin = _admin_of(mcp_env)
    assert colleague.user_id != admin.user_id and colleague.tenant_id == admin.tenant_id
    newer_number, older_number = _number(), _number()
    newer = plant_ticket(
        mcp_env.database_url,
        tenant_id=admin.tenant_id,
        user_id=colleague.user_id,
        number=newer_number,
        created_at=_at(ORDER_YEAR, 20),
    )
    older = plant_ticket(
        mcp_env.database_url,
        tenant_id=admin.tenant_id,
        user_id=admin.user_id,
        number=older_number,
        created_at=_at(ORDER_YEAR, 10),
    )
    solved = plant_ticket(
        mcp_env.database_url,
        tenant_id=admin.tenant_id,
        user_id=colleague.user_id,
        number=_number(),
        created_at=_at(ORDER_YEAR, 5),
        status="solved",
    )
    other_company = plant_ticket(
        mcp_env.database_url,
        tenant_id=mcp_env.tenant_b.tenant_id,
        user_id=mcp_env.tenant_b.user_id,
        number=_number(),
        created_at=_at(ORDER_YEAR, 6),
    )

    rows = _listed(mcp_env, {"limit": LIMIT_MAX})

    ids = [row["ticket_id"] for row in rows]
    planted = {str(newer), str(older), str(solved), str(other_company)}
    assert [i for i in ids if i in planted] == [str(older), str(newer)]
    by_id = {row["ticket_id"]: row for row in rows}
    assert by_id[str(older)]["ticket_number"] == f"TCK-{older_number:04d}"
    assert by_id[str(newer)]["ticket_number"] == f"TCK-{newer_number:04d}"
    assert all(re.fullmatch(r"TCK-\d{4,}", row["ticket_number"]) for row in rows)


@pytest.mark.db
def test_equal_times_break_by_number_lower_first(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches a sort on time alone (two tickets at one instant come back in
    whatever order the database likes, so a page boundary can repeat or skip a
    row) and a tie broken the wrong way round. The HIGHER number is written
    first and both share one `created_at`, so only the number decides."""
    admin = _admin_of(mcp_env)
    base = _number()
    higher = plant_ticket(
        mcp_env.database_url,
        tenant_id=admin.tenant_id,
        user_id=admin.user_id,
        number=base + 5,
        created_at=_at(TIE_YEAR),
    )
    lower = plant_ticket(
        mcp_env.database_url,
        tenant_id=admin.tenant_id,
        user_id=admin.user_id,
        number=base + 2,
        created_at=_at(TIE_YEAR),
    )

    rows = _listed(mcp_env, {"limit": LIMIT_MAX})

    ids = [row["ticket_id"] for row in rows]
    assert [i for i in ids if i in {str(higher), str(lower)}] == [str(lower), str(higher)]


@pytest.mark.db
def test_a_row_names_who_opened_it(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches a join on the wrong user (the admin's own name on every row, or
    the id or email in place of the name) and a row with no name. The opener is
    a colleague, not the caller, so the caller's name cannot satisfy it."""
    colleague = mcp_env.probe
    admin = _admin_of(mcp_env)
    assert colleague.display_name != admin.display_name
    ticket = plant_ticket(
        mcp_env.database_url,
        tenant_id=admin.tenant_id,
        user_id=colleague.user_id,
        number=_number(),
        created_at=_at(ORDER_YEAR, 2),
    )

    rows = {row["ticket_id"]: row for row in _listed(mcp_env, {"limit": LIMIT_MAX})}

    assert rows[str(ticket)]["opened_by"] == colleague.display_name


@pytest.mark.db
def test_the_limit_keeps_the_oldest_rows(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches a limit that is validated but never reaches the query (three
    come back), and a limit applied before the sort or with the sort reversed
    (the newest two come back). The three are written newest first and sit in
    the oldest year of the shared database, so exactly the two oldest planted
    answer."""
    admin = _admin_of(mcp_env)
    ids = {
        day: str(
            plant_ticket(
                mcp_env.database_url,
                tenant_id=admin.tenant_id,
                user_id=admin.user_id,
                number=_number(),
                created_at=_at(LIMIT_YEAR, day),
            )
        )
        for day in (28, 14, 7)
    }

    rows = _listed(mcp_env, {"limit": 2})

    assert [row["ticket_id"] for row in rows] == [ids[7], ids[14]]


def test_a_row_carries_exactly_the_six_fields() -> None:
    """Catches a row that leaks the `body` or a `status` (every row is open),
    or grows any field beyond the six, and an output that renames its `tickets`
    list. Compared as a set so a missing field and an extra one both fail."""
    assert set(tickets.OpenTicketRow.model_fields) == SIX_FIELDS
    assert set(tickets.ListOpenTicketsOutput.model_fields) == {"tickets"}
    row = tickets.OpenTicketRow(
        ticket_id=uuid.uuid4(),
        ticket_number="TCK-0042",
        kind=TicketKind.GENERAL,
        title="fake title",
        opened_by="Fake Person One",
        created_at=_at(2026),
    )
    assert set(row.model_dump()) == SIX_FIELDS
    assert "body" not in row.model_dump()
    assert "status" not in row.model_dump()


@pytest.mark.parametrize("limit", [LIMIT_MIN, LIMIT_MAX])
def test_the_limit_bounds_are_inclusive(limit: int) -> None:
    """Catches an exclusive bound (`gt=1` refuses 1, `lt=200` refuses 200).
    Both ends are rows because a one-sided check cannot tell the two apart."""
    assert tickets.ListOpenTicketsInput(limit=limit).limit == limit


def test_the_default_limit_is_50() -> None:
    """Catches a drifted default (every caller who omits `limit` silently gets
    another page size), pinned as the exact value."""
    assert tickets.ListOpenTicketsInput().limit == LIMIT_DEFAULT == 50


@pytest.mark.parametrize("limit", [LIMIT_MIN - 1, LIMIT_MAX + 1])
def test_the_input_model_refuses_a_limit_of_0_or_201(limit: int) -> None:
    """Catches a bound that is not on the input model at all (a handler that
    clamps or checks after the query would let these construct)."""
    with pytest.raises(ValidationError):
        tickets.ListOpenTicketsInput(limit=limit)


@pytest.mark.db
@pytest.mark.parametrize("limit", [LIMIT_MIN - 1, LIMIT_MAX + 1])
def test_a_limit_of_0_or_201_is_refused_as_validation(
    mcp_env: SeededMcp,  # noqa: F811
    limit: int,
) -> None:
    """Catches a tool that answers a page for an out-of-range limit by clamping
    it, and one that fails with a crash instead of the typed `VALIDATION` code.
    The caller is an `it_admin`, so the matrix cannot be what refuses; only the
    limit can."""
    token = token_for(_admin_of(mcp_env), principals=ADMIN_PRINCIPALS)

    result = call_tool(bearer(token), LIST_OPEN_TICKETS, {"limit": limit})

    assert error_code(result) == "VALIDATION"


@pytest.mark.db
def test_an_employee_is_refused_as_forbidden(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches a tool registered without its role check (an employee reads the
    whole company's open tickets). An open ticket is planted, so a missing
    guard would answer rows, not an empty list; the arguments are valid, so
    only the matrix can refuse."""
    admin = _admin_of(mcp_env)
    plant_ticket(
        mcp_env.database_url,
        tenant_id=admin.tenant_id,
        user_id=admin.user_id,
        number=_number(),
        created_at=_at(ORDER_YEAR, 3),
    )
    token = token_for(admin, principals=("employee",))

    result = call_tool(bearer(token), LIST_OPEN_TICKETS, {})

    assert error_code(result) == "FORBIDDEN"
    assert result.structured_content is None


def test_the_tool_is_registered_as_a_read() -> None:
    """Catches a tool written but never registered (it would not exist on the
    wire), and one registered as mutating (callers would owe an idempotency
    key to read a list)."""
    spec = REGISTRY[LIST_OPEN_TICKETS]
    assert spec.mutating is False
    assert spec.input_model is tickets.ListOpenTicketsInput
    assert spec.output_model is tickets.ListOpenTicketsOutput
