"""GET /me/notifications and the read stamp on one of them.

The two MCP tools an earlier change finished, reached over HTTP instead of over a turn
(23a decision A, 23c decision A and 23h decision A, unchanged). Nothing here
reads the database and nothing here decides anything: the call crosses the MCP
boundary, so RBAC, the kill switch, the self-only scope check and the audit
event apply to a stamp the browser makes exactly as they would to one a turn
made. a project rule holds as written — the mutation is a typed MCP tool behind
RBAC and this module is transport. Neither tool is in the agent's catalogue
(23ga decision E), so this router is the only way either of them is reached at
all.

**Identity comes from the token and from nowhere else**. The
caller's own bearer is forwarded verbatim, no route takes a user or a tenant
argument, and the reader in the derived key is read from the VERIFIED claims
rather than from anything the browser sent. Pinned by
`test_notification_routes.py::test_the_key_is_derived_from_the_tokens_reader_and_never_from_a_query_parameter`,
which sends a `reader_user_id` for another user of the SAME tenant (standing
rule 9) and asserts the arguments as an exact key set, so a field riding along
fails the case too, and by
`::test_the_page_route_sends_no_identity_argument_even_when_the_query_names_one`
for the read — which matters more there, because the read tool ignores extras
and nothing on the far side would catch one.

**The body is the tool's structured output, unchanged**, for the same reason
every other proxied read's is: a route that reshaped on the way out would be a
second contract for 23gc's page and bell to track, and the unread count is the
one number a bell cannot recompute from the page it was handed.

**The page size stays the TOOL's** inclusive 1-200 (§5's second risk). A second
`ge`/`le` here would be a copy of a bound that lives in
`mcp_server/tools/notifications.py` and would refuse 201 with
FastAPI's untyped 422 where §1 asks for the typed 400 the shared
`STATUS_FOR_CODE` dresses `VALIDATION` as. Pinned at both ends by
`::test_the_inclusive_ends_of_the_tools_page_size_are_accepted` and
`::test_a_page_size_outside_the_tools_bounds_is_a_typed_400_with_no_read`.

**`unread_only` is a typed `bool` and not a forwarded string** (decision F),
the shape the `uuid.UUID` path parameter next to it has. A bool is a type, not
a vocabulary the tool owns, which is what left 23h's `status` a string. The
cost is named rather than hidden and pinned rather than claimed:
`unread_only=maybe` answers FastAPI's own 422 and not the design's
`VALIDATION`, which is what
`::test_an_unparseable_unread_only_is_fastapis_own_422_with_no_read` asserts.

The stamp's path is `/notifications/{notification_id}/read` and not
`/mark-read` (decision C): the resource is the notification and the sub-path is
the state being set, the shape `/approvals/{id}/approve` and
`/review-items/{id}/decide` already use. `mark-read` would name the TOOL, which
is an implementation detail of the far side of the MCP boundary.

No lifespan resource is touched: these routes read nothing off `app.state`,
which is why their tests enter no lifespan and open no socket.
"""

import uuid

from fastapi import APIRouter, Header, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from agent_api.deps import authenticated, call_tool_route, unauthenticated
from agent_api.graph.keys import derive_notification_read_key

router = APIRouter()


class ListNotificationsArgs(BaseModel):
    """`list_notifications` with neither the unread filter nor a page size.

    An absent `unread_only` means the tool's own `False` — read and unread
    alike — and an absent limit means its own `LIMIT_DEFAULT`. Repeating either
    here would be a second place for them to drift from,
    which is why the three narrower shapes below are SUBCLASSES rather than
    nullable fields: `model_dump` carries every declared field, so a
    `limit: int | None = None` would send `limit: null` into a bound of 1..200
    and answer `VALIDATION`, and an `unread_only: bool | None = None` would
    send `unread_only: null` into a `bool`. Pinned shape by shape by
    `test_notification_routes.py::test_the_page_route_sends_each_query_parameter_only_when_it_is_asked_for`
    and
    `::test_the_page_route_sends_neither_unread_only_nor_limit_when_neither_is_asked_for`.
    """


class ListNotificationsArgsWithUnreadOnly(ListNotificationsArgs):
    """The page filtered to the unread set, at the tool's own page size.

    The bell's own view: 23ga put this filter on the tool because the unread
    set is what a bell renders, and a proxy that could not ask for it would
    make the filter unreachable from the only caller there is (decision D).
    """

    unread_only: bool


class ListNotificationsArgsWithLimit(ListNotificationsArgs):
    """The page at the caller's own size, read and unread alike."""

    limit: int


class ListNotificationsArgsWithUnreadOnlyAndLimit(ListNotificationsArgs):
    """Both, the shape 23gc's filtered paging sends."""

    unread_only: bool
    limit: int


class MarkNotificationReadArgs(BaseModel):
    """`mark_notification_read`'s the design arguments, assembled by this router.

    `idempotency_key` is DERIVED here and never accepted from the browser
    (decision B): a client-supplied key is an identifier the client is free to
    reuse across two different notifications, which is `IDEMPOTENCY_CONFLICT`
    taken by accident. There is no reader field, by a project rule — the tool
    reads the reader from the same bearer this router forwards. These two keys
    and nothing else are asserted as an exact set by
    `test_notification_routes.py::test_the_stamp_route_answers_the_tools_output_under_the_callers_own_bearer`.
    """

    notification_id: uuid.UUID
    idempotency_key: str


@router.get("/me/notifications")
async def my_notifications(
    authorization: str | None = Header(default=None),
    unread_only: bool | None = Query(default=None),
    limit: int | None = Query(default=None),
) -> JSONResponse:
    """THIS caller's own notifications, newest first, with their unread total.

    The whole filter is "mine", and that "mine" is the token's user: the tool
    declares no user argument and this route sends none. Both query parameters
    are forwarded only when asked for (decision D), so an absent one leaves the
    tool's own default in force rather than sending a null into it.

    The unread count rides back with the page because a bell reading the page
    would go quiet at the page size (23ga decision B); this route re-counts
    nothing.
    """
    arguments: ListNotificationsArgs
    if unread_only is None:
        arguments = (
            ListNotificationsArgs()
            if limit is None
            else ListNotificationsArgsWithLimit(limit=limit)
        )
    elif limit is None:
        arguments = ListNotificationsArgsWithUnreadOnly(unread_only=unread_only)
    else:
        arguments = ListNotificationsArgsWithUnreadOnlyAndLimit(
            unread_only=unread_only, limit=limit
        )
    return await call_tool_route(authorization, "list_notifications", arguments)


@router.post("/notifications/{notification_id}/read")
async def mark_read_route(
    notification_id: uuid.UUID,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    """Stamp one of this caller's own notifications read.

    No body at all (decision B): there is nothing for the browser to say beyond
    which row it opened, and a body would be a payload this route does not
    validate and the tool does not read.

    `notification_id` is a `uuid.UUID` path parameter, so a non-uuid path
    reaches no tool, no key and no query
    (`::test_a_non_uuid_notification_id_reaches_no_tool_and_no_key`). A `str`
    id would digest happily into a perfectly valid key filed against nothing.

    Whose notification it is stays the tool's self-only scope check, not this
    router's: another person's or another tenant's id crosses back as
    `NOT_FOUND` (23ga decision G) and is dressed by the shared map.

    The claims are read here for the reader the key is derived from, which means
    the token is verified twice on this path: once here and once inside
    `call_tool_route`. Two copies of one guard, so neither can be killed on its
    own — the 401 cases pin the ANSWER, not this branch, and
    deleting either copy leaves the other answering exactly the same way.
    """
    verified = authenticated(authorization)
    if verified is None:
        return unauthenticated()
    _token, claims = verified

    arguments = MarkNotificationReadArgs(
        notification_id=notification_id,
        idempotency_key=derive_notification_read_key(
            reader_user_id=uuid.UUID(claims.sub), notification_id=notification_id
        ),
    )
    return await call_tool_route(authorization, "mark_notification_read", arguments)
