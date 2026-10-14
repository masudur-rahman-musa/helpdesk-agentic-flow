"""GET /admin/audit/search: the audit log, filtered, over HTTP.

`search_audit` is an earlier change's tool and nothing in the browser could reach it.
This module is the wire and nothing else: the call crosses the MCP boundary,
so RBAC, the kill switch, the per-row scope filter and the audit event apply
to an auditor's search made from a page exactly as they do to one made in a
turn (23a decision A, unchanged). Everything here is a READ, so a project rule
has nothing to bite on.

**Identity comes from the token and from nowhere else**. The
caller's own bearer is forwarded verbatim by `deps.call_tool_route`, and no
route here takes a user or a tenant argument. Pinned by
`test_audit_routes.py::test_the_search_route_sends_the_eight_filters_and_no_identity_argument`,
which asserts the arguments as an exact key set, so a field riding along fails
the case too.

**A new module rather than a fourteenth route in `admin_routes`** (decision
B). That module predates `deps` and carries its own `_bearer`, `_claims` and
`_is_permitted`; folding the search in there would mean either a second guard
shape inside one module or re-guarding an earlier change's verify route in this diff.
The duplication stays named rather than half-paid (23a Deviations).

**The eight filters are the TOOL's to judge, not this router's** (section 5's
first risk). Every value crosses as the browser sent it and
`SearchAuditInput` — whose four string caps are read off the `audit_events`
columns themselves — is the only validator. A second opinion here would refuse
an over-long filter, an out-of-range page size or an unparseable date with
FastAPI's untyped 422, where SPEC section 8.1's `VALIDATION` dressed as a 400
by the shared `STATUS_FOR_CODE` is what an earlier change has a sentence for. Pinned at
both ends of every bound by
`test_audit_routes.py::test_a_string_filter_of_exactly_the_column_cap_is_accepted`,
`::test_a_string_filter_one_character_over_the_cap_is_a_typed_400_with_no_read`,
`::test_the_inclusive_ends_of_the_tools_page_size_are_accepted`,
`::test_a_page_size_outside_the_tools_bounds_is_a_typed_400_with_no_read`,
`::test_a_cursor_of_one_is_accepted`, `::test_a_cursor_of_zero_is_a_typed_400_with_no_read`
and `::test_a_non_iso_occurred_from_is_a_typed_400_and_never_fastapis_422`.

That is also why the two timestamps are `str` here and not `datetime`: a
`datetime` query parameter answers the untyped 422 for a value the tool would
have refused typed, and a parsed one reaches the tool reformatted rather than
as the string the browser sent.

**The body is the tool's structured output, unchanged**, for the same reason
the `/me` reads are: a route that reshaped on the way out would be a second
contract for 24b's page to track. `next_before_seq` in particular travels
untouched — 13b's scope filter runs after the read, so a narrow caller's page
can be short while matching events exist further back, and that cursor is the
only way to reach them.

No lifespan resource is touched: this route reads nothing off `app.state`,
which is why its tests enter no lifespan and open no socket.
"""

from typing import Any

from fastapi import APIRouter, Header, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from helpdesk.agent_api.deps import call_tool_route

router = APIRouter()

SEARCH_AUDIT_TOOL = "search_audit"


class SearchAuditArgs(BaseModel):
    """`search_audit`'s arguments, carrying ONLY the filters that were asked for.

    Declared with no fields and `extra="allow"` rather than with eight
    nullable ones, because `model_dump` carries every declared field: a
    `limit: int | None = None` would put `limit: null` on the wire against a
    bound of `1`-`200`, and an `event_type: str | None = None` would send
    `event_type: null` where the tool's absent-filter meaning is "every event
    type" (23c's reason). An absent filter therefore sends no key at all, and
    the tool's own `SEARCH_LIMIT_DEFAULT` applies when `limit` is absent.

    Deviation from section 6, recorded in 24a's Deviations: 23c, 23h and this
    plan's section 6 describe a SUBCLASS per shape, which works at one and two
    optional arguments and does not at eight — the eight filters have 256
    combinations. This is the same guarantee written once. Pinned by
    `test_audit_routes.py::test_the_search_route_sends_no_argument_at_all_when_no_filter_is_asked_for`
    and `::test_each_filter_present_sends_exactly_its_own_key_and_no_other`,
    which assert the arguments dict for the empty shape and for each of the
    eight single-filter ones.
    """

    model_config = ConfigDict(extra="allow")


@router.get("/admin/audit/search")
async def search_audit_events(
    authorization: str | None = Header(default=None),
    event_type: str | None = Query(default=None),
    subject_type: str | None = Query(default=None),
    subject_id: str | None = Query(default=None),
    actor_ref: str | None = Query(default=None),
    occurred_from: str | None = Query(default=None),
    occurred_to: str | None = Query(default=None),
    before_seq: int | None = Query(default=None),
    limit: int | None = Query(default=None),
) -> JSONResponse:
    """This tenant's audit events, newest first, filtered by whatever was asked.

    All eight filters are forwarded, and each only when present (decision D):
    13b put them on the tool because an audit log is unusable without them,
    and this route is the search box's only caller. Which events this caller
    may see is the tool's scope filter, never this router's.
    """
    asked: dict[str, Any] = {
        "event_type": event_type,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "actor_ref": actor_ref,
        "occurred_from": occurred_from,
        "occurred_to": occurred_to,
        "before_seq": before_seq,
        "limit": limit,
    }
    arguments = SearchAuditArgs.model_validate(
        {name: value for name, value in asked.items() if value is not None}
    )
    return await call_tool_route(authorization, SEARCH_AUDIT_TOOL, arguments)
