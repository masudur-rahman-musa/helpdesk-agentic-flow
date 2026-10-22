"""Cancelling a waiting request from the chat.

Surface under test (written before it exists):

``helpdesk.agent_api.agent.tool_args``
    ``CancelRequestArgs`` (``request_id``, optional ``reason``, and the
    idempotency key every mutating tool carries) in ``TOOL_INPUTS``.
``helpdesk.agent_api.agent.loop_tools.offered_tools``
    offers ``cancel_request`` as a CHANGE to all seven principals.
``helpdesk.agent_api.agent.gates``
    ``CANCEL_REQUEST`` and ``LIST_MY_REQUESTS`` (the tool names code reads).
``helpdesk.agent_api.agent.confirmation.change_summary``
    the held-cancel line, naming the request from this turn's
    ``list_my_requests`` rows.
``helpdesk.agent_api.agent.graph.held_answer``
    after a yes, a held ``cancel_request`` refused ``REQUEST_NOT_PENDING``
    makes the graph run ``list_my_requests`` once more, so the reply (which has
    no tools) can read the real status. Any other outcome adds no call.
``ErrorCode.REQUEST_NOT_PENDING`` is HTTP 409 in ``core.deps.STATUS_FOR_CODE``
and has its own ``RemoteRequestNotPendingError`` in the MCP client.

Fakes only: the gateway is a ``MockTransport``, the tool server a scripted
fake, the chat row in memory, the clock a plain object. No socket, no
database. Names that do not exist yet are reached inside each test (through
the module), so one missing name turns one test red and not the whole file.
"""

import json
import uuid
from typing import Any

import pytest

from helpdesk.agent_api.agent import gates
from helpdesk.agent_api.agent.confirmation import CHANGE_LINE, OpenQuestion, change_summary
from helpdesk.agent_api.agent.loop_tools import ToolKind, offered_tools
from helpdesk.agent_api.agent.state import ResolvedEntities, ToolResult, ValidatedCall
from helpdesk.agent_api.agent.tool_args import TOOL_INPUTS, planner_schema
from helpdesk.agent_api.core import deps
from helpdesk.agent_api.services import mcp_client
from helpdesk.shared.core.errors import ErrorCode, ToolError
from tests.agent_api.test_agent_loop import (
    YES_MESSAGE,
    FakeStore,
    FakeTools,
    ScriptedGateway,
    answer_body,
    held_question,
    run,
    tool_turns,
)

CANCEL = "cancel_request"
LIST_MY_REQUESTS = "list_my_requests"

PRINCIPALS = (
    "employee",
    "manager",
    "system_owner",
    "hr",
    "it_admin",
    "auditor",
    "platform_admin",
)

REQ_107 = uuid.UUID("bbbbbbbb-0000-5000-8000-000000000107")
REQ_108 = uuid.UUID("bbbbbbbb-0000-5000-8000-000000000108")
STORED_KEY = "stored-cancel-key-0001"

SUMMARY = "I'll cancel this request: REQ-0107 CRM viewer. Say yes to go ahead, or no to stop."


def _row(request_id: uuid.UUID, number: str, status: str) -> dict[str, Any]:
    """One `list_my_requests` row as the tool server shapes it (the fields the chat reads)."""
    return {
        "request_id": str(request_id),
        "request_number": number,
        "status": status,
        "system_name": "CRM",
        "system_slug": "crm",
        "role_slug": "viewer",
        "role_name": "crm.viewer",
    }


def _listing(status: str = "pending") -> dict[str, Any]:
    return {
        "requests": [
            _row(REQ_107, "REQ-0107", status),
            _row(REQ_108, "REQ-0108", "pending"),
        ]
    }


def _cancel_call(request_id: uuid.UUID = REQ_107) -> ValidatedCall:
    arguments = TOOL_INPUTS[CANCEL].model_validate(
        {"request_id": str(request_id), "idempotency_key": STORED_KEY}
    )
    return ValidatedCall(tool=CANCEL, arguments=arguments.model_dump(mode="json"))


def _held_cancel() -> OpenQuestion:
    """A held change from an earlier message: one cancel call under a stored key."""
    return held_question().model_copy(update={"calls": (_cancel_call(),)})


# --- the argument model and the offer ---------------------------------------------


def test_the_cancel_args_carry_the_id_an_optional_reason_and_the_key() -> None:
    """Catches an args model that drops the optional reason, makes it required
    (a bare "cancel it" would be refused), forgets the idempotency key (the
    held call would run unkeyed and a double yes would run twice), or leaves
    the key in what the model is shown."""
    model = TOOL_INPUTS[CANCEL]

    assert set(model.model_fields) == {"request_id", "reason", "idempotency_key"}
    assert model.model_fields["reason"].default is None
    schema = planner_schema(CANCEL)
    assert set(schema["properties"]) == {"request_id", "reason"}
    assert schema["required"] == ["request_id"]


def test_the_gate_names_are_the_tool_names() -> None:
    """Catches a constant that drifts from the wire name, which the graph's
    follow-up read and the summary both match on."""
    assert gates.CANCEL_REQUEST == "cancel_request"
    assert gates.LIST_MY_REQUESTS == "list_my_requests"


@pytest.mark.parametrize("principal", PRINCIPALS)
def test_cancel_request_is_offered_as_a_change_to_every_role(principal: str) -> None:
    """Catches an offer cut to the approver roles (an employee could not take
    back their own request), and one that offers it as a lookup, which would
    run without the person's yes."""
    offered = {tool.name: tool for tool in offered_tools([principal])}

    assert CANCEL in offered, f"{principal} is not offered cancel_request"
    assert offered[CANCEL].kind is ToolKind.CHANGE


# --- the held summary ---------------------------------------------------------------


def test_the_held_cancel_summary_names_the_request() -> None:
    """Catches a summary that falls back to "I'll make this change.", one that
    shows the request id or the stored role name (`crm.viewer`) instead of
    "REQ-0107 CRM viewer", and one that names the other listed request. Two
    requests are listed and one is held, so only the held id may be named."""
    results = (ToolResult(tool=LIST_MY_REQUESTS, ok=True, payload=_listing()),)

    summary = change_summary([_cancel_call()], results, ResolvedEntities())

    assert summary == SUMMARY
    assert summary.endswith(CHANGE_LINE)
    assert "REQ-0108" not in summary
    assert str(REQ_107) not in summary


# --- the follow-up read after a refused cancel --------------------------------------


def _yes_cancel(
    *, failure: ToolError | None, listing_status: str = "approved"
) -> tuple[FakeTools, ScriptedGateway, Any]:
    # No request number or status word in the reply: only the tool results may back those.
    gateway = ScriptedGateway(answer_body("That did not go through, so nothing else changed."))
    tools = FakeTools(
        {CANCEL: {"request_id": str(REQ_107)}, LIST_MY_REQUESTS: _listing(listing_status)},
        failures=None if failure is None else {CANCEL: failure},
    )
    state = run(gateway, tools, FakeStore(question=_held_cancel()), message=YES_MESSAGE)
    return tools, gateway, state


def test_a_cancel_refused_as_not_pending_runs_list_my_requests_once() -> None:
    """Catches a graph that lets the no-tools reply guess the status (it would
    say "cancelled" or "pending" about a request that was approved), one that
    reads the listing twice, and one that runs it but never hands the rows to
    the reply step. The listing says `approved`, and that row must be in the
    reply step's tool turns."""
    refusal = mcp_client.RemoteRequestNotPendingError("REQUEST_NOT_PENDING")

    tools, gateway, state = _yes_cancel(failure=refusal)

    assert tools.names == [CANCEL, LIST_MY_REQUESTS]
    assert len(gateway.requests) == 1
    assert "tools" not in gateway.requests[0]
    turns = tool_turns(gateway.requests[0])
    assert len(turns) == 2
    assert json.loads(turns[0]["content"]) == {"error": "REQUEST_NOT_PENDING"}
    listed = json.loads(turns[1]["content"])
    assert [row["request_number"] for row in listed["requests"]] == ["REQ-0107", "REQ-0108"]
    assert listed["requests"][0]["status"] == "approved"
    assert state.tool_results[-1] == ToolResult(
        tool=LIST_MY_REQUESTS, ok=True, payload=_listing("approved")
    )


@pytest.mark.parametrize(
    "failure_name", ["RemoteForbiddenError", "RemoteNotFoundError", "RemotePolicyBlockedError"]
)
def test_a_cancel_refused_for_any_other_code_runs_no_extra_read(failure_name: str) -> None:
    """Catches a graph that re-reads after every refusal (or every held
    change): only `REQUEST_NOT_PENDING` means "someone decided first". The
    refusal here is `FORBIDDEN`, `NOT_FOUND` or `POLICY_BLOCKED`, and a
    `list_my_requests` call is scripted, so a wrong extra call is recorded
    rather than crashing."""
    failure_class = getattr(mcp_client, failure_name)

    tools, gateway, _state = _yes_cancel(failure=failure_class("refused"))

    assert tools.names == [CANCEL]
    assert len(gateway.requests) == 1


def test_a_cancel_that_works_runs_no_extra_read() -> None:
    """Catches a graph that re-reads after every held cancel, not only after
    the refused one: the reply already has the cancel's own answer."""
    tools, gateway, _state = _yes_cancel(failure=None)

    assert tools.names == [CANCEL]
    assert len(gateway.requests) == 1


# --- the code and its error class ---------------------------------------------------


def test_request_not_pending_is_a_409_with_its_own_remote_error() -> None:
    """Catches a code with no HTTP row (it would fall to 500 and read as an
    outage), a status other than exactly 409, a client map that sends the code
    down `INTERNAL`, a class under the wrong code, and a wire string the
    client cannot parse back to its class."""
    code = ErrorCode("REQUEST_NOT_PENDING")

    assert code.value == "REQUEST_NOT_PENDING"
    assert deps.STATUS_FOR_CODE[code] == 409
    remote = mcp_client.REMOTE_ERRORS[code]
    assert remote is mcp_client.RemoteRequestNotPendingError
    assert remote.code is code
    assert issubclass(remote, mcp_client.RemoteToolError)
    parsed = mcp_client.error_for("REQUEST_NOT_PENDING")
    assert type(parsed) is mcp_client.RemoteRequestNotPendingError
