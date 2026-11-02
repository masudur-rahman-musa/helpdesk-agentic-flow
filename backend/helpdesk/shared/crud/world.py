"""The whole state of one tenant that a conversation can change, read in one go.

The eval route reads it before and after a turn, so a grader can check where
the world ended up. Every select names whole models, so the session's tenant
guard scopes each read and each join; another tenant's rows never come back.
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.models import (
    AccessRequest,
    AccessReview,
    Department,
    Grant,
    ReviewItem,
    Role,
    System,
    Ticket,
    User,
)


@dataclass(frozen=True)
class RequestRow:
    number: int
    requester_id: uuid.UUID
    target_id: uuid.UUID
    role_name: str
    status: str


@dataclass(frozen=True)
class GrantRow:
    user_id: uuid.UUID
    role_name: str
    status: str
    expires_at: datetime | None


@dataclass(frozen=True)
class TicketRow:
    number: int
    user_id: uuid.UUID
    kind: str
    status: str


@dataclass(frozen=True)
class PersonRow:
    user_id: uuid.UUID
    email: str
    department: str | None
    status: str
    start_date: date
    reset_asked: bool
    unlocked: bool


@dataclass(frozen=True)
class ReviewRow:
    status: str
    items_decided: int


@dataclass(frozen=True)
class WorldRows:
    requests: list[RequestRow]
    grants: list[GrantRow]
    tickets: list[TicketRow]
    people: list[PersonRow]
    reviews: list[ReviewRow]


async def read_world_rows(session: AsyncSession) -> WorldRows:
    """Requests, grants, tickets, people and reviews of the bound tenant, each in a fixed order."""
    requests = await session.execute(
        select(AccessRequest, Role, System)
        .join(Role, Role.id == AccessRequest.role_id)
        .join(System, System.id == Role.system_id)
        .order_by(AccessRequest.number)
    )
    grants = await session.execute(
        select(Grant, Role, System)
        .join(Role, Role.id == Grant.role_id)
        .join(System, System.id == Role.system_id)
        .order_by(Grant.user_id, System.slug, Role.slug, Grant.granted_at)
    )
    tickets = await session.execute(select(Ticket).order_by(Ticket.number))
    people = await session.execute(
        select(User, Department)
        .outerjoin(Department, Department.id == User.department_id)
        .order_by(User.email)
    )
    decided = (
        select(func.count())
        .select_from(ReviewItem)
        .where(ReviewItem.review_id == AccessReview.id, ReviewItem.decision.is_not(None))
        .scalar_subquery()
    )
    reviews = await session.execute(
        select(AccessReview, decided).order_by(AccessReview.created_at, AccessReview.id)
    )
    return WorldRows(
        requests=[
            RequestRow(
                number=request.number,
                requester_id=request.requester_user_id,
                target_id=request.target_user_id,
                role_name=f"{system.slug}.{role.slug}",
                status=request.status,
            )
            for request, role, system in requests.tuples()
        ],
        grants=[
            GrantRow(
                user_id=grant.user_id,
                role_name=f"{system.slug}.{role.slug}",
                status=grant.status,
                expires_at=grant.expires_at,
            )
            for grant, role, system in grants.tuples()
        ],
        tickets=[
            TicketRow(number=t.number, user_id=t.user_id, kind=t.kind, status=t.status)
            for t in tickets.scalars()
        ],
        people=[
            PersonRow(
                user_id=user.id,
                email=user.email,
                department=department.name if department is not None else None,
                status=user.status,
                start_date=user.start_date,
                reset_asked=user.password_reset_requested_at is not None,
                unlocked=user.account_unlocked_at is not None,
            )
            for user, department in people.tuples()
        ],
        reviews=[
            ReviewRow(status=review.status, items_decided=int(count))
            for review, count in reviews.tuples()
        ],
    )
