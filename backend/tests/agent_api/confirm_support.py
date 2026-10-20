"""Fakes shared by the an earlier change test files (confirm every change).

Built on the `ChatDb`, `Tools` and `play` fakes of `test_draft_turns.py`; this
file only adds what an earlier change needs and those lack: a chat row whose caller has
named principals, a directory with a VPN system, a gateway that tolerates the
follow-on requests of a turn whose ending the plan does not fix, and the
payloads of the approval tools. Nothing here opens a socket or a database.
Every test builds its own objects, so nothing is shared between tests.
"""

import json
import uuid
from datetime import date
from typing import Any

import httpx

from helpdesk.agent_api.agent.state import ProposedCall, ToolResult
from helpdesk.shared.models import System, User
from tests.agent_api.test_agent_compose import StagedAnswer, StagedGateway
from tests.agent_api.test_draft_turns import (
    EMPTY_PLAN,
    TENANT_ID,
    USER_ID,
    ChatDb,
    _Rows,
    wiki_db,
)

FOLLOW_ON_REPLY = "Your fake request was looked at."
"""A composed reply that uses no decision word, given to a request the test did
not stage on purpose."""

RESET = "reset_own_password"
UNLOCK = "unlock_own_account"
TICKET = "create_ticket"
LIST_APPROVALS = "list_pending_approvals"
APPROVE = "approve_request"

VPN_ID = uuid.UUID("66666666-6666-5666-8666-666666666667")
DANA_ID = uuid.UUID("99999999-9999-5999-8999-999999999999")
"""Another person than the speaker, obviously fake."""

REQ_6 = uuid.UUID("aaaaaaaa-0000-5000-8000-000000000006")
REQ_13 = uuid.UUID("aaaaaaaa-0000-5000-8000-000000000013")
REQ_19 = uuid.UUID("aaaaaaaa-0000-5000-8000-000000000019")
REQ_UNLISTED = uuid.UUID("aaaaaaaa-0000-5000-8000-000000000099")

LISTED = (
    (REQ_6, "REQ-0006", "engineer"),
    (REQ_13, "REQ-0013", "analyst"),
    (REQ_19, "REQ-0019", "admin"),
)
"""The three requests waiting on the fake manager: id, number, role slug."""


class PrincipalDb(ChatDb):
    """A `ChatDb` whose caller has a directory row with the given principals.

    `load_session` reads principals from that row by the token's user id, so
    this is how a test makes the speaker an employee or a manager.
    """

    def __init__(self, principals: list[str]) -> None:
        super().__init__()
        self.user = User(
            id=USER_ID,
            tenant_id=TENANT_ID,
            department_id=None,
            email="fake.person@example.test",
            display_name="Fake Person",
            password_hash="argon2id$obviously-fake-test-hash",
            principals=principals,
            status="active",
            start_date=date(2026, 1, 5),
            end_date=None,
        )

    async def execute(self, statement: Any) -> _Rows:
        if statement.column_descriptions[0]["entity"] is User:
            return _Rows([self.user])
        return await super().execute(statement)


def wiki_db_as(*principals: str) -> PrincipalDb:
    """A chat in the wiki tenant whose speaker holds these principals."""
    db = PrincipalDb(list(principals))
    base = wiki_db()
    db.systems.extend(base.systems)
    db.roles.extend(base.roles)
    return db


def vpn_db() -> ChatDb:
    """The wiki tenant plus a VPN system, so "remove my VPN access" resolves."""
    db = wiki_db()
    db.systems.append(
        System(
            id=VPN_ID,
            tenant_id=TENANT_ID,
            slug="vpn",
            name="VPN",
            sensitivity="low",
            owner_user_id=None,
        )
    )
    return db


class FollowOnGateway(StagedGateway):
    """The staged answers in order; a request past them gets a harmless answer.

    For a turn whose ending the plan does not fix (it may plan once more, or
    compose), the follow-on is an empty plan for a planning request and a plain
    reply for a composing one. The test then asserts on the tools and the state,
    never on the follow-on text.
    """

    def __init__(self, *texts: str) -> None:
        super().__init__(answers=[StagedAnswer(text=text) for text in texts])
        self._staged = len(texts)

    def handler(self, request: httpx.Request) -> httpx.Response:
        if len(self.requests) < self._staged:
            return super().handler(request)
        self.requests.append(request)
        body = json.loads(request.content)
        name = body.get("response_format", {}).get("json_schema", {}).get("name")
        text = (
            json.dumps({"text": FOLLOW_ON_REPLY, "asks_question": False, "used_passages": [1]})
            if name == "ComposedReply"
            else EMPTY_PLAN
        )
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": text}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )


def listed_approvals(*, only: tuple[uuid.UUID, ...] | None = None) -> dict[str, Any]:
    """What `list_pending_approvals` answers: three requests, as the server shapes them."""
    rows = [
        {
            "request_id": str(request_id),
            "request_number": number,
            "target_display_name": f"Fake Person {number}",
            "system_name": "Data Warehouse",
            "system_slug": "data-warehouse",
            "role_name": f"data-warehouse.{slug}",
            "role_slug": slug,
            "status": "pending",
        }
        for request_id, number, slug in LISTED
        if only is None or request_id in only
    ]
    return {"approvals": rows}


def approval_results() -> tuple[ToolResult, ...]:
    """The list result as a turn would hold it, for the summary function."""
    return (ToolResult(tool=LIST_APPROVALS, ok=True, payload=listed_approvals()),)


def approve_call(request_id: uuid.UUID) -> tuple[str, dict[str, Any]]:
    """One proposed approval, in the shape `plan_json` takes."""
    return (APPROVE, {"request_id": str(request_id)})


def proposed(tool: str, **arguments: Any) -> ProposedCall:
    """One proposal for a direct `validate_tool_calls` run."""
    return ProposedCall(tool=tool, arguments=arguments)
