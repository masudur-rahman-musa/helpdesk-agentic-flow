"""Giving up your own access from the chat.

Surface under test (written before it exists):

``helpdesk.agent_api.agent.loop_tools.offered_tools``
    offers ``revoke_access`` as a CHANGE to all seven principals (today only
    the roles the matrix gave a revoke cell).
``helpdesk.agent_api.agent.confirmation.change_summary``
    the held-revoke line names the access from this turn's ``list_my_access``
    rows by ``grant_id``: "I'll remove your access to CRM viewer." then
    ``CHANGE_LINE``. A grant that is not in the listing keeps the line without
    a name: "I'll remove this access."

Fakes only: no socket, no database. Names that do not exist yet are reached
inside each test, so one missing name turns one test red and not the file.
"""

import uuid
from typing import Any

import pytest

from helpdesk.agent_api.agent.confirmation import CHANGE_LINE, change_summary
from helpdesk.agent_api.agent.loop_tools import ToolKind, offered_tools
from helpdesk.agent_api.agent.state import ResolvedEntities, ToolResult, ValidatedCall
from helpdesk.agent_api.agent.tool_args import TOOL_INPUTS

REVOKE = "revoke_access"
LIST_MY_ACCESS = "list_my_access"

PRINCIPALS = (
    "employee",
    "manager",
    "system_owner",
    "hr",
    "it_admin",
    "auditor",
    "platform_admin",
)

GRANT_CRM = uuid.UUID("cccccccc-0000-5000-8000-000000000001")
GRANT_WIKI = uuid.UUID("cccccccc-0000-5000-8000-000000000002")
GRANT_UNLISTED = uuid.UUID("cccccccc-0000-5000-8000-000000000003")
STORED_KEY = "stored-revoke-key-0001"

HELD_LINE = "I'll remove your access to CRM viewer. Say yes to go ahead, or no to stop."
UNNAMED_LINE = "I'll remove this access. Say yes to go ahead, or no to stop."


def _grant_row(
    grant_id: uuid.UUID, system_name: str, system_slug: str, role: str
) -> dict[str, Any]:
    """One `list_my_access` row as the tool server shapes it (the fields the chat reads)."""
    return {
        "grant_id": str(grant_id),
        "role_slug": role,
        "role_name": f"{system_slug}.{role}",
        "privilege": "read",
        "system_slug": system_slug,
        "system_name": system_name,
    }


def _listing() -> ToolResult:
    return ToolResult(
        tool=LIST_MY_ACCESS,
        ok=True,
        payload={
            "grants": [
                _grant_row(GRANT_CRM, "CRM", "crm", "viewer"),
                _grant_row(GRANT_WIKI, "Internal Wiki", "wiki", "editor"),
            ]
        },
    )


def _revoke_call(grant_id: uuid.UUID) -> ValidatedCall:
    arguments = TOOL_INPUTS[REVOKE].model_validate(
        {"grant_id": str(grant_id), "reason": "No longer needed.", "idempotency_key": STORED_KEY}
    )
    return ValidatedCall(tool=REVOKE, arguments=arguments.model_dump(mode="json"))


@pytest.mark.parametrize("principal", PRINCIPALS)
def test_revoke_access_is_offered_as_a_change_to_every_role(principal: str) -> None:
    """Catches an offer cut to the roles that held a revoke cell (an employee
    could not give up their own access from the chat), and one that offers it
    as a lookup, which would run without the person's yes."""
    offered = {tool.name: tool for tool in offered_tools([principal])}

    assert REVOKE in offered, f"{principal} is not offered revoke_access"
    assert offered[REVOKE].kind is ToolKind.CHANGE


def test_the_held_revoke_names_the_access_from_list_my_access() -> None:
    """Catches a line that stays "I'll remove this access.", one that names the
    system only, one that shows the stored role name (`crm.viewer`) or the
    grant id, and one that names the other listed grant. Two grants are listed
    and one is held, so only the held `grant_id` may be named."""
    summary = change_summary([_revoke_call(GRANT_CRM)], [_listing()], ResolvedEntities())

    assert summary == HELD_LINE
    assert summary.endswith(CHANGE_LINE)
    assert "Wiki" not in summary
    assert str(GRANT_CRM) not in summary


def test_a_grant_not_in_this_turns_list_keeps_the_old_line() -> None:
    """Unhappy path. Catches a line that names the first listed grant whatever
    the held id is, and one that raises on a missing row. The held grant is
    absent from the listing and nothing is resolved, so the only honest line
    names nothing."""
    summary = change_summary([_revoke_call(GRANT_UNLISTED)], [_listing()], ResolvedEntities())

    assert summary == UNNAMED_LINE
    assert "CRM" not in summary
    assert "Wiki" not in summary
