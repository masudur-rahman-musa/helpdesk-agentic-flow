"""Pydantic models for the /admin/killswitch board and toggle."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from helpdesk.shared.services.killswitch import REASON_MAX_LENGTH, TOOL_NAME_MAX_LENGTH


class ListKillSwitchesArgs(BaseModel):
    """No arguments: the tenant is the token's and the tools are the server's."""


class SetKillSwitchArgs(BaseModel):
    """`set_kill_switch`'s arguments, built by the route.

    No tenant or user id. `scope` is "global" or the token's own tenant id.
    """

    tool_name: str
    enabled: bool
    scope: str
    reason: str
    idempotency_key: str


class SetKillSwitchBody(BaseModel):
    """The browser's half of a toggle.

    Extra fields are refused: an ignored `tenant_id` would answer 200 to an
    admin who thinks they stopped a tool for one tenant. The length caps are
    the tool's own constants, so the two sides cannot disagree.
    """

    model_config = ConfigDict(extra="forbid")

    tool_name: str = Field(min_length=1, max_length=TOOL_NAME_MAX_LENGTH)
    enabled: bool
    scope: Literal["global", "tenant"]
    """A tenant id is not allowed here, so no client can pick the tenant."""

    reason: str = Field(min_length=1, max_length=REASON_MAX_LENGTH)
    """Required both ways, so turning a tool back on is on record too."""
