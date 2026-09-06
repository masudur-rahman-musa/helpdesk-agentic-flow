"""An earlier change, section 1 criterion 9, section 6: every tool x every principal.

An earlier change owed item (a): the matrix was proved through `authorize`, never
through the transport. A guard that holds in the pure function and is not
wired into the pipeline is not a guard, so every row below is driven through
a real MCP client session over the real streamable HTTP transport.

The expectation is the design written out literally here, the same way
``packages/helpdesk/tests/test_rbac.py`` writes it: importing ``_MATRIX`` and
reimplementing ``authorize`` would make the test drift together with the code
it is supposed to hold still.

Exhaustiveness is over the tools the registry HOLDS.
the design asks for every tool; the rest land as the tools land (an earlier change,
scope note 2). What is exhaustive here is the principal axis: all seven,
plus the two ways a token can carry no principal the matrix recognises.

A permitted call must actually succeed for the assertion to pin RBAC rather
than a later stage, so each mutating tool is called with minimally valid
arguments (a fresh idempotency key per call). Denials need no arguments at
all: RBAC runs before input validation, which is itself pinned by
``test_mcp_pipeline.py``'s criterion-5 pair.

The CROSS-TENANT axis (the design, owed by an earlier change decision B, added by plan
11d) is the second sweep here: a tenant-A token carrying one principal,
called with tenant-B arguments, for every tool that ACCEPTS an id and every
one of the seven principals. An earlier change decision G narrows the tool axis
deliberately: a tool whose only input is the token has no cross-tenant call
to make, so forcing a synthetic id onto it would test the argument parser and
not the isolation. Those five identity-only tools get one own-tenant case
each instead, and ``ID_TAKING_TOOLS`` plus ``NO_ID_TOOLS`` are asserted to
cover the registry exactly, so the narrowing stays a decision rather than
becoming an omission the next tool inherits.

No pytest-asyncio: each case is a sync function driving one ``asyncio.run``.
"""

import asyncio
import uuid
from functools import cache
from typing import Any

import pytest
from helpdesk.auth.rbac import Principal
from helpdesk.models import AccessRequest, Grant, RequestStatus, User
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
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import create_async_engine

from scripts.seed import CUAN, NORDLICHT, TenantRows, generate_tenant

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
# this principal to call this tool. All eight registered tools are granted to
# every principal by the design (the reads are self-scoped, the three helpdesk
# mutations self-only or any, and every principal may classify, list their own
# requests, and file one for THEMSELVES); a future tool with a narrower row
# goes here as an explicit dict, never as `dict.fromkeys(..., True)` copied by
# habit.
#
# `create_access_request` is the self row of the design's pair, and the arguments
# below name the caller as the target so that is the row this exercises. The
# on-behalf row (`manager` own_department; `hr`, `it_admin`, `platform_admin`
# any) is a permission checked inside the handler, not a registered tool, so
# it is not on this axis: it is pinned by the two on-behalf cases in
# `test_mcp_create_access_request.py`.
MATRIX: dict[str, dict[str, bool]] = {
    "get_me": dict.fromkeys(PRINCIPALS, True),
    "list_my_access": dict.fromkeys(PRINCIPALS, True),
    "reset_own_password": dict.fromkeys(PRINCIPALS, True),
    "unlock_own_account": dict.fromkeys(PRINCIPALS, True),
    "create_ticket": dict.fromkeys(PRINCIPALS, True),
    "classify_access_request": dict.fromkeys(PRINCIPALS, True),
    "list_my_requests": dict.fromkeys(PRINCIPALS, True),
    "create_access_request": dict.fromkeys(PRINCIPALS, True),
    # An earlier change. The first rows on this axis that are not "everyone": the design
    # gives the approval row four cells and leaves `employee`, `hr` and
    # `auditor` without one. `hr` being denied here is the recorded the design /
    # §9.3 conflict, not an oversight.
    "list_pending_approvals": {
        "employee": False,
        "manager": True,
        "system_owner": True,
        "hr": False,
        "it_admin": True,
        "auditor": False,
        "platform_admin": True,
    },
    "approve_request": {
        "employee": False,
        "manager": True,
        "system_owner": True,
        "hr": False,
        "it_admin": True,
        "auditor": False,
        "platform_admin": True,
    },
    "reject_request": {
        "employee": False,
        "manager": True,
        "system_owner": True,
        "hr": False,
        "it_admin": True,
        "auditor": False,
        "platform_admin": True,
    },
    # An earlier change. the design's revoke row, written out literally rather than
    # derived from `_MATRIX`: five cells, and `hr` DOES hold one here (unlike
    # the approval rows above), so a row copied from `approve_request` by
    # habit fails. `employee` and `auditor` have no cell at all.
    "revoke_access": {
        "employee": False,
        "manager": True,
        "system_owner": True,
        "hr": True,
        "it_admin": True,
        "auditor": False,
        "platform_admin": True,
    },
}

# Tools whose PERMITTED call cannot succeed on arguments alone: both deciding
# tools need a real pending request the caller holds a seat on, and building
# one per (tool, principal) cell would make this file a second approvals
# suite. A random request id answers NOT_FOUND, which is the assertion that
# matters here — the matrix let the caller through and a LATER stage refused
# them, so a permitted cell can never be satisfied by a FORBIDDEN.
# `test_mcp_approvals.py` owns what happens after that stage.
ALLOW_ANSWERS_NOT_FOUND = frozenset({"approve_request", "reject_request", "revoke_access"})

# An earlier change decision G. The tools that take an id argument, which is the axis
# the cross-tenant sweep can actually exercise, and the rest. The two sets are
# asserted to partition the registry, so a new tool has to be classified
# rather than silently skipped.
ID_TAKING_TOOLS: frozenset[str] = frozenset(
    {
        "classify_access_request",
        "create_access_request",
        "approve_request",
        "reject_request",
        "revoke_access",
    }
)
NO_ID_TOOLS: frozenset[str] = frozenset(
    {
        "get_me",
        "list_my_access",
        "list_my_requests",
        "reset_own_password",
        "unlock_own_account",
        "create_ticket",
        "list_pending_approvals",
    }
)
# The five decision G names explicitly: everything they answer is decided by
# the token alone, so each gets one own-tenant case below. `create_ticket` and
# `list_pending_approvals` take no id either, but they are not identity-only
# reads of the caller's own record and are covered by their own suites.
IDENTITY_ONLY_TOOLS: tuple[str, ...] = (
    "get_me",
    "list_my_access",
    "list_my_requests",
    "reset_own_password",
    "unlock_own_account",
)


@cache
def _tenant_rows(slug: str) -> TenantRows:
    """The deterministic seed rows for one tenant, computed once: a classify
    call needs a real role id in the caller's own tenant."""
    return generate_tenant({CUAN.slug: CUAN, NORDLICHT.slug: NORDLICHT}[slug])


def _arguments_for(tool_name: str, env: SeededMcp) -> dict[str, Any]:
    """Minimally valid arguments, so a permitted call exercises RBAC, not
    validation. Mutating tools need a fresh idempotency key per call; the
    ticket title is unique so no case replays another's row; classify names
    the caller as target with a real seeded role (both tenants seed
    wiki.viewer), so a permitted call actually succeeds."""
    if tool_name in {"reset_own_password", "unlock_own_account"}:
        return {"idempotency_key": f"fake-key-{uuid.uuid4().hex}"}
    if tool_name == "create_ticket":
        return {
            "kind": "general",
            "title": f"fake matrix ticket {uuid.uuid4().hex}",
            "body": "fake body",
            "idempotency_key": f"fake-key-{uuid.uuid4().hex}",
        }
    if tool_name in {"classify_access_request", "create_access_request"}:
        rows = _tenant_rows(env.tenant_a.tenant_slug)
        assert rows.tenant_id == env.tenant_a.tenant_id
        arguments = {
            "target_user_id": str(env.tenant_a.user_id),
            "role_id": str(rows.role_ids[("wiki", "viewer")]),
        }
        if tool_name == "create_access_request":
            # The caller names THEMSELVES, so this row exercises the design's
            # self path only; the on-behalf permission is never reached.
            arguments["justification"] = f"fake matrix request {uuid.uuid4().hex}"
            arguments["idempotency_key"] = f"fake-key-{uuid.uuid4().hex}"
        return arguments
    if tool_name in ALLOW_ANSWERS_NOT_FOUND:
        # Valid arguments naming a row that does not exist: a permitted
        # caller must get past RBAC and be refused NOT_FOUND by the lookup,
        # never FORBIDDEN by the matrix.
        arguments = {"idempotency_key": f"fake-key-{uuid.uuid4().hex}"}
        if tool_name == "revoke_access":
            arguments["grant_id"] = str(uuid.uuid4())
            arguments["reason"] = f"fake matrix revocation {uuid.uuid4().hex}"
            return arguments
        arguments["request_id"] = str(uuid.uuid4())
        if tool_name == "reject_request":
            arguments["reason"] = f"fake matrix rejection {uuid.uuid4().hex}"
        return arguments
    return {}


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
    """A tool registered without a row here would go untested rather than fail.

    The same exhaustiveness now covers the cross-tenant axis (an earlier change
    decision G): a new tool must be classified as id-taking or not, or the
    sweep below would quietly skip it and the isolation claim would be about
    fewer tools than the registry holds.
    """
    assert set(REGISTRY) == set(MATRIX)
    assert set(REGISTRY) == ID_TAKING_TOOLS | NO_ID_TOOLS
    assert frozenset() == ID_TAKING_TOOLS & NO_ID_TOOLS
    assert set(IDENTITY_ONLY_TOOLS) <= NO_ID_TOOLS


@pytest.mark.parametrize(("tool_name", "principal"), CASES, ids=[f"{t}-{p}" for t, p in CASES])
def test_matrix_expectation_holds_through_the_transport(
    mcp_env: SeededMcp,  # noqa: F811
    tool_name: str,
    principal: str,
) -> None:
    """A token carrying ONLY this principal: refused FORBIDDEN exactly when
    the matrix denies, and not refused when it allows."""
    token = token_for(mcp_env.tenant_a, principals=(principal,))

    result = call_tool(bearer(token), tool_name, _arguments_for(tool_name, mcp_env))

    if MATRIX[tool_name][principal] and tool_name in ALLOW_ANSWERS_NOT_FOUND:
        assert error_code(result) == "NOT_FOUND", (
            f"{principal} is permitted to call {tool_name} by SPEC §7, so the refusal "
            f"must come from the lookup, not the matrix: {result.content!r}"
        )
    elif MATRIX[tool_name][principal]:
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
    """The refusal direction, through the transport. Every registered tool
    allows every real principal, so this is where FORBIDDEN is actually
    reachable — without it the matrix test would assert only that nothing is
    ever refused. No arguments on purpose: RBAC answers before validation."""
    token = token_for(mcp_env.tenant_a, principals=principals)

    result = call_tool(bearer(token), tool_name, {})

    assert error_code(result) == "FORBIDDEN", label


# --- the cross-tenant axis (the design, an earlier change decision G) ----------------------


def _write(database_url: str, statement: Any) -> None:
    """One committed statement written around the tools."""

    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as conn:
                await conn.execute(statement)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def _fetch(database_url: str, statement: Any) -> list[Any]:
    """Every row one SELECT answers, read around the tools."""

    async def scenario() -> list[Any]:
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as conn:
                return list(await conn.execute(statement))
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


@cache
def _pending_request(
    database_url: str, tenant_id: uuid.UUID, user_id: uuid.UUID, role_id: uuid.UUID
) -> uuid.UUID:
    """One pending request in the named tenant, planted once for the sweep.

    a project rule: the namespace is this row, not a rebuilt database. A
    random uuid would test nothing here — every tool answers NOT_FOUND for one
    whether the tenant guard exists or not, so the id the sweep sends has to
    be a real row in the other tenant.
    """
    request_id = uuid.uuid4()
    _write(
        database_url,
        insert(AccessRequest).values(
            id=request_id,
            tenant_id=tenant_id,
            requester_user_id=user_id,
            target_user_id=user_id,
            role_id=role_id,
            justification=f"fake cross-tenant fixture request {uuid.uuid4().hex}",
            requested_expiry_days=None,
            risk_class="critical",
            decision="approval",
            decision_reasons=[],
            citations=[],
            status=RequestStatus.PENDING.value,
            approver_user_ids=[],
            approvals=[],
            session_id=None,
            trace_id=None,
            idempotency_key=None,
        ),
    )
    return request_id


@cache
def _live_grant_id(database_url: str, tenant_id: uuid.UUID) -> uuid.UUID:
    """A grant the seed already wrote in the named tenant."""
    rows = _fetch(
        database_url,
        select(Grant.id)
        .where(Grant.tenant_id == tenant_id, Grant.status == "active")
        .order_by(Grant.id)
        .limit(1),
    )
    assert rows, "the seed must leave at least one live grant in this tenant"
    return uuid.UUID(str(rows[0][0]))


def _tenant_b_role_id(env: SeededMcp) -> uuid.UUID:
    rows = _tenant_rows(env.tenant_b.tenant_slug)
    assert rows.tenant_id == env.tenant_b.tenant_id
    return rows.role_ids[("wiki", "viewer")]


def _cross_tenant_arguments(tool_name: str, env: SeededMcp) -> dict[str, Any]:
    """Arguments naming tenant B's rows, for a token minted in tenant A."""
    role_id = _tenant_b_role_id(env)
    if tool_name in {"classify_access_request", "create_access_request"}:
        arguments = {"target_user_id": str(env.tenant_b.user_id), "role_id": str(role_id)}
        if tool_name == "create_access_request":
            arguments["justification"] = f"fake cross-tenant request {uuid.uuid4().hex}"
            arguments["idempotency_key"] = f"fake-key-{uuid.uuid4().hex}"
        return arguments
    arguments = {"idempotency_key": f"fake-key-{uuid.uuid4().hex}"}
    if tool_name == "revoke_access":
        arguments["grant_id"] = str(_live_grant_id(env.database_url, env.tenant_b.tenant_id))
        arguments["reason"] = f"fake cross-tenant revocation {uuid.uuid4().hex}"
        return arguments
    arguments["request_id"] = str(
        _pending_request(env.database_url, env.tenant_b.tenant_id, env.tenant_b.user_id, role_id)
    )
    if tool_name == "reject_request":
        arguments["reason"] = f"fake cross-tenant rejection {uuid.uuid4().hex}"
    return arguments


CROSS_TENANT_CASES = [
    (tool_name, principal) for tool_name in sorted(ID_TAKING_TOOLS) for principal in PRINCIPALS
]


@pytest.mark.parametrize(
    ("tool_name", "principal"),
    CROSS_TENANT_CASES,
    ids=[f"{t}-{p}" for t, p in CROSS_TENANT_CASES],
)
def test_cross_tenant_arguments_are_refused_for_every_tool_and_principal(
    mcp_env: SeededMcp,  # noqa: F811
    tool_name: str,
    principal: str,
) -> None:
    """the design's isolation row, owed by an earlier change decision B: a tenant-A token
    carrying tenant-B arguments answers with a refusal and never with data,
    for every tool that takes an id and every one of the seven principals.

    The ids are REAL rows in tenant B, not random uuids, so the refusal has to
    come from the tenant guard rather than from an id nobody could resolve
    anyway. `platform_admin` is the case that matters most: the widest
    principal the design has is still confined to its own tenant, and a handler
    reading `Scope.ANY` as "any tenant" passes every other case in this file.

    Either code is accepted because the two refusals are deliberately
    indistinguishable at the seam — an id in another tenant must read exactly
    like an id that does not exist, or the argument becomes an existence
    oracle. What is not accepted is a call that succeeds.
    """
    token = token_for(mcp_env.tenant_a, principals=(principal,))

    result = call_tool(bearer(token), tool_name, _cross_tenant_arguments(tool_name, mcp_env))

    assert result.is_error is True, (
        f"{principal} called {tool_name} with tenant-B arguments from a tenant-A token "
        f"and got data back: {result.content!r}"
    )
    assert error_code(result) in {"FORBIDDEN", "NOT_FOUND"}, result.content


@pytest.mark.parametrize("tool_name", IDENTITY_ONLY_TOOLS)
def test_an_identity_only_tool_answers_about_the_callers_own_tenant(
    mcp_env: SeededMcp,  # noqa: F811
    tool_name: str,
) -> None:
    """Decision G's other half: the five tools whose only input is the token.

    There is no cross-tenant call to make here, so the claim under test is
    that the answer is about the CALLER's own tenant. Each case names a
    tenant-B row of the same kind and asserts it is absent from (or untouched
    by) the answer, and each answer is proven non-empty first — a read that
    returned nothing at all would satisfy an absence assertion while telling
    us nothing. Catches a handler resolving its subject from anything but the
    bound tenant plus the token's `sub`.
    """
    caller = mcp_env.tenant_a
    other = mcp_env.tenant_b
    other_request_id = _pending_request(
        mcp_env.database_url, other.tenant_id, other.user_id, _tenant_b_role_id(mcp_env)
    )
    own_request_id = _pending_request(
        mcp_env.database_url,
        caller.tenant_id,
        caller.user_id,
        _tenant_rows(caller.tenant_slug).role_ids[("wiki", "viewer")],
    )
    other_grant_id = _live_grant_id(mcp_env.database_url, other.tenant_id)
    before = _stamp(mcp_env.database_url, tool_name, other.user_id)
    arguments: dict[str, Any] = (
        {"idempotency_key": f"fake-key-{uuid.uuid4().hex}"}
        if tool_name in {"reset_own_password", "unlock_own_account"}
        else {}
    )

    result = call_tool(bearer(token_for(caller)), tool_name, arguments)

    assert result.is_error is False, result.content
    payload = result.structured_content
    assert payload is not None
    if tool_name == "get_me":
        assert payload["tenant_id"] == str(caller.tenant_id)
        assert payload["user_id"] == str(caller.user_id)
    elif tool_name == "list_my_access":
        grant_ids = [str(entry["grant_id"]) for entry in payload["grants"]]
        assert grant_ids, "the seeded caller holds grants; an empty answer pins nothing"
        assert str(other_grant_id) not in grant_ids
        assert _tenants_of(mcp_env.database_url, Grant, grant_ids) == {caller.tenant_id}
    elif tool_name == "list_my_requests":
        request_ids = [str(entry["request_id"]) for entry in payload["requests"]]
        assert str(own_request_id) in request_ids, "the caller's own row must be there"
        assert str(other_request_id) not in request_ids
        assert _tenants_of(mcp_env.database_url, AccessRequest, request_ids) == {caller.tenant_id}
    else:
        assert _stamp(mcp_env.database_url, tool_name, caller.user_id) is not None, (
            "the effect must land on the caller's own row"
        )
        assert _stamp(mcp_env.database_url, tool_name, other.user_id) == before, (
            "and on no row in the other tenant"
        )


def _tenants_of(database_url: str, entity: Any, ids: list[str]) -> set[uuid.UUID]:
    """Which tenants the rows behind these ids actually belong to."""
    rows = _fetch(
        database_url,
        select(entity.tenant_id).where(entity.id.in_([uuid.UUID(value) for value in ids])),
    )
    return {uuid.UUID(str(row[0])) for row in rows}


def _stamp(database_url: str, tool_name: str, user_id: uuid.UUID) -> Any:
    """The `users` timestamp the tool under test writes, for one user."""
    column = (
        User.password_reset_requested_at
        if tool_name == "reset_own_password"
        else User.account_unlocked_at
    )
    rows = _fetch(database_url, select(column).where(User.id == user_id))
    assert len(rows) == 1
    return rows[0][0]
