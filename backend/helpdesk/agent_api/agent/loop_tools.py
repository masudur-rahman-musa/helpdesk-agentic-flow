"""The tools the agent loop offers, and the two finders it runs itself.

The list offered is cut to what the caller's principals may call
(`rbac.tools_for`), so the model never sees a tool the person cannot use.
That only says what to show: every call still goes through MCP with the
caller's own token, where the tool's own check holds.

Three kinds. A lookup is an MCP read tool, run as asked. A finder
(`find_role`, `find_person`) runs here, so loose words, typos and picks
become real rows without a new MCP tool. A change is an MCP mutating tool
the loop only holds for a yes; it is offered with its planner schema, which
has no idempotency key (`test_agent_loop.py`).
"""

import difflib
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from pydantic import ValidationError

from helpdesk.agent_api.agent import matching
from helpdesk.agent_api.agent.matching import Candidate
from helpdesk.agent_api.agent.role_words import plain_role_name
from helpdesk.agent_api.agent.tool_args import (
    SUGGEST_ROLES,
    ListRequestableRolesArgs,
    SuggestRolesArgs,
    SuggestRolesResult,
    planner_schema,
)
from helpdesk.agent_api.services.mcp_client import ToolCaller
from helpdesk.shared.auth import rbac
from helpdesk.shared.core.errors import ToolError

FIND_ROLE = "find_role"
FIND_PERSON = "find_person"
LIST_REQUESTABLE_ROLES = "list_requestable_roles"

MAX_FOUND = 3
"""Rows one finder answers: few enough to read, and a cap on how much of the
directory one word lists."""

CATALOGUE_LIMIT = 200
"""The most roles `list_requestable_roles` answers in one page."""


class ToolKind(StrEnum):
    """How the loop treats a call: run it, run it here, or hold it for a yes."""

    LOOKUP = "lookup"
    FIND = "find"
    CHANGE = "change"


@dataclass(frozen=True)
class LoopTool:
    """One tool as the model is offered it."""

    name: str
    description: str
    parameters: dict[str, Any]
    kind: ToolKind

    def wire(self) -> dict[str, Any]:
        """The OpenAI function shape the gateway takes."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


_WORDS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"words": {"type": "string", "description": "The words the person used."}},
    "required": ["words"],
}

_LOOKUPS: Mapping[str, str] = {
    "get_me": "The person's own record: their user id, name and department.",
    "list_my_access": "The access the person holds now.",
    "list_my_requests": "The person's access requests and where each stands.",
    "list_my_tickets": "The person's tickets and where each stands.",
    "list_pending_approvals": "The access requests waiting for this person to approve.",
    "search_policy": "Search the company's access rules for passages that answer a question.",
    "classify_access_request": (
        "Who approves a role and what the policy decides for it, before anything is filed."
    ),
}
"""Each lookup the loop offers, with the words the model reads. The name is
also the MCP tool and the RBAC row."""

_CHANGES: Mapping[str, tuple[str, frozenset[rbac.Tool]]] = {
    "create_access_request": (
        "Propose filing an access request. The person sees a summary and must say yes.",
        frozenset(
            {rbac.Tool.CREATE_ACCESS_REQUEST_SELF, rbac.Tool.CREATE_ACCESS_REQUEST_ON_BEHALF}
        ),
    ),
    "revoke_access": ("Propose removing a grant.", frozenset({rbac.Tool.REVOKE_ACCESS})),
    "onboard_user": ("Propose setting up a new joiner.", frozenset({rbac.Tool.ONBOARD_USER})),
    "transfer_user": (
        "Propose moving a person to another department.",
        frozenset({rbac.Tool.TRANSFER_USER}),
    ),
    "offboard_user": ("Propose removing a leaver's access.", frozenset({rbac.Tool.OFFBOARD_USER})),
    "open_access_review": (
        "Propose opening an access review.",
        frozenset({rbac.Tool.OPEN_ACCESS_REVIEW}),
    ),
    "decide_review_item": (
        "Propose keeping or revoking one flagged grant in a review.",
        frozenset({rbac.Tool.DECIDE_REVIEW_ITEM}),
    ),
    "approve_request": (
        "Propose approving a request listed by list_pending_approvals.",
        frozenset({rbac.Tool.APPROVE_REQUEST}),
    ),
    "reject_request": (
        "Propose rejecting a request listed by list_pending_approvals.",
        frozenset({rbac.Tool.REJECT_REQUEST}),
    ),
    "reset_own_password": (
        "Propose resetting the person's own password.",
        frozenset({rbac.Tool.RESET_OWN_PASSWORD}),
    ),
    "unlock_own_account": (
        "Propose unlocking the person's own account.",
        frozenset({rbac.Tool.UNLOCK_OWN_ACCOUNT}),
    ),
    "create_ticket": (
        "Propose opening a ticket for a person on the helpdesk team.",
        frozenset({rbac.Tool.CREATE_TICKET}),
    ),
}
"""Each change the loop may hold, and the RBAC rows any one of which lets it
be offered. Filing has two rows, self and on behalf, under one MCP name."""

_ACTS_FOR_OTHERS = frozenset(
    {
        rbac.Tool.CREATE_ACCESS_REQUEST_ON_BEHALF,
        rbac.Tool.ONBOARD_USER,
        rbac.Tool.TRANSFER_USER,
        rbac.Tool.OFFBOARD_USER,
    }
)
"""A role holding any of these acts for someone else, so it may look people up."""


def offered_tools(principals: Sequence[str]) -> tuple[LoopTool, ...]:
    """The tools these principals are offered: lookups, finders, then changes."""
    allowed = frozenset(rbac.tools_for(principals))
    offered = [
        LoopTool(name, description, planner_schema(name), ToolKind.LOOKUP)
        for name, description in _LOOKUPS.items()
        if rbac.Tool(name) in allowed
    ]
    if rbac.Tool.LIST_REQUESTABLE_ROLES in allowed:
        offered.append(
            LoopTool(
                FIND_ROLE,
                "Find a role from loose words, typos included. Answers up to 3 roles "
                "with their id, plain name, system and whether the person holds it.",
                _WORDS_SCHEMA,
                ToolKind.FIND,
            )
        )
    if allowed & _ACTS_FOR_OTHERS:
        offered.append(
            LoopTool(
                FIND_PERSON,
                "Find a person in the company by name. Answers up to 3 people "
                "with their id, name and department.",
                _WORDS_SCHEMA,
                ToolKind.FIND,
            )
        )
    offered.extend(
        LoopTool(name, description, planner_schema(name), ToolKind.CHANGE)
        for name, (description, rows) in _CHANGES.items()
        if allowed & rows
    )
    return tuple(offered)


@dataclass(frozen=True)
class PersonRow:
    """One person as the finder may show them. No email, on purpose."""

    id: uuid.UUID
    display_name: str
    department: str


class Directory(Protocol):
    """The tenant's directory reads the finders need, bound to the caller's tenant."""

    async def role_ids(self) -> Mapping[tuple[str, str], uuid.UUID]: ...

    async def people(self) -> Sequence[PersonRow]: ...


async def find_role(words: str, *, tools: ToolCaller, directory: Directory) -> list[dict[str, Any]]:
    """Up to 3 roles the words name, from the person's requestable roles.

    The name matcher runs first; when it finds nothing, the roles close in
    meaning (`suggest_roles`) answer instead, and a failed suggestion is no
    rows. A role the directory has no id for is left out, so every id
    answered is real.
    """
    payload = await tools.call_tool(
        LIST_REQUESTABLE_ROLES, ListRequestableRolesArgs(limit=CATALOGUE_LIMIT)
    )
    listed: dict[tuple[str, str], Mapping[str, Any]] = {}
    for row in payload.get("roles") or []:
        if isinstance(row, Mapping):
            listed[(str(row.get("system_slug")), str(row.get("role_slug")))] = row

    keys = {uuid.uuid5(uuid.NAMESPACE_URL, f"{slug[0]}.{slug[1]}"): slug for slug in listed}
    candidates = [
        Candidate(
            id=key,
            slug=f"{slug[0]} {slug[1]}",
            name=plain_role_name(str(listed[slug].get("system_name")), slug[1]),
        )
        for key, slug in keys.items()
    ]
    found = [
        (slug, str(listed[slug].get("system_name")))
        for slug in (
            keys[candidate.id] for candidate in matching.match_candidates(words, candidates)
        )
    ]
    if not found:
        try:
            suggested = SuggestRolesResult.model_validate(
                dict(await tools.call_tool(SUGGEST_ROLES, SuggestRolesArgs(query=words)))
            ).roles
        except (ToolError, ValidationError):
            suggested = []
        found = [((role.system_slug, role.role_slug), role.system_name) for role in suggested]

    ids = await directory.role_ids()
    rows: list[dict[str, Any]] = []
    for slug, system_name in found:
        role_id = ids.get(slug)
        if role_id is None:
            continue
        rows.append(
            {
                "role_id": str(role_id),
                "name": plain_role_name(system_name, slug[1]),
                "system": system_name,
                "already_held": bool(listed.get(slug, {}).get("already_held", False)),
            }
        )
    return rows[:MAX_FOUND]


async def find_person(words: str, *, directory: Directory) -> list[dict[str, Any]]:
    """Up to 3 people the words name: every typed word close to a word of the
    name, or the whole name close, at the matcher's fuzzy cut-off. Best first."""
    typed = matching.normalise(words)
    if not typed:
        return []
    scored: list[tuple[float, PersonRow]] = []
    for person in await directory.people():
        name = matching.normalise(person.display_name)
        whole = difflib.SequenceMatcher(None, typed, name).ratio()
        name_words = name.split()
        every_word = all(
            any(
                difflib.SequenceMatcher(None, word, part).ratio() >= matching.FUZZY_CUTOFF
                for part in name_words
            )
            for word in typed.split()
        )
        if every_word or whole >= matching.FUZZY_CUTOFF:
            scored.append((whole, person))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [
        {"user_id": str(person.id), "name": person.display_name, "department": person.department}
        for _, person in scored[:MAX_FOUND]
    ]
