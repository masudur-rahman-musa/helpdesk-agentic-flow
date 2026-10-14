"""The `list_sod_rules` tool: the design's Admin group, the SoD rules read.

`list_sod_rules` is the read the design's `/admin/rules` page is
drawn from: the tenant's separation-of-duties rules with both sides resolved
to a role and system a person can read. Everything it decides lives in
`helpdesk.access.rules` -- the three statements, the total order and the null
block on a side that cannot be resolved -- and what lives here is the wire
contract alone. It takes no arguments, and its two cells are `it_admin` and
`platform_admin`, the design's audience for that page.

No function here opens a session or touches the transport: the pipeline binds
the session to the token's tenant before the handler is called.
"""

import uuid

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.mcp_server.core.identity import Identity
from helpdesk.mcp_server.services.access_requests.rules import SodRuleRole, read_sod_rules


class ListSodRulesInput(BaseModel):
    """No fields at all, and nothing for a caller to choose.

    The tenant is the token's and the rules are that tenant's own rows (hard
    rule 6), so there is no argument to trust and none to validate.
    `extra="forbid"` rather than `ignore`, exactly as `ListKillSwitchesInput`:
    a caller who sends a `tenant_id` believing they are reading another
    tenant's rules must be refused, not quietly answered with their own.
    Pinned as an EXACTLY empty field set by
    `test_mcp_list_sod_rules.py::test_the_tool_takes_no_arguments_at_all`,
    which no later field can ride along into.
    """

    model_config = ConfigDict(extra="forbid")


class SodRuleRoleBlock(BaseModel):
    """One side of a rule, resolved. Exactly five fields (plan criterion 4).

    Null on a side whose role row cannot be read, never absent and never a
    dropped rule: the id stays top-level on the row so the admin reading the
    page can act on it (decision B). Pinned by
    `test_mcp_list_sod_rules.py::test_the_board_answers_the_envelope_and_an_exact_seven_key_row`.
    """

    role_id: uuid.UUID
    slug: str
    name: str
    system_slug: str
    system_name: str


class SodRuleRow(BaseModel):
    """One rule as 24eb puts it on the wire. Exactly seven fields.

    Both ids are always present and each resolved block may be null, which is
    the whole of decision B: nullability is the marker, and no flag is
    carried beside it because there is no second fact to say.
    """

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


async def list_sod_rules(
    session: AsyncSession, identity: Identity, args: ListSodRulesInput
) -> ListSodRulesOutput:
    """This tenant's separation-of-duties rules, resolved to names.

    What the design's `/admin/rules` page is drawn from. The rows have been
    readable as a DECISION since an earlier change -- `snapshot.py` hands the engine
    three ids and a `policy_ref` -- and readable as RULES nowhere: which two
    roles conflict, in words, and why, is the question this answers and
    nothing else did.

    Transport only. The order, the three statements and the null block on an
    unresolvable side are `helpdesk.access.rules.read_sod_rules`'s, so 24f's
    page and every later caller read one sequence rather than a second copy
    inside a handler; the mapping below adds nothing but the wire's names.

    No scope helper and no second permission check: the design gives the row two
    cells, both `Scope.ANY`, so the pipeline's matrix stage IS the whole check
    and deleting it hands the rules to the five principals with no cell,
    pinned by `test_mcp_list_sod_rules.py::test_the_five_denied_principals_read_no_rules`
    (a project rule: there is no second copy of that guard to hide behind).

    Nothing is written, so the pipeline's generic `tool.succeeded` is the
    whole record a read leaves
    (`::test_one_call_writes_exactly_one_event_naming_the_tool_and_the_caller`).
    The session is the pipeline's, already bound to the token's tenant.
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

    details = await read_sod_rules(session)
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
