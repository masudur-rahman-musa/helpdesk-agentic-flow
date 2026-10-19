"""The tenant settings tool: show or hide the tool steps box in the chat.

Only `platform_admin` may call it. The tenant is the token's, never an
argument, and the stored flag sits beside the tenant's other settings.
"""

from pydantic import BaseModel

from helpdesk.mcp_server.tools.base import MutatingToolInput
from helpdesk.mcp_server.tools.context import ToolContext
from helpdesk.shared.core.errors import NotFoundError
from helpdesk.shared.crud.tenants import set_tool_steps_visible as store_tool_steps_visible


class SetToolStepsVisibleInput(MutatingToolInput):
    """Input for `set_tool_steps_visible`. Extras are refused, so a stray
    `tenant_id` is an error, not a write to the caller's own tenant."""

    show_tool_steps: bool


class SetToolStepsVisibleOutput(BaseModel):
    """What the tenant's chat will now do."""

    show_tool_steps: bool


async def set_tool_steps_visible(
    ctx: ToolContext, args: SetToolStepsVisibleInput
) -> SetToolStepsVisibleOutput:
    """Store the flag for the caller's own tenant. The pipeline's role check
    and audit event wrap this call; nothing here repeats them."""
    stored = await store_tool_steps_visible(
        ctx.session, ctx.identity.tenant_id, args.show_tool_steps
    )
    if not stored:
        raise NotFoundError("no such tenant")
    return SetToolStepsVisibleOutput(show_tool_steps=args.show_tool_steps)


__all__ = [
    "SetToolStepsVisibleInput",
    "SetToolStepsVisibleOutput",
    "set_tool_steps_visible",
]
