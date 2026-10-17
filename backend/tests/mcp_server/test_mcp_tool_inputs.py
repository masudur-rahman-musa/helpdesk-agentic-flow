"""An earlier change: the shape of two tool input groups, read off the classes alone.

No Postgres, Redis, fixture or network: class and registry inspection only.
"""

import uuid

from helpdesk.mcp_server.tools.approvals import ApproveRequestInput, RejectRequestInput
from helpdesk.mcp_server.tools.base import MutatingToolInput
from helpdesk.mcp_server.tools.killswitch import SetKillSwitchInput
from helpdesk.mcp_server.tools.registry import REGISTRY
from tests.mcp_server.test_mcp_registry import MUTATING_TOOL_NAMES


def test_set_kill_switch_is_a_mutating_tool() -> None:
    """Catches the half-done change a design note rejects: an input that inherits
    `MutatingToolInput` while the registry spec stays `mutating=False` (dispatch
    never keys the call, so the required key is dead weight), or a spec flipped
    to `mutating=True` over an input that has no key to claim. All three facts
    are asserted because each alone can be true while the others are not."""
    assert issubclass(SetKillSwitchInput, MutatingToolInput)
    assert REGISTRY["set_kill_switch"].mutating is True
    assert "set_kill_switch" in MUTATING_TOOL_NAMES


def test_approve_and_reject_declare_request_id_once() -> None:
    """Catches the duplicate this plan removes: `request_id` declared on both
    inputs. Also catches a base that exists but holds nothing (each class would
    still redeclare the field), and two different bases (one per tool), because
    the shared base must be one class that both inherit and that owns the field.
    """
    shared = [
        cls
        for cls in ApproveRequestInput.__mro__
        if cls in RejectRequestInput.__mro__
        and cls is not MutatingToolInput
        and issubclass(cls, MutatingToolInput)
    ]
    assert shared, "approve and reject must share a base below MutatingToolInput"
    base = shared[0]
    assert base is not ApproveRequestInput and base is not RejectRequestInput
    assert "request_id" in base.__annotations__
    assert "request_id" not in ApproveRequestInput.__annotations__
    assert "request_id" not in RejectRequestInput.__annotations__
    for model in (ApproveRequestInput, RejectRequestInput):
        assert model.model_fields["request_id"].annotation is uuid.UUID
