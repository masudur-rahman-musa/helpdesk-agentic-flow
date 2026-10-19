"""Pydantic models for the POST /admin/settings switch."""

from pydantic import BaseModel, ConfigDict, StrictBool


class SetToolStepsVisibleArgs(BaseModel):
    """`set_tool_steps_visible`'s arguments, built by the route.

    No tenant or user id: both come from the token.
    """

    show_tool_steps: bool
    idempotency_key: str


class SetToolStepsVisibleBody(BaseModel):
    """The browser's half of the switch.

    Extra fields are refused, so a browser-chosen key or tenant id is an error,
    not ignored. The value must be a real true or false, not `"yes"` or `1`.
    """

    model_config = ConfigDict(extra="forbid")

    show_tool_steps: StrictBool
