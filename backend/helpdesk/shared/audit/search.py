"""Reading one tenant's audit log back under a filter.

The session is bound to one tenant, so this takes no tenant argument and
cannot be asked for another tenant's events. One call reads at most
`SCAN_CAP` rows. The scope filter runs later, in the tool, so a page can
come back short; `lowest_seq` passed back as `before_seq` reaches the rest.
The log is never pruned, so an unbounded read is not an option.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import ColumnElement
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.core.errors import ErrorCode, ToolError
from helpdesk.shared.models import AuditEvent
from helpdesk.shared.crud.audit import fetch_audit_events_page

SCAN_CAP = 500
"""How many rows one search may read, before the scope filter runs."""


class InvalidTimeRangeError(ToolError):
    """`occurred_from` is later than `occurred_to`.

    Refused before any query, not answered with an empty page: a caller who
    swapped two dates would otherwise be told their audit trail is clean.
    """

    code = ErrorCode.VALIDATION


@dataclass(frozen=True)
class AuditSearchPage:
    """One page of the log, newest first, with what a cursor needs."""

    events: tuple[AuditEvent, ...]
    """The matching rows, `seq` DESC."""

    lowest_seq: int | None
    """The lowest `seq` scanned, or None; pass it back as `before_seq`."""

    has_more: bool
    """True when the read stopped at `SCAN_CAP` rather than the end of the log."""


def _filters(
    *,
    event_type: str | None,
    subject_type: str | None,
    subject_id: str | None,
    actor_ref: str | None,
    occurred_from: datetime | None,
    occurred_to: datetime | None,
    before_seq: int | None,
) -> list[ColumnElement[bool]]:
    """One clause per filter given; a missing filter adds no clause at all."""
    clauses: list[ColumnElement[bool]] = []
    if event_type is not None:
        clauses.append(AuditEvent.event_type == event_type)
    if subject_type is not None:
        clauses.append(AuditEvent.subject_type == subject_type)
    if subject_id is not None:
        clauses.append(AuditEvent.subject_id == subject_id)
    if actor_ref is not None:
        clauses.append(AuditEvent.actor_ref == actor_ref)
    if occurred_from is not None:
        clauses.append(AuditEvent.occurred_at >= occurred_from)
    if occurred_to is not None:
        clauses.append(AuditEvent.occurred_at <= occurred_to)
    if before_seq is not None:
        # Strictly before: the cursor names the last row the caller already
        # has, and `<=` would repeat it on every page.
        clauses.append(AuditEvent.seq < before_seq)
    return clauses


async def search_audit_events(
    session: AsyncSession,
    *,
    event_type: str | None = None,
    subject_type: str | None = None,
    subject_id: str | None = None,
    actor_ref: str | None = None,
    occurred_from: datetime | None = None,
    occurred_to: datetime | None = None,
    before_seq: int | None = None,
) -> AuditSearchPage:
    """At most `SCAN_CAP` of the bound tenant's events, newest `seq` first.

    Ordered by `seq`, not `occurred_at`: two events can share an instant.
    """
    if occurred_from is not None and occurred_to is not None and occurred_from > occurred_to:
        raise InvalidTimeRangeError(
            f"occurred_from {occurred_from.isoformat()} is later than "
            f"occurred_to {occurred_to.isoformat()}"
        )

    clauses = _filters(
        event_type=event_type,
        subject_type=subject_type,
        subject_id=subject_id,
        actor_ref=actor_ref,
        occurred_from=occurred_from,
        occurred_to=occurred_to,
        before_seq=before_seq,
    )
    rows = await fetch_audit_events_page(session, clauses, SCAN_CAP)
    return AuditSearchPage(
        events=rows,
        lowest_seq=rows[-1].seq if rows else None,
        has_more=len(rows) == SCAN_CAP,
    )
