"""GET /me/notifications and marking one of them read.

These call MCP tools the agent never sees, so access checks, the kill switch
and audit still apply. The caller's own token is forwarded and the reader
comes from the verified claims. The tool's output is returned unchanged,
including the unread count a bell cannot work out from one page.
"""

import uuid
from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Query, Request

from helpdesk.agent_api.agent.keys import derive_notification_read_key
from helpdesk.agent_api.core.deps import request_claims
from helpdesk.agent_api.schemas.notifications import (
    ListNotificationsArgs,
    ListNotificationsArgsWithLimit,
    ListNotificationsArgsWithUnreadOnly,
    ListNotificationsArgsWithUnreadOnlyAndLimit,
    MarkNotificationReadArgs,
)
from helpdesk.agent_api.services.tool_proxy import proxy_tool

router = APIRouter()


@router.get("/me/notifications")
async def my_notifications(
    request: Request,
    unread_only: bool | None = Query(default=None),
    limit: int | None = Query(default=None),
) -> Mapping[str, Any]:
    """This caller's notifications, newest first, with the unread total.

    Filters are sent only when asked for, so the tool's defaults apply otherwise.
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
    return await proxy_tool(request, "list_notifications", arguments)


@router.post("/notifications/{notification_id}/read")
async def mark_read_route(
    notification_id: uuid.UUID,
    request: Request,
) -> Mapping[str, Any]:
    """Mark one of this caller's notifications read.

    No body: the id is all there is to say. Someone else's id answers not found,
    from the tool.
    """
    claims = request_claims(request)

    arguments = MarkNotificationReadArgs(
        notification_id=notification_id,
        idempotency_key=derive_notification_read_key(
            reader_user_id=uuid.UUID(claims.sub), notification_id=notification_id
        ),
    )
    return await proxy_tool(request, "mark_notification_read", arguments)
