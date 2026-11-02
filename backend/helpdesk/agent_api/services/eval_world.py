"""The case tenant's world as the eval route answers it, built from the world reads.

People are named the way the conversation cases name them. A seeded person is
`<department lowercased>.<index>`: the department and place the seed gave
them, read back from their start date, so a transfer does not rename them. A
person added since (a new joiner) is their email. Roles are `<system>.<role>`.
"""

import uuid
from datetime import date, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.agent_api.schemas.eval import (
    EvalWorld,
    WorldGrant,
    WorldPerson,
    WorldRequest,
    WorldReview,
    WorldTicket,
)
from helpdesk.shared.crud.world import PersonRow, WorldRows, read_world_rows
from helpdesk.shared.services.seed import TENANTS, TenantSpec, seed_user_id

# The seed gives user number `n` of a tenant the start date `HIRING_EPOCH +
# (n * 97) % 2900` days, and numbers a department's users in a row. 97 and 2900
# share no factor, so the date gives the number back.
HIRING_EPOCH_ORDINAL = datetime(2018, 1, 1).toordinal()
START_DATE_STEP = 97
START_DATE_SPAN = 2900
_STEP_INVERSE = pow(START_DATE_STEP, -1, START_DATE_SPAN)

_SPECS = {spec.slug: spec for spec in TENANTS}


def seed_number(start_date: date) -> int:
    """A seeded user's place in the tenant's seed order, read back from the start date."""
    days = start_date.toordinal() - HIRING_EPOCH_ORDINAL
    return (days * _STEP_INVERSE) % START_DATE_SPAN


def _seat_name(spec: TenantSpec, number: int) -> str | None:
    """`<department lowercased>.<index>` for seed number `number`, or `None` past the end."""
    first = 0
    for department, count in zip(spec.departments, spec.users_per_department, strict=True):
        if number < first + count:
            return f"{department.lower()}.{number - first}"
        first += count
    return None


def person_names(tenant_slug: str, people: list[PersonRow]) -> dict[uuid.UUID, str]:
    """Each person's name in the world: their seed seat, else their email."""
    spec = _SPECS.get(tenant_slug)
    names: dict[uuid.UUID, str] = {}
    for person in people:
        name = None
        if spec is not None and person.user_id == seed_user_id(spec.key, person.email):
            name = _seat_name(spec, seed_number(person.start_date))
        names[person.user_id] = name or person.email
    return names


def build_world(tenant_slug: str, rows: WorldRows) -> EvalWorld:
    names = person_names(tenant_slug, rows.people)

    def name(user_id: uuid.UUID) -> str:
        return names.get(user_id, str(user_id))

    return EvalWorld(
        requests=[
            WorldRequest(
                number=r.number,
                requester=name(r.requester_id),
                target=name(r.target_id),
                role=r.role_name,
                status=r.status,
            )
            for r in rows.requests
        ],
        grants=[
            WorldGrant(
                person=name(g.user_id), role=g.role_name, status=g.status, expires_at=g.expires_at
            )
            for g in rows.grants
        ],
        tickets=[
            WorldTicket(number=t.number, person=name(t.user_id), kind=t.kind, status=t.status)
            for t in rows.tickets
        ],
        people=[
            WorldPerson(
                person=name(p.user_id),
                department=p.department,
                status=p.status,
                reset_asked=p.reset_asked,
                unlocked=p.unlocked,
            )
            for p in rows.people
        ],
        reviews=[WorldReview(status=r.status, items_decided=r.items_decided) for r in rows.reviews],
    )


async def read_world(db: AsyncSession, tenant_slug: str) -> EvalWorld:
    """The world of the tenant `db` is bound to."""
    return build_world(tenant_slug, await read_world_rows(db))
