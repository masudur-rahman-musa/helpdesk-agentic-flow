"""Readable numbers: the format, the per-company counter, the unique number.

Surface under test (written before it exists):

``helpdesk.shared.models.numbers``
    ``NumberKind`` (StrEnum: ``TICKET = "ticket"``, ``ACCESS_REQUEST = "access_request"``),
    ``PREFIXES``, ``format_number(kind, number) -> str`` (ValueError below 1),
    ``ReferenceCounter`` (tenant-scoped: ``kind``, ``last``).
``helpdesk.shared.crud.numbers``
    ``next_number(session, kind) -> int``: the tenant comes from the session.
``Repository.create`` fills ``number`` itself for a model that has the column.

Cost: the two format cases are plain functions. The rest carry ``db`` because
an atomic upsert and a unique index are things only Postgres can show. They
ride the shared engine and one fresh tenant each, so they share nothing.
"""

import asyncio
import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from helpdesk.shared.core.db import tenant_session
from helpdesk.shared.crud import Repository
from helpdesk.shared.crud.numbers import next_number
from helpdesk.shared.models import AccessRequest, Ticket
from helpdesk.shared.models.numbers import NumberKind, format_number
from tests.support.seed import SeededTenant, seed_tenant


@pytest.mark.parametrize(
    ("kind", "number", "shown"),
    [
        (NumberKind.TICKET, 1, "TCK-0001"),
        (NumberKind.TICKET, 42, "TCK-0042"),
        (NumberKind.ACCESS_REQUEST, 107, "REQ-0107"),
        (NumberKind.ACCESS_REQUEST, 9999, "REQ-9999"),
        (NumberKind.ACCESS_REQUEST, 10000, "REQ-10000"),
    ],
)
def test_format_number_pads_to_four_digits(kind: NumberKind, number: int, shown: str) -> None:
    """Catches an implementation that pads to the wrong width (3 or 5 digits),
    swaps the two prefixes, or cuts the number at four digits so 10000 reads
    REQ-0000."""
    assert format_number(kind, number) == shown


def test_the_kinds_are_stored_as_these_exact_words() -> None:
    """Catches a rename of the stored `kind` words: they are rows in the
    counter table, so changing one silently restarts that company's count."""
    assert NumberKind.TICKET.value == "ticket"
    assert NumberKind.ACCESS_REQUEST.value == "access_request"
    assert {member.value for member in NumberKind} == {"ticket", "access_request"}


@pytest.mark.parametrize("number", [0, -1])
@pytest.mark.parametrize("kind", list(NumberKind))
def test_format_number_refuses_zero_and_below(kind: NumberKind, number: int) -> None:
    """Catches an implementation that formats 0 or a negative number as
    TCK-0000 or TCK--001 instead of refusing: the first real number is 1, and
    a 0 can only come from a bug upstream."""
    with pytest.raises(ValueError):
        format_number(kind, number)


# --- the counter (Postgres) ----------------------------------------------------


@pytest.mark.db
def test_next_number_counts_per_company(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Catches a counter that is one global sequence: company B's first
    request would then be REQ-0003 instead of REQ-0001. A counter keyed on
    kind alone, without the tenant, fails the same way."""

    async def scenario() -> None:
        company_a = await seed_tenant(session_factory, "num-a")
        company_b = await seed_tenant(session_factory, "num-b")
        async with tenant_session(session_factory, company_a.tenant_id) as session:
            first = await next_number(session, NumberKind.ACCESS_REQUEST)
            second = await next_number(session, NumberKind.ACCESS_REQUEST)
        async with tenant_session(session_factory, company_b.tenant_id) as session:
            other = await next_number(session, NumberKind.ACCESS_REQUEST)
        assert (first, second, other) == (1, 2, 1)

    asyncio.run(scenario())


@pytest.mark.db
def test_next_number_keeps_ticket_and_request_counts_apart(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Catches one counter shared by both kinds: a ticket and then a request
    in the same company would share one count, so the first request would not
    be REQ-0001. The seed already filed one ticket, so tickets count on from it."""

    async def scenario() -> None:
        company = await seed_tenant(session_factory, "num-kinds")
        async with tenant_session(session_factory, company.tenant_id) as session:
            ticket = await next_number(session, NumberKind.TICKET)
            request = await next_number(session, NumberKind.ACCESS_REQUEST)
            second_ticket = await next_number(session, NumberKind.TICKET)
        assert request == 1
        assert second_ticket == ticket + 1

    asyncio.run(scenario())


@pytest.mark.db
def test_two_requests_filed_at_once_get_different_numbers(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Catches a counter that reads the last number and then writes the next
    in two steps (or max+1 with no lock): two sessions filing together in a
    company with no counter row yet would both get 1. Only the single
    INSERT ... ON CONFLICT DO UPDATE statement ends with 1 and 2."""

    async def file_one(tenant_id: uuid.UUID) -> int:
        async with tenant_session(session_factory, tenant_id) as session:
            return await next_number(session, NumberKind.ACCESS_REQUEST)

    async def scenario() -> None:
        company = await seed_tenant(session_factory, "num-race")
        numbers = await asyncio.gather(file_one(company.tenant_id), file_one(company.tenant_id))
        assert sorted(numbers) == [1, 2]

    asyncio.run(scenario())


def _ticket_fields(seeded: SeededTenant) -> dict[str, object]:
    return {
        "user_id": seeded.user_id,
        "kind": "general",
        "title": "fake numbered ticket",
        "body": "fake body",
        "status": "open",
        "session_id": None,
    }


def _request_fields(seeded: SeededTenant) -> dict[str, object]:
    return {
        "requester_user_id": seeded.user_id,
        "target_user_id": seeded.user_id,
        "role_id": seeded.role_id,
        "justification": "fake numbered request",
        "requested_expiry_days": None,
        "risk_class": "low",
        "decision": "auto",
        "decision_reasons": [],
        "citations": [],
        "status": "pending",
        "approver_user_ids": [],
        "approvals": [],
        "session_id": None,
        "trace_id": None,
        "idempotency_key": None,
    }


@pytest.mark.db
def test_a_duplicate_number_in_one_company_is_refused(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Catches a missing unique index on (tenant_id, number): without it a
    second request with number 1 in the same company goes in and two requests
    read REQ-0001."""

    async def scenario() -> None:
        company = await seed_tenant(session_factory, "num-dup")
        # Raised out of the session, so its rollback runs and no commit is tried.
        with pytest.raises(IntegrityError):
            async with tenant_session(session_factory, company.tenant_id) as session:
                repo = Repository(session, AccessRequest)
                await repo.create(number=1, **_request_fields(company))
                await repo.create(number=1, **_request_fields(company))

    asyncio.run(scenario())


@pytest.mark.db
def test_the_same_number_in_two_companies_is_allowed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Catches a unique index on `number` alone: two companies must both be
    able to have REQ-0001."""

    async def scenario() -> None:
        company_a = await seed_tenant(session_factory, "num-same-a")
        company_b = await seed_tenant(session_factory, "num-same-b")
        for company in (company_a, company_b):
            async with tenant_session(session_factory, company.tenant_id) as session:
                row = await Repository(session, AccessRequest).create(
                    number=1, **_request_fields(company)
                )
                assert row.number == 1

    asyncio.run(scenario())


@pytest.mark.db
def test_create_fills_the_number_when_the_caller_gives_none(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Catches a `Repository.create` that leaves `number` empty (a NOT NULL
    error) or draws a number even when the caller passed one (burning counter
    values): two requests get 1 and 2, then an explicit 50 is kept as given,
    and the next draw is 3."""

    async def scenario() -> None:
        company = await seed_tenant(session_factory, "num-fill")
        async with tenant_session(session_factory, company.tenant_id) as session:
            requests = Repository(session, AccessRequest)
            first = await requests.create(**_request_fields(company))
            second = await requests.create(**_request_fields(company))
            explicit = await requests.create(number=50, **_request_fields(company))
            third = await requests.create(**_request_fields(company))
            assert [first.number, second.number, explicit.number, third.number] == [1, 2, 50, 3]

    asyncio.run(scenario())


@pytest.mark.db
def test_a_new_ticket_gets_a_ticket_number_not_a_request_number(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Catches a `Repository.create` that always draws the request counter:
    after one request, a ticket made through the repository must still be
    number 1 on its own counter (the seeded ticket already drew number 1, so
    the new one is 2)."""

    async def scenario() -> None:
        company = await seed_tenant(session_factory, "num-tck")
        async with tenant_session(session_factory, company.tenant_id) as session:
            await Repository(session, AccessRequest).create(**_request_fields(company))
            ticket = await Repository(session, Ticket).create(**_ticket_fields(company))
            assert ticket.number == 2

    asyncio.run(scenario())
