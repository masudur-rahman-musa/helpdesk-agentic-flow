"""The `suggest_roles` tool: the caller's own roles closest in meaning to some words.

The agent calls it when a typed system or role matches nothing, so the person
can say "docs" and be offered the wiki. The tenant comes from the token, so no
argument may name one. A suggestion is only a name to offer: it files nothing
and grants nothing, and the policy engine still decides any request made later.
"""

from pydantic import BaseModel, ConfigDict, Field

from helpdesk.mcp_server.services.rag import role_suggestions
from helpdesk.mcp_server.services.rag.role_suggestions import RoleRow, TextVectorCache
from helpdesk.mcp_server.tools.context import ToolContext
from helpdesk.mcp_server.tools.read import ROLES_LIMIT_MAX
from helpdesk.shared.crud.directory import list_role_catalogue

_IGNORE_EXTRAS = ConfigDict(extra="ignore")
"""Extra arguments are dropped, never refused: the tenant comes from the token."""

_CACHE = TextVectorCache()
"""One per process: the embedder it feeds is per process too. The text is the
only key, so no tenant's roles can reach another's answer."""


class SuggestRolesInput(BaseModel):
    """The words the person used. The tenant is not an argument."""

    model_config = _IGNORE_EXTRAS

    query: str = Field(description="The system and role as the person typed them.")


class SuggestedRole(BaseModel):
    system_slug: str
    system_name: str
    role_slug: str
    role_name: str
    score: float


class SuggestRolesOutput(BaseModel):
    """Up to three roles, best first; empty when none is close enough."""

    roles: list[SuggestedRole]


async def suggest_roles(ctx: ToolContext, args: SuggestRolesInput) -> SuggestRolesOutput:
    """Rank this tenant's roles against the words; refuse a query that is empty
    or too long before any model call."""
    query = role_suggestions.check_suggest_query(args.query)
    catalogue = await list_role_catalogue(ctx.session, None, ROLES_LIMIT_MAX)
    rows = [
        RoleRow(
            system_slug=system.slug,
            system_name=system.name,
            role_slug=role.slug,
            role_name=role.name,
        )
        for role, system in catalogue
    ]
    found = role_suggestions.suggest(ctx.clients.embedder(), _CACHE, query, rows)
    return SuggestRolesOutput(
        roles=[
            SuggestedRole(
                system_slug=item.row.system_slug,
                system_name=item.row.system_name,
                role_slug=item.row.role_slug,
                role_name=item.row.role_name,
                score=item.score,
            )
            for item in found
        ]
    )
