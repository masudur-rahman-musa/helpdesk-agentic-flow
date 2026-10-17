"""The `list_sod_rules` tool: the tenant's separation-of-duties rules, in words.

The reading and ordering live in the rules service; this is the wire
contract. Only `it_admin` and `platform_admin` may call it.
"""

import uuid

from pydantic import BaseModel, ConfigDict

from helpdesk.mcp_server.services.access_requests.rules import SodRuleRole, read_sod_rules
from helpdesk.mcp_server.tools.context import ToolContext


class ListSodRulesInput(BaseModel):
    """No fields at all.

    Extras are refused, not ignored: a caller sending a `tenant_id` to read
    another tenant's rules must be told no, not quietly given their own.
    """

    model_config = ConfigDict(extra="forbid")


class SodRuleRoleBlock(BaseModel):
    """One side of a rule, resolved to names.

    Null when the role row cannot be read, but the rule is still listed, with
    its id on the row so an admin can act on it.
    """

    role_id: uuid.UUID
    slug: str
    name: str
    system_slug: str
    system_name: str


class SodRuleRow(BaseModel):
    """One rule. Both ids are always set; a null block marks a side that could not be read."""

    rule_id: uuid.UUID
    role_a_id: uuid.UUID
    role_a: SodRuleRoleBlock | None
    role_b_id: uuid.UUID
    role_b: SodRuleRoleBlock | None
    rationale: str
    policy_ref: str


class ListSodRulesOutput(BaseModel):
    """Every rule the tenant has, in the helper's order."""

    rules: list[SodRuleRow]


async def list_sod_rules(ctx: ToolContext, args: ListSodRulesInput) -> ListSodRulesOutput:
    """This tenant's separation-of-duties rules, resolved to names.

    The pipeline's role check is the only permission check. Nothing is written.
    """

    def block(role: SodRuleRole | None) -> SodRuleRoleBlock | None:
        if role is None:
            return None
        return SodRuleRoleBlock(
            role_id=role.role_id,
            slug=role.slug,
            name=role.name,
            system_slug=role.system_slug,
            system_name=role.system_name,
        )

    details = await read_sod_rules(ctx.session)
    return ListSodRulesOutput(
        rules=[
            SodRuleRow(
                rule_id=detail.rule_id,
                role_a_id=detail.role_a_id,
                role_a=block(detail.role_a),
                role_b_id=detail.role_b_id,
                role_b=block(detail.role_b),
                rationale=detail.rationale,
                policy_ref=detail.policy_ref,
            )
            for detail in details
        ]
    )


__all__ = [
    "ListSodRulesInput",
    "ListSodRulesOutput",
    "SodRuleRoleBlock",
    "SodRuleRow",
    "list_sod_rules",
]
