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
import itertools
import uuid
from functools import cache
from typing import Any

import pytest
from helpdesk.auth.rbac import Principal
from helpdesk.models import (
    AccessRequest,
    AccessReview,
    Grant,
    RequestStatus,
    ReviewFlag,
    ReviewItem,
    ReviewStatus,
    User,
)
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
    # gives the approval rows five cells and leaves `employee` and `auditor`
    # without one. `hr` HOLDS a cell here since an earlier change step 5:
    # the design puts an `hr` seat on a `critical` HRIS chain, and without the
    # cell that seat was fillable by nobody. `auditor` is the cell that
    # matters now — it holds one on the audit row and none here.
    "list_pending_approvals": {
        "employee": False,
        "manager": True,
        "system_owner": True,
        "hr": True,
        "it_admin": True,
        "auditor": False,
        "platform_admin": True,
    },
    "approve_request": {
        "employee": False,
        "manager": True,
        "system_owner": True,
        "hr": True,
        "it_admin": True,
        "auditor": False,
        "platform_admin": True,
    },
    "reject_request": {
        "employee": False,
        "manager": True,
        "system_owner": True,
        "hr": True,
        "it_admin": True,
        "auditor": False,
        "platform_admin": True,
    },
    # An earlier change. the design's `onboard_user` row: three cells, and the narrowest
    # of the lifecycle rows — `manager` and `system_owner` hold nothing here
    # even though the revoke row above gives them both a cell.
    "onboard_user": {
        "employee": False,
        "manager": False,
        "system_owner": False,
        "hr": True,
        "it_admin": True,
        "auditor": False,
        "platform_admin": True,
    },
    # An earlier change. the design's `transfer_user` row: the same three cells as
    # `onboard_user`, and all three at `Scope.ANY`, so there is no
    # argument-dependent second check to blunt the denial direction.
    "transfer_user": {
        "employee": False,
        "manager": False,
        "system_owner": False,
        "hr": True,
        "it_admin": True,
        "auditor": False,
        "platform_admin": True,
    },
    # An earlier change. the design's `offboard_user` row: TWO cells, the narrowest
    # lifecycle row of the three. `it_admin` holds a cell on both rows above
    # and NONE here, so a row copied from `transfer_user` by habit fails.
    "offboard_user": {
        "employee": False,
        "manager": False,
        "system_owner": False,
        "hr": True,
        "it_admin": False,
        "auditor": False,
        "platform_admin": True,
    },
    # An earlier change. the design's two review rows, identical to each other and to the
    # revoke row: five cells, `hr` holds one, `employee` and `auditor` hold
    # none. `auditor` is the cell that matters — it holds one on the audit row
    # and none here, so a row copied from `search_audit` by habit fails.
    "open_access_review": {
        "employee": False,
        "manager": True,
        "system_owner": True,
        "hr": True,
        "it_admin": True,
        "auditor": False,
        "platform_admin": True,
    },
    "list_review_items": {
        "employee": False,
        "manager": True,
        "system_owner": True,
        "hr": True,
        "it_admin": True,
        "auditor": False,
        "platform_admin": True,
    },
    # An earlier change. the design's `decide_review_item` row, identical to the two
    # review rows above: the tool that turns a flag into a revocation is
    # reachable by exactly the principals who may open the review and read
    # its items, and by no one else.
    "decide_review_item": {
        "employee": False,
        "manager": True,
        "system_owner": True,
        "hr": True,
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
# `onboard_user` joins them for a different reason: a permitted
# call with a REAL department would hire a person per (tool, principal) cell
# into the shared seeded database. A department id that resolves nowhere is
# refused NOT_FOUND by the handler's own lookup, which is the same assertion —
# the matrix let the caller through and a later stage refused them.
# `transfer_user` joins for the same reason as `onboard_user`: a
# permitted call naming a REAL person would move somebody per (tool, principal)
# cell inside the shared seeded database, so it names a user id that resolves
# nowhere and is refused NOT_FOUND by the handler's own lookup.
# `offboard_user` joins for the same reason as `transfer_user`: a
# permitted call naming a REAL person would strip somebody's access per
# (tool, principal) cell inside the shared seeded database, so it names a user
# id that resolves nowhere and is refused NOT_FOUND by the domain's own lookup.
ALLOW_ANSWERS_NOT_FOUND = frozenset(
    {
        "approve_request",
        "reject_request",
        "revoke_access",
        "onboard_user",
        "transfer_user",
        "offboard_user",
    }
)

# An earlier change. A third answer this sweep has to allow for, and the reason it
# exists: both review tools check the design's SCOPE before the id's existence
#, the opposite order from `revoke_access`. So a
# permitted `manager` or `system_owner` sending an id they cannot reach is
# refused FORBIDDEN by the scope gate, not NOT_FOUND by a lookup, and this
# sweep cannot separate that from a matrix denial for those two cells. It
# therefore asserts only that a permitted principal never gets DATA back from
# an unreachable id; the matrix cell itself is pinned per principal, in both
# directions, by `test_mcp_reviews.py::
# test_each_permitted_principal_opens_a_review_in_their_scope` and
# `::test_the_two_denied_principals_open_nothing`, and the scope gate's own
# kill is `::test_a_manager_may_not_open_a_review_for_another_department`.
# An earlier change adds the third: `decide_review_item` answers the scope question
# from the item's own review BEFORE the item's existence is tested (decision
# H), so it belongs here and NOT in `ALLOW_ANSWERS_NOT_FOUND` (standing rule
# 25) — a permitted-but-out-of-scope caller and a matrix denial would both
# read FORBIDDEN and that cell would pin nothing. Its matrix cell is pinned
# per principal by `test_mcp_reviews.py::
# test_each_permitted_principal_decides_an_item_in_their_scope` and
# `::test_the_two_denied_principals_decide_nothing`, and the ORDER of the two
# stages by `::test_an_out_of_scope_item_is_refused_before_its_existence_is_tested`.
SCOPE_BEFORE_EXISTENCE_TOOLS = frozenset(
    {"open_access_review", "list_review_items", "decide_review_item"}
)

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
        "onboard_user",
        "transfer_user",
        "offboard_user",
        # An earlier change: one takes a department or system id, the other a review
        # id, so both have a cross-tenant call to make.
        "open_access_review",
        "list_review_items",
        # An earlier change: it takes a review ITEM id, so it has one too.
        "decide_review_item",
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


# A period no other call in this sweep uses: decision H's shape admits four
# quarters, so the YEAR is what varies.
_MATRIX_PERIOD_YEARS = itertools.count(2201)


def _matrix_period() -> str:
    return f"{next(_MATRIX_PERIOD_YEARS)}-Q1"


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
    if tool_name == "onboard_user":
        # A department id that resolves nowhere, so a permitted principal is
        # refused NOT_FOUND rather than hiring somebody per matrix cell.
        return {
            "email": f"fake.matrix.joiner.{uuid.uuid4().hex}@fake.invalid",
            "display_name": "Fake Matrix Joiner",
            "department_id": str(uuid.uuid4()),
            "start_date": "2026-09-01",
            "manager_user_id": str(env.tenant_a.user_id),
            "idempotency_key": f"fake-key-{uuid.uuid4().hex}",
        }
    if tool_name == "transfer_user":
        # A user id that resolves nowhere, so a permitted principal is refused
        # NOT_FOUND rather than moving somebody per matrix cell.
        return {
            "user_id": str(uuid.uuid4()),
            "new_department_id": str(uuid.uuid4()),
            "effective_date": "2026-09-15",
            "idempotency_key": f"fake-key-{uuid.uuid4().hex}",
        }
    if tool_name == "offboard_user":
        # A user id that resolves nowhere, so a permitted principal is refused
        # NOT_FOUND rather than offboarding somebody per matrix cell.
        return {
            "user_id": str(uuid.uuid4()),
            "end_date": "2026-10-31",
            "idempotency_key": f"fake-key-{uuid.uuid4().hex}",
        }
    if tool_name == "open_access_review":
        # A department id that resolves nowhere, so no principal opens a real
        # review per matrix cell in the shared seeded database. A fresh period
        # per call as well, so nothing can be refused as a duplicate instead.
        return {
            "department_id": str(uuid.uuid4()),
            "period": _matrix_period(),
            "idempotency_key": f"fake-key-{uuid.uuid4().hex}",
        }
    if tool_name == "list_review_items":
        return {"review_id": str(uuid.uuid4())}
    if tool_name == "decide_review_item":
        # An item id that resolves nowhere, so no principal ends anybody's
        # access per matrix cell in the shared seeded database. A scoped
        # caller is refused FORBIDDEN by the scope gate and an `any` caller
        # NOT_FOUND by the lookup behind it, which is why this tool is in
        # SCOPE_BEFORE_EXISTENCE_TOOLS rather than ALLOW_ANSWERS_NOT_FOUND.
        return {
            "item_id": str(uuid.uuid4()),
            "decision": "keep",
            "idempotency_key": f"fake-key-{uuid.uuid4().hex}",
        }
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

    if MATRIX[tool_name][principal] and tool_name in SCOPE_BEFORE_EXISTENCE_TOOLS:
        assert result.is_error is True, (
            f"{principal} sent {tool_name} an id nothing in the tenant resolves and got "
            f"data back: {result.content!r}"
        )
        assert error_code(result) in {"FORBIDDEN", "NOT_FOUND"}, result.content
    elif MATRIX[tool_name][principal] and tool_name in ALLOW_ANSWERS_NOT_FOUND:
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
def _foreign_review(env: SeededMcp) -> uuid.UUID:
    """One real `access_reviews` row in the OTHER tenant, planted once.

    A random uuid would test nothing here: every tool answers NOT_FOUND for
    one whether the tenant guard exists or not, so the id the sweep sends has
    to be a row that really exists somewhere else.
    """
    review_id = uuid.uuid4()
    _write(
        env.database_url,
        insert(AccessReview).values(
            id=review_id,
            tenant_id=env.tenant_b.tenant_id,
            reviewer_user_id=env.tenant_b.user_id,
            department_id=env.tenant_b.department_id,
            system_id=None,
            period="2026-Q1",
            status=ReviewStatus.OPEN.value,
            due_at=None,
            completed_at=None,
        ),
    )
    return review_id


@cache
def _foreign_review_item(env: SeededMcp) -> uuid.UUID:
    """One real `review_items` row in the OTHER tenant, planted once.

    Same reason as `_foreign_review`: a random uuid answers NOT_FOUND whether
    the tenant guard exists or not, so the id the sweep sends has to point at
    a row that really exists somewhere else — and at a real grant there, so a
    decision that escaped the guard would end genuine access.
    """
    item_id = uuid.uuid4()
    _write(
        env.database_url,
        insert(ReviewItem).values(
            id=item_id,
            tenant_id=env.tenant_b.tenant_id,
            review_id=_foreign_review(env),
            grant_id=_live_grant_id(env.database_url, env.tenant_b.tenant_id),
            flag=ReviewFlag.OK.value,
            decision=None,
            decided_at=None,
        ),
    )
    return item_id


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
    if tool_name == "onboard_user":
        # Tenant B's department AND tenant B's manager, both real rows there:
        # the refusal has to come from the tenant guard, not from ids nobody
        # could resolve anyway.
        return {
            "email": f"fake.cross.joiner.{uuid.uuid4().hex}@fake.invalid",
            "display_name": "Fake Cross Joiner",
            # "Engineering" is seeded in BOTH tenants, so this resolves in
            # tenant B whichever of the two the fixture made tenant B.
            "department_id": str(
                _tenant_rows(env.tenant_b.tenant_slug).department_ids["Engineering"]
            ),
            "start_date": "2026-09-01",
            "manager_user_id": str(env.tenant_b.user_id),
            "idempotency_key": f"fake-key-{uuid.uuid4().hex}",
        }
    if tool_name == "transfer_user":
        # Tenant B's person AND tenant B's department, both real rows there:
        # the refusal has to come from the tenant guard, not from ids nobody
        # could resolve anyway.
        return {
            "user_id": str(env.tenant_b.user_id),
            # "Engineering" is seeded in BOTH tenants, so this resolves in
            # tenant B whichever of the two the fixture made tenant B.
            "new_department_id": str(
                _tenant_rows(env.tenant_b.tenant_slug).department_ids["Engineering"]
            ),
            "effective_date": "2026-09-15",
            "idempotency_key": f"fake-key-{uuid.uuid4().hex}",
        }
    if tool_name == "offboard_user":
        # Tenant B's person, a real row there: the refusal has to come from the
        # tenant guard, not from an id nobody could resolve anyway.
        return {
            "user_id": str(env.tenant_b.user_id),
            "end_date": "2026-10-31",
            "idempotency_key": f"fake-key-{uuid.uuid4().hex}",
        }
    if tool_name == "open_access_review":
        # Tenant B's department, a real row there: the refusal has to come from
        # the tenant guard (or the scope gate above it), not from an id nobody
        # could resolve anyway.
        return {
            "department_id": str(
                _tenant_rows(env.tenant_b.tenant_slug).department_ids["Engineering"]
            ),
            "period": _matrix_period(),
            "idempotency_key": f"fake-key-{uuid.uuid4().hex}",
        }
    if tool_name == "list_review_items":
        return {"review_id": str(_foreign_review(env))}
    if tool_name == "decide_review_item":
        return {
            "item_id": str(_foreign_review_item(env)),
            "decision": "keep",
            "idempotency_key": f"fake-key-{uuid.uuid4().hex}",
        }
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
