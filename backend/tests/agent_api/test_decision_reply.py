"""The reply after an approver says yes: fixed words, said to the approver.

Pure: `decision_reply` reads this turn's tool results only. The compose case
uses the staged gateway from `test_agent_compose.py` with no answers staged,
so any model call fails the test at the moment it is made.
"""

from typing import Any

from helpdesk.agent_api.agent.confirmation import decision_reply
from helpdesk.agent_api.agent.state import ToolResult
from helpdesk.shared.core.errors import ErrorCode
from tests.agent_api.test_agent_compose import (
    RecordingSession,
    StagedGateway,
    _compose,
    _state,
)

MIDDLE_REPLY = (
    "You approved REQ-0033 (Production AWS auditor). "
    "It still waits for the department manager and the system owner."
)


def _approved(**payload: Any) -> ToolResult:
    base: dict[str, Any] = {
        "request_number": "REQ-0033",
        "role_name": "Production AWS auditor",
        "status": "pending",
        "slot": "it_admin",
        "chain_complete": False,
        "failure_code": None,
        "waiting_on": ["department_manager", "system_owner"],
    }
    base.update(payload)
    return ToolResult(tool="approve_request", ok=True, payload=base)


def test_a_middle_approval_names_the_role_and_who_is_left() -> None:
    """Catches the writer's old reply, which read the filled seat (`it_admin`)
    as the role and told the approver "Your request is waiting"."""
    reply = decision_reply([_approved()])
    assert reply == MIDDLE_REPLY
    assert "it_admin" not in MIDDLE_REPLY and "Your request" not in MIDDLE_REPLY


def test_the_last_approval_says_the_access_is_given() -> None:
    reply = decision_reply([_approved(chain_complete=True, status="approved", waiting_on=[])])
    assert reply == (
        "You approved REQ-0033 (Production AWS auditor). "
        "That was the last approval, so the access is given."
    )


def test_a_failed_grant_says_so() -> None:
    """Unhappy path: the chain completed but the fresh check refused the grant."""
    reply = decision_reply(
        [_approved(chain_complete=True, status="failed", failure_code="SOD_CONFLICT")]
    )
    assert reply is not None
    assert reply.startswith("You approved REQ-0033 (Production AWS auditor), but the access")
    assert "marked failed" in reply


def test_a_rejection_is_said_to_the_rejecter() -> None:
    result = ToolResult(
        tool="reject_request",
        ok=True,
        payload={
            "request_number": "REQ-0033",
            "role_name": "Production AWS auditor",
            "status": "rejected",
            "slot": "it_admin",
        },
    )
    assert decision_reply([result]) == (
        "You rejected REQ-0033 (Production AWS auditor). "
        "The person who asked is told, with your reason."
    )


def test_two_decisions_get_one_line_each() -> None:
    other = _approved(request_number="REQ-0034", role_name="CRM viewer", waiting_on=["hr"])
    reply = decision_reply([_approved(), other])
    assert reply is not None
    assert reply.split("\n") == [
        MIDDLE_REPLY,
        "You approved REQ-0034 (CRM viewer). It still waits for HR.",
    ]


def test_other_turns_keep_the_writer() -> None:
    """Unhappy path: no result, a failed decision, a listing, or a mix is not a
    decision-only turn, so the writer still answers it."""
    failed = ToolResult(tool="approve_request", ok=False, error_code=ErrorCode.FORBIDDEN)
    listing = ToolResult(tool="list_pending_approvals", ok=True, payload={"approvals": []})
    assert decision_reply([]) is None
    assert decision_reply([failed]) is None
    assert decision_reply([listing]) is None
    assert decision_reply([listing, _approved()]) is None


def test_a_decision_turn_makes_no_model_call() -> None:
    """Catches a compose node that still asks the writer after a decision: the
    gateway has nothing staged, so a model call fails the test."""
    state = _compose(
        _state(tool_results=[_approved()]), StagedGateway(answers=[]), RecordingSession()
    )
    assert state.reply == MIDDLE_REPLY
    assert state.reply_citations == ()
