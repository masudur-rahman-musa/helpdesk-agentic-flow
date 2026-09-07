"""One argument model per tool the agent may call, and one result model.

Decision C, and the reason this file is a copy of something: agent-api and
mcp-server are separate deployables that talk over HTTP and never import each
other, so the shapes have to exist on both sides. What makes the copy TRUE is
`test_agent_tool_catalogue.py`, which reads `tools/list` off the real server
and compares property names, required sets and `top_k`'s default. A rename on
the server is a red suite here; runtime validation against an advertised
schema would instead be a mid-turn failure for a user.

This plan registers `search_policy` alone. 17c adds the rest, each with its
own drift row: a model with no advertised counterpart is validated by nothing.
"""

from collections.abc import Mapping
from types import MappingProxyType

from pydantic import BaseModel, Field

from agent_api.graph.state import Citation

SEARCH_POLICY = "search_policy"
"""The tool name as the server's registry spells it (the design)."""

SEARCH_POLICY_TOP_K = 6
"""How many policy chunks one retrieval asks for.

The corpus default 15a fixes, written out here rather than imported from
`helpdesk.rag.retrieval`: importing that module would pull fastembed and the
vector store into agent-api, which reaches the corpus only through the tool.
The copy is held true by
`test_agent_tool_catalogue.py::test_the_advertised_top_k_default_is_the_catalogues_own`,
which reads the number off the server's own advertisement.
"""


class SearchPolicyArgs(BaseModel):
    """A question and a page size. The tenant is NOT an argument.

    No identity of any kind is declared here: the collection
    searched is derived from the token by the server, and an argument naming a
    tenant would be dropped there rather than honoured. No bound is declared
    either — `retrieval.check_query` and `check_top_k` run on the server before
    any effect, so the cap keeps one home and one error message.
    """

    query: str = Field(description="The policy question, in the user's own words.")
    top_k: int = Field(
        default=SEARCH_POLICY_TOP_K,
        description="How many policy chunks to return, at most.",
    )


class SearchPolicyResult(BaseModel):
    """The page, and whether the tool refused to answer from it.

    `refused` is READ, never recomputed (decision D): the threshold lives with
    `should_refuse` in 15a, so a tuning change is one number in one place.
    `citations` is empty whenever `refused` is true, which is the server's
    guarantee and the reason a refused turn can cite nothing.
    """

    citations: list[Citation]
    refused: bool


TOOL_INPUTS: Mapping[str, type[BaseModel]] = MappingProxyType({SEARCH_POLICY: SearchPolicyArgs})
"""Every tool the agent may call, by name, with the model its arguments take.

A read-only mapping on purpose: it is the set the drift test sweeps, so a tool
registered at runtime would be a tool nothing compares against the server.
Pinned by
`test_agent_tool_catalogue.py::test_tool_inputs_holds_exactly_the_one_tool_this_plan_registers`.
"""

__all__ = [
    "SEARCH_POLICY",
    "SEARCH_POLICY_TOP_K",
    "TOOL_INPUTS",
    "SearchPolicyArgs",
    "SearchPolicyResult",
]
