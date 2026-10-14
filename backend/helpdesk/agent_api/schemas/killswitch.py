"""Pydantic models for the /admin/killswitch board and toggle."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from helpdesk.shared.killswitch import REASON_MAX_LENGTH, TOOL_NAME_MAX_LENGTH


class ListKillSwitchesArgs(BaseModel):
    """No arguments at all: the tenant is the token's and the tool list is the
    server's own registry.

    The zero-argument form `me_routes` uses. `ListKillSwitchesInput` declares
    no field and forbids extras, so a key riding along from here would answer
    `VALIDATION` on every load of the board."""


class SetKillSwitchArgs(BaseModel):
    """`set_kill_switch`'s SPEC section 8.2 arguments, assembled by this router.

    Exactly four keys: no idempotency key (13c decision C) and no tenant or
    user id. `scope` is a `str` carrying either `SCOPE_GLOBAL` or
    the token's own tenant id rendered as its string, which is the pair
    `SetKillSwitchInput`'s `Literal["global"] | uuid.UUID` accepts.
    """

    tool_name: str
    enabled: bool
    scope: str
    reason: str


class SetKillSwitchBody(BaseModel):
    """The browser's half of a toggle: the four keys, and nothing else.

    `extra="forbid"` is load-bearing rather than tidy: a `tenant_id` here is
    the browser asking to name the tenant a switch is written for, and an
    ignored one would answer 200 to an admin who believed they had stopped a
    tool for one tenant while the switch went wherever `scope` pointed
    (`test_killswitch_routes.py::test_a_tenant_id_riding_along_in_the_body_is_a_typed_400_and_opens_no_session`).

    The two string bounds are the TOOL's own constants, imported and never
    retyped: `tool_name` becomes a Redis key segment
    through `key_for` and `reason` becomes a stored `SwitchState` field, so
    both get their pre-check before any effect runs, and a second opinion one
    character narrower than the store's would refuse a value the tool accepts.
    """

    model_config = ConfigDict(extra="forbid")

    tool_name: str = Field(min_length=1, max_length=TOOL_NAME_MAX_LENGTH)
    enabled: bool
    scope: Literal["global", "tenant"]
    """The one bound this router owns, and the one it must own: a tenant id is
    not a member, so no client-supplied tenant can reach the tool from here at
    all (decision C)."""

    reason: str = Field(min_length=1, max_length=REASON_MAX_LENGTH)
    """Required in both directions, like the tool's own: clearing a switch has
    nowhere to store a reason, so the `killswitch.changed` payload is the only
    record that turning a tool back on was a decision somebody made (13c
    decision H)."""
