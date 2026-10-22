"""A person takes back their own access request while it still waits.

Locked with the same `lock_request` approve and reject take, so a cancel and
a last approval on the same row cannot both win: whichever locks second sees
a row that is no longer `pending`.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.access.approvals import lock_request
from helpdesk.shared.access.requests import set_request_status
from helpdesk.shared.core.errors import ErrorCode, NotFoundError, ToolError
from helpdesk.shared.models import AccessRequest, RequestStatus


class NotYourRequestError(ToolError):
    """The caller neither filed the request nor is it for them.

    Checked before the status, so a stranger never learns where it stands.
    """

    code = ErrorCode.FORBIDDEN


class CancelNotPendingError(ToolError):
    """The request was already decided, even if only a moment ago.

    Its own code, not `POLICY_BLOCKED`, so the chat can say plainly it was
    decided and look up how.
    """

    code = ErrorCode.REQUEST_NOT_PENDING


async def cancel_own_request(
    session: AsyncSession, request_id: uuid.UUID, *, caller_user_id: uuid.UUID
) -> AccessRequest:
    """Lock, check, then mark the request `cancelled`. Seats stay as they were.

    `NOT_FOUND` for another company's or a missing row, `FORBIDDEN` for one
    the caller has no part in, `REQUEST_NOT_PENDING` once decided.
    """
    request = await lock_request(session, request_id)
    if request is None:
        raise NotFoundError("no such access request in this tenant")
    if caller_user_id not in (request.requester_user_id, request.target_user_id):
        raise NotYourRequestError("only the requester or the target may cancel a request")
    if request.status != RequestStatus.PENDING.value:
        raise CancelNotPendingError(f"the request is {request.status}, not pending")
    await set_request_status(session, request.id, status=RequestStatus.CANCELLED)
    return request
