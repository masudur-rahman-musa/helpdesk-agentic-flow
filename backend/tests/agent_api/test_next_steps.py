"""The next steps a reply may offer, worked out by code from the role data and
the permission matrix.

Expected surface: ``helpdesk.agent_api.agent.next_steps.next_step_lines(
principals, resolved) -> list[str]`` and ``ResolvedEntities.other_role_names``.

Cost: nothing. Pure functions and plain objects.
"""

from helpdesk.agent_api.agent.next_steps import next_step_lines
from helpdesk.agent_api.agent.state import ResolvedEntities
from helpdesk.shared.auth.capabilities import SENTENCES
from helpdesk.shared.auth.rbac import Tool


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
