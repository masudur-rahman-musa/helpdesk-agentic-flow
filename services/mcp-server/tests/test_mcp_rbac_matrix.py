"""An earlier change, section 1 criterion 9, section 6: every tool x every principal.

An earlier change owed item (a): the matrix was proved through `authorize`, never
through the transport. A guard that holds in the pure function and is not
wired into the pipeline is not a guard, so every row below is driven through
a real MCP client session over the real streamable HTTP transport.

The expectation is the design written out literally here, the same way
``packages/helpdesk/tests/test_rbac.py`` writes it: importing ``_MATRIX`` and
reimplementing ``authorize`` would make the test drift together with the code
it is supposed to hold still.

Exhaustiveness is over the tools an earlier change REGISTERS. the design asks for every
tool; the rest land as the tools land. What is
exhaustive here is the principal axis: all seven, plus the two ways a token
can carry no principal the matrix recognises.

No pytest-asyncio: each case is a sync function driving one ``asyncio.run``.
"""

import pytest
from helpdesk.auth.rbac import Principal
from mcp_fixtures import (  # noqa: F401 - fixtures are resolved by name
    SeededMcp,
    bearer,
    call_tool,
    error_code,
    mcp_env,
    mcp_pg,
    mcp_redis_base_url,
    mcp_redis_url,
    seeded_mcp_db,
    token_for,
)
from mcp_server.registry import REGISTRY

PRINCIPALS: tuple[str, ...] = (
    "employee",
    "manager",
    "system_owner",
    "hr",
    "it_admin",
    "auditor",
    "platform_admin",
)

# the design, written out per tool and per principal. True = the matrix permits
# this principal to call this tool. Both an earlier change tools are self-scoped reads
# that every principal may make; a future tool with a narrower row goes here as
# an explicit dict, never as `dict.fromkeys(..., True)` copied by habit.
MATRIX: dict[str, dict[str, bool]] = {
    "get_me": dict.fromkeys(PRINCIPALS, True),
    "list_my_access": dict.fromkeys(PRINCIPALS, True),
}

# The two shapes of a token that carries nothing the matrix recognises.
# `authorize` ignores principal strings outside the enum, so both deny.
NO_PRINCIPAL_TOKENS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("no principals at all", ()),
    ("only a principal outside the enum", ("not_a_principal",)),
    ("only a near-miss of a real principal", ("Employee",)),
)

CASES = [(tool_name, principal) for tool_name in sorted(REGISTRY) for principal in PRINCIPALS]


def test_the_principal_axis_is_the_whole_enum() -> None:
    """A row missing from the enum list would silently shrink the matrix."""
    assert set(PRINCIPALS) == {principal.value for principal in Principal}
    assert len(PRINCIPALS) == 7


def test_every_registered_tool_has_a_matrix_row() -> None:
    """A tool registered without a row here would go untested rather than fail."""
    assert set(REGISTRY) == set(MATRIX)


@pytest.mark.parametrize(("tool_name", "principal"), CASES, ids=[f"{t}-{p}" for t, p in CASES])
def test_matrix_expectation_holds_through_the_transport(
    mcp_env: SeededMcp,  # noqa: F811
    tool_name: str,
    principal: str,
) -> None:
    """A token carrying ONLY this principal: refused FORBIDDEN exactly when
    the matrix denies, and not refused when it allows."""
    token = token_for(mcp_env.tenant_a, principals=(principal,))

    result = call_tool(bearer(token), tool_name, {})

    if MATRIX[tool_name][principal]:
        assert result.is_error is False, (
            f"{principal} is permitted to call {tool_name} by SPEC §7, "
            f"but the transport refused: {result.content!r}"
        )
    else:
        assert error_code(result) == "FORBIDDEN", (
            f"{principal} is denied {tool_name} by SPEC §7 and must be refused FORBIDDEN"
        )


@pytest.mark.parametrize(
    ("label", "principals"), NO_PRINCIPAL_TOKENS, ids=[label for label, _ in NO_PRINCIPAL_TOKENS]
)
@pytest.mark.parametrize("tool_name", sorted(REGISTRY))
def test_a_token_with_no_recognised_principal_is_refused(
    mcp_env: SeededMcp,  # noqa: F811
    tool_name: str,
    label: str,
    principals: tuple[str, ...],
) -> None:
    """The refusal direction, through the transport. Both 10a tools allow every
    real principal, so this is where FORBIDDEN is actually reachable — without
    it the matrix test would assert only that nothing is ever refused."""
    token = token_for(mcp_env.tenant_a, principals=principals)

    result = call_tool(bearer(token), tool_name, {})

    assert error_code(result) == "FORBIDDEN", label
