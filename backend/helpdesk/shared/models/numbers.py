"""Readable numbers for tickets and access requests, counted per company.

The chat and the web show `TCK-0042` or `REQ-0107`, never the database id.
`reference_counters` holds the last number each company used for each kind.
"""

from enum import StrEnum

from sqlalchemy import Index, String
from sqlalchemy.orm import Mapped, mapped_column

from helpdesk.shared.models.base import TenantScoped


class NumberKind(StrEnum):
    """What a number counts. The words are stored rows: renaming one restarts a count."""

    TICKET = "ticket"
    ACCESS_REQUEST = "access_request"


PREFIXES: dict[NumberKind, str] = {
    NumberKind.TICKET: "TCK",
    NumberKind.ACCESS_REQUEST: "REQ",
}

_PAD = 4


def format_number(kind: NumberKind, number: int) -> str:
    """`TCK-0042`. Past 9999 the number simply grows (`REQ-10000`).

    Numbers start at 1, so 0 or less can only be a bug upstream.
    """
    if number < 1:
        raise ValueError(f"a readable number starts at 1, got {number}")
    return f"{PREFIXES[kind]}-{number:0{_PAD}d}"


class ReferenceCounter(TenantScoped):
    __tablename__ = "reference_counters"
    __table_args__ = (
        Index("uq_reference_counters_tenant_id_kind", "tenant_id", "kind", unique=True),
    )

    kind: Mapped[str] = mapped_column(String(32))
    last: Mapped[int] = mapped_column()
