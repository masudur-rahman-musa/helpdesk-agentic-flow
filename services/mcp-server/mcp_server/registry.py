"""Every tool declared once: wire name, permission, schemas, audit event type.

One declaration per tool means the transport, the RBAC check, the kill
switch and the audit log all read the same name — a tool advertised under a
name the matrix does not know, or one the kill switch cannot store, fails in
`test_mcp_registry.py` rather than at the moment someone needs the stop.

`mutating` is declared but unused in 10a: an earlier change's `run_once` reads it to
decide which calls need an idempotency key, and declaring it now keeps that
a property of the registry rather than a list kept somewhere else.
"""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from helpdesk.auth import rbac
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_server.identity import Identity
from mcp_server.tools import read

SUCCESS_EVENT_TYPE = "tool.succeeded"
"""What a read tool writes when it worked. Per-tool rather than global (plan
10a decision C): row 11's mutating tools declare `grant.executed` and friends
from the design, and a single generic type would have to be replaced then."""

DENIED_EVENT_TYPE = "tool.denied"
"""The caller held a valid token and the matrix refused them."""

DISABLED_EVENT_TYPE = "tool.disabled"
"""An admin had stopped the tool. Distinct from denied: the audit trail must
be able to tell a permission decision from a platform stop."""

FAILED_EVENT_TYPE = "tool.failed"
"""The call reached the tool (or a check other than the two above) and did
not complete. Written from a fresh session so it survives the rollback that
takes the tool's own writes with it."""


@dataclass(frozen=True)
class ToolSpec[I: BaseModel, O: BaseModel]:
    """One tool, declared for every part of the pipeline that needs it.

    `name` is the wire name and equals `tool.value`, asserted by
    `test_mcp_registry.py::test_each_spec_names_a_real_matrix_tool_and_agrees_with_its_key`:
    the string a client sends, the string the kill switch keys on and the
    matrix member are then the same string by construction.
    """

    name: str
    tool: rbac.Tool
    description: str
    input_model: type[I]
    output_model: type[O]
    event_type: str
    mutating: bool
    handler: Callable[[AsyncSession, Identity, I], Awaitable[O]]


_SPECS: tuple[ToolSpec[Any, Any], ...] = (
    ToolSpec(
        name=rbac.Tool.GET_ME.value,
        tool=rbac.Tool.GET_ME,
        description="Return the calling user's own directory record.",
        input_model=read.GetMeInput,
        output_model=read.GetMeOutput,
        event_type=SUCCESS_EVENT_TYPE,
        mutating=False,
        handler=read.get_me,
    ),
    ToolSpec(
        name=rbac.Tool.LIST_MY_ACCESS.value,
        tool=rbac.Tool.LIST_MY_ACCESS,
        description="List the access the calling user holds right now.",
        input_model=read.ListMyAccessInput,
        output_model=read.ListMyAccessOutput,
        event_type=SUCCESS_EVENT_TYPE,
        mutating=False,
        handler=read.list_my_access,
    ),
)

REGISTRY: Mapping[str, ToolSpec[Any, Any]] = MappingProxyType({spec.name: spec for spec in _SPECS})


def lookup(name: str) -> ToolSpec[Any, Any]:
    """The spec for a wire name, or a typed refusal.

    This is the gate the plan's criterion 8 names: the client's string is
    matched against the registry before anything downstream can turn it into
    a Redis key, so an unregistered or malformed name writes nothing at all
    (`test_mcp_registry.py::test_an_unregistered_wire_name_touches_no_redis_key`).

    `UnknownToolError` carries `INTERNAL`, not `FORBIDDEN`: a name outside
    the registry means the caller is wrong, and reading it as a permission
    decision would hide a wiring bug behind a legitimate-looking denial.
    """
    spec = REGISTRY.get(name)
    if spec is None:
        raise rbac.UnknownToolError(f"unknown tool: {name!r}")
    return spec
