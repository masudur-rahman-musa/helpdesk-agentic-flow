"""The next steps a reply may offer, worked out by code from the role data and
the permission matrix.

Expected surface: ``helpdesk.agent_api.agent.next_steps.next_step_lines(
principals, resolved) -> list[str]`` and ``ResolvedEntities.other_role_names``.

Cost: nothing. Pure functions and plain objects.
"""

import pytest

from helpdesk.agent_api.agent.next_steps import next_step_lines, without_ticket_offer
from helpdesk.agent_api.agent.state import Intent, ResolvedEntities, TurnState
from helpdesk.shared.auth.capabilities import SENTENCES
from helpdesk.shared.auth.rbac import Tool
from tests.agent_api.test_agent_compose import RecordingSession, StagedGateway, _compose, _state


def test_a_role_on_a_one_role_system_offers_no_other_role() -> None:
    """Catches an implementation that always offers "another role" (or "a
    different level") for a resolved system, whatever the role data holds:
    VPN has one role, so the reply would invite a choice that does not exist."""
    resolved = ResolvedEntities(
        system_slug="vpn", system_name="VPN", role_slug="vpn-user", role_name="User"
    )

    lines = next_step_lines(["employee"], resolved)

    assert not any("VPN" in line for line in lines)
    assert not any("another role" in line for line in lines)


def test_other_roles_on_the_system_are_named() -> None:
    """Catches an implementation that names the roles in another format, drops
    the system name, or leaves out the roles the person could ask for instead."""
    resolved = ResolvedEntities(
        system_slug="kyc",
        system_name="KYC",
        role_slug="kyc-analyst",
        role_name="Analyst",
        other_role_names=("Approver", "Viewer"),
    )

    lines = next_step_lines(["employee"], resolved)

    assert "Ask for another role on KYC: Approver, Viewer." in lines


def test_the_ticket_line_comes_from_the_permission_matrix() -> None:
    """Catches an implementation that hard-codes its own ticket wording instead
    of using the sentence the permission matrix already owns."""
    lines = next_step_lines(["employee"], None)

    assert SENTENCES[Tool.CREATE_TICKET] in lines


def test_unknown_principals_and_nothing_resolved_give_no_steps() -> None:
    """Catches an implementation that offers the ticket to a person whose
    principals the matrix does not know, or invents a role line with nothing
    resolved. Principals are unknown and nothing is resolved, so no other guard
    can supply the empty list."""
    assert next_step_lines(["nobody"], None) == []


@pytest.mark.parametrize(
    "intent", [Intent.REQUEST_ACCESS, Intent.CHECK_MY_ACCESS, Intent.RESET_PASSWORD]
)
def test_no_ticket_on_a_turn_the_chat_handles(intent: Intent) -> None:
    """Catches a next-step list that offers a ticket for something the chat does
    itself (a request, an access check, a password reset). The same employee
    gets the ticket line when no intent is given (the test below pins that),
    so only the intent rule can empty it."""
    assert SENTENCES[Tool.CREATE_TICKET] in next_step_lines(["employee"], None)
    assert next_step_lines(["employee"], None, intent) == []


@pytest.mark.parametrize(
    ("intent", "tool_failed"),
    [
        pytest.param(Intent.OTHER, False, id="other"),
        pytest.param(Intent.OPEN_TICKET, False, id="open-ticket"),
        pytest.param(Intent.CHECK_MY_ACCESS, True, id="failed-tool"),
        pytest.param(Intent.REQUEST_ACCESS, True, id="failed-request"),
    ],
)
def test_ticket_on_other_or_failed_turn(intent: Intent, tool_failed: bool) -> None:
    """Catches a rule that strips the ticket line everywhere: an `other` turn, an
    `open_ticket` turn and any turn where a tool failed still offer the ticket
    the matrix allows, so a person is never left with no way forward."""
    lines = next_step_lines(["employee"], None, intent, tool_failed=tool_failed)
    assert lines == [SENTENCES[Tool.CREATE_TICKET]]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(
            "You hold the VPN user role. If you need more, open a ticket.",
            "You hold the VPN user role.",
            id="open",
        ),
        pytest.param(
            "You hold the VPN user role. I can raise a ticket for you. Anything else?",
            "You hold the VPN user role. Anything else?",
            id="raise-middle",
        ),
        pytest.param(
            "Your request is REQ-0007 and it is waiting. You can create a ticket if it stalls.",
            "Your request is REQ-0007 and it is waiting.",
            id="create",
        ),
        pytest.param(
            "Ticket TCK-0002 is open. Nothing else is needed.",
            "Ticket TCK-0002 is open. Nothing else is needed.",
            id="mention-kept",
        ),
        pytest.param("I can file a ticket for you.", "I can file a ticket for you.", id="only"),
    ],
)
def test_unoffered_ticket_sentence_is_dropped(text: str, expected: str) -> None:
    """Catches a composed reply that still offers a ticket on a turn that offered
    none: the sentence is cut. A sentence that only mentions a ticket number is
    kept, and a text that would be left empty comes back whole (it is the only
    thing the person has to read)."""
    assert without_ticket_offer(text) == expected


OFFER = "If you need more, open a ticket."


def _turn_of(intent: Intent) -> TurnState:
    """A finished turn of one intent, for an employee, with no tool failure."""
    state = _state()
    assert state.extraction is not None
    return state.model_copy(
        update={
            "principals": ("employee",),
            "extraction": state.extraction.model_copy(update={"intent": intent}),
        }
    )


def test_compose_cuts_the_unoffered_ticket_sentence() -> None:
    """Catches a `compose_reply` that sends the writer's own ticket offer through
    on a `check_my_access` turn, where no ticket line was offered. The writer's
    text is the same on both turns; only the intent differs, and an `other`
    turn keeps its sentence, so only the not-offered rule can cut it."""
    text = f"You hold VPN user. {OFFER}"

    checked = _compose(_turn_of(Intent.CHECK_MY_ACCESS), StagedGateway(text), RecordingSession())
    other = _compose(_turn_of(Intent.OTHER), StagedGateway(text), RecordingSession())

    assert checked.reply == "You hold VPN user."
    assert other.reply == text
