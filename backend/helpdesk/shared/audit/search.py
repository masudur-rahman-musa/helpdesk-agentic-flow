"""Reading one tenant's audit log back under a filter (the design, an earlier change).

The audit package owns its own SQL, the way `verify.py` does: no service
reaches for `session.execute` against `audit_events`, so every read of the
log is in one place and one place is where a filter can be got wrong.

The session is TENANT-BOUND (`helpdesk.core.db.tenant_session`), so this
module takes no tenant argument at all and cannot be asked for another
tenant's events; pinned by
`test_audit_search.py::test_another_tenants_events_are_invisible_through_the_bound_session`,
which plants identical rows in two tenants and searches with no filter, so
nothing but the ORM guard can withhold either set.

The bound on one call is `SCAN_CAP` rows READ, not the caller's `limit`.
An earlier change decision A filters by scope in the tool handler, AFTER this read,
so a page can come back short of what the caller asked for; `lowest_seq` is
what makes the rest reachable through `before_seq`. `audit_events` is the
one table the design never prunes, which is why an unbounded read is not an
option here (decision B).
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, select
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.errors import ErrorCode, ToolError
from helpdesk.shared.models import AuditEvent

SCAN_CAP = 500
"""How many rows ONE search may read, before the scope filter runs.

Pinned exactly by
`test_audit_search.py::test_the_scan_cap_is_read_exactly_and_hands_back_a_cursor`,
which plants `SCAN_CAP + 1` rows: the only planting size that separates an
unbounded read, an off-by-one cap and this one.
"""


class InvalidTimeRangeError(ToolError):
    """`occurred_from` is later than `occurred_to`.

    Refused BEFORE any statement runs, rather than answered with the empty
    page such a range would legitimately match: a caller who transposed two
    dates would otherwise be told their audit trail is clean. Pinned by
    `test_audit_search.py::test_an_inverted_time_range_is_refused_validation_before_any_statement_runs`,
    which counts the statements the engine sends.
    """

    code = ErrorCode.VALIDATION


@dataclass(frozen=True)
class AuditSearchPage:
    """One page of the log, newest first, with what a cursor needs."""

    events: tuple[AuditEvent, ...]
    """The matching rows, `seq` DESC."""

    lowest_seq: int | None
    """The lowest `seq` this call scanned, or None when nothing matched.

    Where the next page starts: the caller passes it back as `before_seq`.
    """

    has_more: bool
    """True exactly when the read stopped at `SCAN_CAP` rather than at the
    end of the log, so a caller can tell a full page from a final one."""


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
    """Decision D's filter surface, each one applied only when it is given.

    An absent filter adds no clause at all — not an empty-string comparison —
    which is the half of criterion 3 the `no-filter` row of
    `test_audit_search.py::test_each_decision_d_filter_narrows_and_an_absent_one_does_not`
    pins.
    """
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

    Every filter is optional and every one of them narrows; ordering is by
    `seq` and never by `occurred_at`, because `seq` is the only column that
    totally orders a tenant's chain (two events can share an instant).
    """
    if occurred_from is not None and occurred_to is not None and occurred_from > occurred_to:
        raise InvalidTimeRangeError(
            f"occurred_from {occurred_from.isoformat()} is later than "
            f"occurred_to {occurred_to.isoformat()}"
        )

    statement: Any = (
        select(AuditEvent)
        .where(
            *_filters(
                event_type=event_type,
                subject_type=subject_type,
                subject_id=subject_id,
                actor_ref=actor_ref,
                occurred_from=occurred_from,
                occurred_to=occurred_to,
                before_seq=before_seq,
            )
        )
        .order_by(AuditEvent.seq.desc())
        .limit(SCAN_CAP)
    )
    rows = tuple((await session.execute(statement)).scalars().all())
    return AuditSearchPage(
        events=rows,
        lowest_seq=rows[-1].seq if rows else None,
        has_more=len(rows) == SCAN_CAP,
    )
