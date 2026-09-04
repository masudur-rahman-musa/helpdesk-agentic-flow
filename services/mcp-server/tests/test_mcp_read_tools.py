"""An earlier change, section 1 criterion 3, section 6: `get_me` and `list_my_access`.

Surface under test (written before it exists):

``mcp_server.tools.read``
    ``GetMeInput`` / ``ListMyAccessInput`` — no fields; extra arguments are
    IGNORED, never rejected. Identity comes only from the JWT,
    so a ``user_id`` in the arguments is data to drop, not a reason to fail
    and not a thing to honour.
    ``GetMeOutput`` — ``user_id``, ``tenant_id``, ``email``, ``display_name``,
    ``department_id: uuid.UUID | None``, ``principals: list[str]``, ``status``.
    ``ListMyAccessOutput`` — ``grants: list[ActiveGrant]``.

``helpdesk.core.repository``
    ``find_user_by_id(session, user_id) -> User | None``
    ``ActiveGrant(BaseModel, frozen=True)`` — ``grant_id``, ``role_slug``,
    ``role_name``, ``privilege``, ``system_slug``, ``system_name``,
    ``granted_at``, ``expires_at``.
    ``list_active_grants_for_user(session, user_id, now) -> Sequence[ActiveGrant]``
    where active means ``status == "active"`` AND ``revoked_at IS NULL`` AND
    (``expires_at IS NULL`` OR ``expires_at > now``). Three conjuncts, one
    hidden fixture row per conjunct, so dropping any one fails a named case.

A token whose ``sub`` names a user in another tenant reads nothing: the
session is bound to the token's tenant, so the row is not found. That is
``NOT_FOUND``, one of the eight §8.1 codes — a cross-tenant read must not be
reported as an internal error, and must never return the row.

No pytest-asyncio: each case is a sync function driving one ``asyncio.run``.
"""

import asyncio
import uuid
from datetime import timedelta
from typing import Any

from helpdesk.core.db import create_engine, create_session_factory, tenant_session
from helpdesk.core.repository import list_active_grants_for_user
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
    token_with_claims,
)

# One microsecond, the smallest step Postgres stores in a timestamptz: the
# expiry boundary pair straddles exactly this.
ONE_TICK = timedelta(microseconds=1)


def _structured(result: Any) -> dict[str, Any]:
    assert result.is_error is False, result.content
    assert result.structured_content is not None, "a successful call carries structured content"
    content: dict[str, Any] = result.structured_content
    return content


def _grant_ids(payload: dict[str, Any]) -> set[uuid.UUID]:
    return {uuid.UUID(str(grant["grant_id"])) for grant in payload["grants"]}


# --- get_me -------------------------------------------------------------------


def test_get_me_returns_the_token_users_own_record(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    user = mcp_env.tenant_a

    payload = _structured(call_tool(bearer(token_for(user)), "get_me", {}))

    assert payload["user_id"] == str(user.user_id)
    assert payload["tenant_id"] == str(user.tenant_id)
    assert payload["email"] == user.email
    assert payload["display_name"] == user.display_name
    assert payload["department_id"] == str(user.department_id)
    assert sorted(payload["principals"]) == sorted(user.principals)
    assert payload["status"] == user.status


def test_get_me_ignores_user_id_and_tenant_id_in_its_arguments(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """a project rule, with the tenant guard taken out of the picture.

    The user named in the arguments is a real, readable user in the CALLER'S
    OWN tenant, so an implementation that honoured the
    argument — or fell back to it — returns that colleague's record rather
    than being saved by tenant scoping. A cross-tenant version of this test
    passes whether the rule holds or not, because honouring a cross-tenant
    id merely misses and can silently fall back to the token user.
    """
    caller = mcp_env.tenant_a
    colleague = mcp_env.probe
    assert caller.tenant_id == colleague.tenant_id, "the point of this test"
    assert caller.user_id != colleague.user_id

    payload = _structured(
        call_tool(
            bearer(token_for(caller)),
            "get_me",
            {"user_id": str(colleague.user_id), "tenant_id": str(colleague.tenant_id)},
        )
    )

    assert payload["user_id"] == str(caller.user_id)
    assert payload["tenant_id"] == str(caller.tenant_id)
    assert payload["email"] == caller.email
    assert payload["user_id"] != str(colleague.user_id)
    assert payload["email"] != colleague.email


def test_a_tenant_b_token_cannot_read_a_tenant_a_user(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """The token verifies, but its tenant does not own the subject: bound to
    tenant B, the row simply is not there."""
    forged = token_with_claims(mcp_env.tenant_b, {"sub": str(mcp_env.tenant_a.user_id)})

    result = call_tool(bearer(forged), "get_me", {})

    assert error_code(result) == "NOT_FOUND"


def test_each_tenants_token_reads_only_its_own_tenants_row(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    for user in (mcp_env.tenant_a, mcp_env.tenant_b):
        payload = _structured(call_tool(bearer(token_for(user)), "get_me", {}))
        assert payload["tenant_id"] == str(user.tenant_id)
        assert payload["email"] == user.email


# --- list_my_access ------------------------------------------------------------


def test_list_my_access_returns_exactly_the_active_grants(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    payload = _structured(call_tool(bearer(token_for(mcp_env.probe)), "list_my_access", {}))

    assert _grant_ids(payload) == {grant.grant_id for grant in mcp_env.visible_grants}
    by_id = {uuid.UUID(str(grant["grant_id"])): grant for grant in payload["grants"]}
    for expected in mcp_env.visible_grants:
        row = by_id[expected.grant_id]
        assert row["role_slug"] == expected.role_slug
        assert row["role_name"] == expected.role_name
        assert row["privilege"] == expected.privilege
        assert row["system_slug"] == expected.system_slug
        assert row["system_name"] == expected.system_name


def test_list_my_access_omits_revoked_expired_and_non_active_grants(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """One hidden row per conjunct of "active": a past `expires_at`, a set
    `revoked_at`, and a status that is not `active`."""
    payload = _structured(call_tool(bearer(token_for(mcp_env.probe)), "list_my_access", {}))

    visible = _grant_ids(payload)
    for hidden in mcp_env.hidden_grant_ids:
        assert hidden not in visible, hidden
    assert len(mcp_env.hidden_grant_ids) == 3


def test_list_my_access_ignores_a_user_id_argument(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """a project rule, with the tenant guard taken out of the picture.

    The caller and the user named in the arguments are in the SAME tenant, so
    an implementation that honoured `user_id` would return the probe's rows
    rather than be saved by tenant scoping. A cross-tenant version of this
    test passes whether the rule holds or not.
    """
    caller = mcp_env.tenant_a
    assert caller.tenant_id == mcp_env.probe.tenant_id, "the point of this test"

    payload = _structured(
        call_tool(
            bearer(token_for(caller)),
            "list_my_access",
            {"user_id": str(mcp_env.probe.user_id)},
        )
    )

    assert _grant_ids(payload).isdisjoint({g.grant_id for g in mcp_env.visible_grants}), (
        "the probe's grants came back for a caller who only named the probe"
    )


def test_a_tenant_b_token_sees_none_of_tenant_as_grants(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    payload = _structured(call_tool(bearer(token_for(mcp_env.tenant_b)), "list_my_access", {}))

    assert _grant_ids(payload).isdisjoint({g.grant_id for g in mcp_env.visible_grants})
    assert _grant_ids(payload).isdisjoint(set(mcp_env.hidden_grant_ids))


# --- the repository helper's expiry boundary ----------------------------------


def _active_grant_ids(
    database_url: str, tenant_id: uuid.UUID, user_id: uuid.UUID, now: Any
) -> set[uuid.UUID]:
    async def scenario() -> set[uuid.UUID]:
        engine = create_engine(database_url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, tenant_id) as session:
                grants = await list_active_grants_for_user(session, user_id, now)
                return {grant.grant_id for grant in grants}
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def test_the_expiry_boundary_is_exclusive_at_expires_at(
    seeded_mcp_db: SeededMcp,  # noqa: F811
) -> None:
    """`expires_at` names the first instant the grant is gone.

    The pass/fail pair straddles it by one microsecond, the smallest step the
    column stores: a one-sided assertion cannot tell `>` from `>=`, and the
    difference is a revoked-by-time grant still reading as live.
    """
    probe = seeded_mcp_db.probe
    boundary = seeded_mcp_db.boundary_expires_at

    still_live = _active_grant_ids(
        seeded_mcp_db.database_url, probe.tenant_id, probe.user_id, boundary - ONE_TICK
    )
    gone = _active_grant_ids(seeded_mcp_db.database_url, probe.tenant_id, probe.user_id, boundary)

    assert seeded_mcp_db.boundary_grant_id in still_live
    assert seeded_mcp_db.boundary_grant_id not in gone


def test_active_grants_come_back_in_system_then_role_order(
    seeded_mcp_db: SeededMcp,  # noqa: F811
) -> None:
    """The order the helper's docstring claims, asserted as a sequence.

    Set comparisons cannot see an order, so without this the docstring could
    say anything. Alphabetical by system slug, then by role slug.
    """
    probe = seeded_mcp_db.probe

    async def scenario() -> list[tuple[str, str]]:
        engine = create_engine(seeded_mcp_db.database_url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, probe.tenant_id) as session:
                grants = await list_active_grants_for_user(
                    session, probe.user_id, seeded_mcp_db.boundary_expires_at - ONE_TICK
                )
                return [(grant.system_slug, grant.role_slug) for grant in grants]
        finally:
            await engine.dispose()

    pairs = asyncio.run(scenario())

    expected = sorted(
        (grant.system_slug, grant.role_slug) for grant in seeded_mcp_db.visible_grants
    )
    assert pairs == expected, pairs
    # The fixture inserts these three rows in the reverse of this order, so
    # the assertion above fails if the ORDER BY is dropped rather than
    # passing on insertion order (asserted in the fixture itself).
    assert pairs != [(grant.system_slug, grant.role_slug) for grant in seeded_mcp_db.visible_grants]


def test_the_repository_helper_agrees_with_the_tool(
    seeded_mcp_db: SeededMcp,  # noqa: F811
) -> None:
    """Read through the helper directly, before any transport is involved:
    the same three rows, so a later disagreement names which side moved."""
    probe = seeded_mcp_db.probe
    now = seeded_mcp_db.boundary_expires_at - ONE_TICK

    active = _active_grant_ids(seeded_mcp_db.database_url, probe.tenant_id, probe.user_id, now)

    assert active == {grant.grant_id for grant in seeded_mcp_db.visible_grants}
