"""The next steps a reply may offer, worked out by code.

Pure: no I/O. Another role is offered only when the resolved role's system has
one, and a ticket only when the person's principals may call `create_ticket`,
in the matrix's own words. The writer gets this list and may offer nothing
else.
"""

from collections.abc import Sequence

from helpdesk.agent_api.agent.state import ResolvedEntities
from helpdesk.shared.auth.capabilities import SENTENCES
from helpdesk.shared.auth.rbac import Tool, tools_for


def next_step_lines(principals: Sequence[str], resolved: ResolvedEntities | None) -> list[str]:
    """The other-role line first, when there is one, then the ticket line."""
    lines: list[str] = []
    if resolved is not None and resolved.other_role_names:
        names = ", ".join(resolved.other_role_names)
        lines.append(f"Ask for another role on {resolved.system_name}: {names}.")
    if Tool.CREATE_TICKET in tools_for(principals):
        lines.append(SENTENCES[Tool.CREATE_TICKET])
    return lines
