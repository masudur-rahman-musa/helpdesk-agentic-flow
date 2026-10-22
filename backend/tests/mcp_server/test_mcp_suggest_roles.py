"""The `suggest_roles` MCP tool, driven over the real transport.

Surface under test (written before it exists; the implementer must provide
exactly this):

``mcp_server.tools.role_suggest`` (new)
    ``SuggestRolesInput`` - ``query: str``; extra arguments are IGNORED.
    ``SuggestedRole`` - exactly ``system_slug``, ``system_name``, ``role_slug``,
    ``role_name``, ``score``. ``SuggestRolesOutput`` - ``roles: list[...]``.
    ``suggest_roles(ctx, args)`` reads the roles and systems of the TOKEN's
    tenant through the tenant-scoped repository and embeds with
    ``ctx.clients.embedder()``.

``mcp_server.tools.registry.REGISTRY["suggest_roles"]``
    ``tool=rbac.Tool.SUGGEST_ROLES``, ``mutating=False``. Every principal,
    ``Scope.ANY``.

The embedder is a fake substituted the way ``test_mcp_search_policy.py`` does
it, through the ``rag_clients.embedder`` module attribute, so no case pays an
ONNX load. The fake gives a vector that depends on the TEXT alone (every vector
sits inside a narrow arc, so every pair scores above the floor, and equal texts
score exactly 1.0). The tool keeps a per-process cache keyed by text; a vector
that depends on nothing but the text keeps that cache from leaking one case's
answer into another.
"""

import math
import uuid
import zlib
from collections.abc import Iterator, Sequence
from typing import Any

import pytest

from helpdesk.mcp_server.core import rag_clients
from helpdesk.mcp_server.services.rag.embedding import SparseVector
from helpdesk.mcp_server.services.rag.role_suggestions import TextVectorCache
from helpdesk.mcp_server.tools import role_suggest
from helpdesk.mcp_server.tools.registry import REGISTRY
from helpdesk.shared.auth import rbac
from scripts.seed import CUAN, NORDLICHT, TenantSpec
from tests.support.mcp import (
    SeededMcp,
    bearer,
    call_tool,
    error_code,
    list_tools,
    sql_execute,
    sql_rows,
    token_with_claims,
)

TOOL = "suggest_roles"
PRINCIPALS = tuple(principal.value for principal in rbac.Principal)
ROLE_FIELDS = {"system_slug", "system_name", "role_slug", "role_name", "score"}
_SPECS: dict[str, TenantSpec] = {CUAN.slug: CUAN, NORDLICHT.slug: NORDLICHT}


class TextOnlyEmbedder:
    """A dense vector that depends on the text alone and costs nothing.

    The angle is a hash of the text, inside half a radian, so any two texts
    score at least cos(0.5) = 0.877 (above the 0.70 floor) and equal texts
    score exactly 1.0. The sparse model and the token counter are not part of
    this tool, so they fail loudly.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed_dense(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        self.calls.append(list(texts))
        out: list[tuple[float, ...]] = []
        for text in texts:
            angle = (zlib.crc32(text.encode()) % 1000) / 1000.0 * 0.5
            out.append((math.cos(angle), math.sin(angle)))
        return out

    def embed_sparse(self, texts: Sequence[str]) -> list[SparseVector]:
        raise AssertionError("suggest_roles never uses the sparse model")

    def count_tokens(self, text: str) -> int:
        raise AssertionError("suggest_roles never counts tokens")


@pytest.fixture
def embedder(monkeypatch: pytest.MonkeyPatch) -> TextOnlyEmbedder:
    """The fake, installed where the handler reads the process embedder."""
    model = TextOnlyEmbedder()
    monkeypatch.setattr(rag_clients, "embedder", lambda: model)
    # A fresh cache too: vectors another file's embedder left in it would not
    # match this one's width.
    monkeypatch.setattr(role_suggest, "_CACHE", TextVectorCache())
    return model


def _call(
    user: Any, arguments: dict[str, Any], *, principals: tuple[str, ...] | None = None
) -> Any:
    claims: dict[str, object] = {"sid": str(uuid.uuid4())}
    if principals is not None:
        claims["principals"] = list(principals)
    return call_tool(bearer(token_with_claims(user, claims)), TOOL, arguments)


def _roles(result: Any) -> list[dict[str, Any]]:
    assert result.is_error is False, result.content
    assert result.structured_content is not None
    roles: list[dict[str, Any]] = result.structured_content["roles"]
    return roles


def _pairs(spec: TenantSpec) -> set[tuple[str, str]]:
    return {(role.system, role.slug) for role in spec.roles}


# --- registration -------------------------------------------------------------


def test_the_registry_row_is_the_matrix_tool_and_is_not_mutating() -> None:
    """Catches a row registered under another matrix member (it would borrow
    that tool's principals) and one marked `mutating=True` (a read would then
    demand an idempotency key)."""
    spec = REGISTRY[TOOL]

    assert spec.name == rbac.Tool.SUGGEST_ROLES.value == TOOL
    assert spec.tool is rbac.Tool.SUGGEST_ROLES
    assert spec.mutating is False


def test_suggest_roles_is_advertised_with_one_required_query(mcp_env: SeededMcp) -> None:
    """Catches an input model with another required field or with `query`
    optional, read off the schema a CLIENT sees."""
    result = list_tools(bearer(token_with_claims(mcp_env.tenant_a, {})))
    advertised = {tool.name: tool for tool in result.tools}

    assert TOOL in advertised, "suggest_roles is not on the wire at all"
    schema = advertised[TOOL].input_schema
    assert schema["required"] == ["query"]
    assert set(schema["properties"]) == {"query"}


@pytest.mark.parametrize("principal", PRINCIPALS)
def test_every_principal_may_suggest_roles(
    mcp_env: SeededMcp, embedder: TextOnlyEmbedder, principal: str
) -> None:
    """Catches a matrix row narrower than every principal: the caller holds ONE
    principal, so a single `FORBIDDEN` is an employee who cannot be helped."""
    result = _call(mcp_env.tenant_a, {"query": "fake docs editor"}, principals=(principal,))

    assert result.is_error is False, error_code(result)


# --- the tenant ---------------------------------------------------------------


@pytest.fixture
def foreign_twin(mcp_env: SeededMcp) -> Iterator[tuple[str, str]]:
    """A tenant B system and role with the SAME names as one of tenant A's,
    whose slugs sort BEFORE every other slug. Yields (system name, role name)
    of the shared names. Removed again afterwards, so no other case sees it."""
    a_row = sql_rows(
        mcp_env.database_url,
        "SELECT s.name AS system_name, r.name AS role_name FROM roles r"
        " JOIN systems s ON s.id = r.system_id WHERE r.tenant_id = :t"
        " ORDER BY r.slug, s.slug LIMIT 1",
        {"t": mcp_env.tenant_a.tenant_id},
    )[0]
    system_id, role_id = uuid.uuid4(), uuid.uuid4()
    system_slug, role_slug = f"0-twin-{system_id.hex[:8]}", f"0-twin-{role_id.hex[:8]}"
    sql_execute(
        mcp_env.database_url,
        "INSERT INTO systems (id, tenant_id, slug, name, sensitivity)"
        " VALUES (:id, :t, :slug, :name, 'low')",
        {
            "id": system_id,
            "t": mcp_env.tenant_b.tenant_id,
            "slug": system_slug,
            "name": a_row.system_name,
        },
    )
    sql_execute(
        mcp_env.database_url,
        "INSERT INTO roles (id, tenant_id, system_id, slug, name, privilege, default_expiry_days)"
        " VALUES (:id, :t, :sys, :slug, :name, 'read', 90)",
        {
            "id": role_id,
            "t": mcp_env.tenant_b.tenant_id,
            "sys": system_id,
            "slug": role_slug,
            "name": a_row.role_name,
        },
    )
    try:
        yield str(a_row.system_name), str(a_row.role_name)
    finally:
        sql_execute(mcp_env.database_url, "DELETE FROM roles WHERE id = :id", {"id": role_id})
        sql_execute(mcp_env.database_url, "DELETE FROM systems WHERE id = :id", {"id": system_id})


@pytest.mark.db
def test_suggest_roles_reads_only_the_tokens_tenant(
    mcp_env: SeededMcp, embedder: TextOnlyEmbedder, foreign_twin: tuple[str, str]
) -> None:
    """Catches a role read with no tenant filter: tenant B holds a role whose
    names equal one of tenant A's, so it scores exactly as high, and its slugs
    sort first, so an unfiltered read would put it in A's top three."""
    system_name, role_name = foreign_twin

    roles = _roles(_call(mcp_env.tenant_a, {"query": f"{system_name} {role_name}"}))

    assert roles, "tenant A has roles, so something must be offered"
    assert not [r for r in roles if r["role_slug"].startswith("0-twin-")], roles
    assert (roles[0]["system_name"], roles[0]["role_name"]) == (system_name, role_name)
    assert roles[0]["score"] == pytest.approx(1.0)


@pytest.mark.db
def test_suggest_roles_returns_slugs_and_names(
    mcp_env: SeededMcp, embedder: TextOnlyEmbedder
) -> None:
    """Catches a row missing a field, one carrying an extra (an internal id
    reaching a prompt), and two fields swapped: the best row is compared with
    the stored names, not with another output field."""
    expected = sql_rows(
        mcp_env.database_url,
        "SELECT s.slug AS system_slug, s.name AS system_name,"
        " r.slug AS role_slug, r.name AS role_name FROM roles r"
        " JOIN systems s ON s.id = r.system_id WHERE r.tenant_id = :t"
        " ORDER BY r.slug, s.slug LIMIT 1",
        {"t": mcp_env.tenant_a.tenant_id},
    )[0]

    roles = _roles(
        _call(mcp_env.tenant_a, {"query": f"{expected.system_name} {expected.role_name}"})
    )

    best = roles[0]
    assert set(best) == ROLE_FIELDS
    assert best["system_slug"] == expected.system_slug
    assert best["system_name"] == expected.system_name
    assert best["role_slug"] == expected.role_slug
    assert best["role_name"] == expected.role_name
    assert best["score"] == pytest.approx(1.0)
    assert len(roles) <= 3
    assert [r["score"] for r in roles] == sorted((r["score"] for r in roles), reverse=True)


def test_a_tenant_argument_is_ignored(mcp_env: SeededMcp, embedder: TextOnlyEmbedder) -> None:
    """Catches a tool that honours a `tenant` argument over the token (tenant
    B's roles would come back), and an input model that rejects extras (the
    call must succeed, not turn into `VALIDATION`).

    Same query with and without the extra argument: the answers must be equal,
    non-empty, and made only of tenant A's own roles."""
    caller, other = mcp_env.tenant_a, mcp_env.tenant_b
    own = _pairs(_SPECS[caller.tenant_slug])
    foreign_only = _pairs(_SPECS[other.tenant_slug]) - own
    assert foreign_only, "fixture premise: the other tenant has roles this one lacks"
    query = "fake docs editor"

    plain = _roles(_call(caller, {"query": query}))
    with_extra = _roles(
        _call(caller, {"query": query, "tenant": str(other.tenant_id), "tenant_id": "x"})
    )

    assert plain, "the premise: every text scores above the floor, so roles come back"
    assert with_extra == plain
    assert {(r["system_slug"], r["role_slug"]) for r in with_extra} <= own


# --- the query ----------------------------------------------------------------


@pytest.mark.parametrize("query", ["q" * 201, "", "   "], ids=["over-200", "empty", "blank"])
def test_a_refused_query_reaches_no_embed_and_is_validation(
    mcp_env: SeededMcp, embedder: TextOnlyEmbedder, query: str
) -> None:
    """Catches a handler that embeds the query before checking it: 201
    characters or a blank query must answer `VALIDATION` with the model
    untouched."""
    result = _call(mcp_env.tenant_a, {"query": query})

    assert error_code(result) == "VALIDATION"
    assert embedder.calls == []


def test_a_query_of_exactly_200_characters_is_accepted(
    mcp_env: SeededMcp, embedder: TextOnlyEmbedder
) -> None:
    """Catches an exclusive cap at the tool boundary: 200 characters is legal,
    and the model really was asked (a pass is an accepted query, not an early
    return)."""
    result = _call(mcp_env.tenant_a, {"query": "q" * 200})

    assert result.is_error is False, error_code(result)
    assert any("q" * 200 in call for call in embedder.calls)


# --- the phrases of a system --------------------------------------------------


@pytest.mark.db
def test_a_system_phrase_finds_its_roles(mcp_env: SeededMcp, embedder: TextOnlyEmbedder) -> None:
    """Catches a tool that never reads `systems.about` (the query "team pages"
    equals a phrase of the wiki and no role name, so only the phrase can score
    1.0), a read that takes the phrases of another tenant, and one that leaks
    `about` into the output rows."""
    spec = _SPECS[mcp_env.tenant_a.tenant_slug]
    assert "team pages" in [p.strip() for p in spec.system_about["wiki"].split(",")], (
        "fixture premise: the wiki's phrases hold the query"
    )

    roles = _roles(_call(mcp_env.tenant_a, {"query": "team pages"}))

    assert roles, "the wiki's phrase equals the query, so something must be offered"
    assert roles[0]["system_slug"] == "wiki"
    assert roles[0]["score"] == pytest.approx(1.0)
    assert all(set(role) == ROLE_FIELDS for role in roles), roles
    assert all("about" not in role for role in roles)


@pytest.fixture
def many_early_roles(mcp_env: SeededMcp) -> Iterator[int]:
    """201 roles of one planted system of tenant A, whose system slug and role
    slugs all sort BEFORE every seeded slug (and so before `wiki`). Yields the
    count. Removed again afterwards, so no other case sees them."""
    count = 201
    system_id = uuid.uuid4()
    suffix = system_id.hex[:8]
    sql_execute(
        mcp_env.database_url,
        "INSERT INTO systems (id, tenant_id, slug, name, sensitivity)"
        " VALUES (:id, :t, :slug, 'Fake Bulk System', 'low')",
        {"id": system_id, "t": mcp_env.tenant_a.tenant_id, "slug": f"0-fake-bulk-{suffix}"},
    )
    try:
        for index in range(count):
            sql_execute(
                mcp_env.database_url,
                "INSERT INTO roles (id, tenant_id, system_id, slug, name, privilege,"
                " default_expiry_days) VALUES (:id, :t, :sys, :slug, :name, 'read', 90)",
                {
                    "id": uuid.uuid4(),
                    "t": mcp_env.tenant_a.tenant_id,
                    "sys": system_id,
                    "slug": f"fake-a{index:03d}",
                    "name": f"Fake Bulk Role {index:03d}",
                },
            )
        yield count
    finally:
        sql_execute(
            mcp_env.database_url, "DELETE FROM roles WHERE system_id = :s", {"s": system_id}
        )
        sql_execute(mcp_env.database_url, "DELETE FROM systems WHERE id = :id", {"id": system_id})


@pytest.mark.db
def test_a_role_past_the_first_two_hundred_is_still_offered(
    mcp_env: SeededMcp, embedder: TextOnlyEmbedder, many_early_roles: int
) -> None:
    """Catches a tool that scores only the first 200 roles (by system slug, then
    role slug) of the tenant: 201 planted roles sort ahead of every `wiki` role,
    so a capped read never sees the wiki, whose phrase equals the query and
    which must come first.
    """
    assert many_early_roles > 200, "premise: more planted roles than the old cap"

    roles = _roles(_call(mcp_env.tenant_a, {"query": "team pages"}))

    assert roles, "something must be offered"
    assert roles[0]["system_slug"] == "wiki"
    assert roles[0]["score"] == pytest.approx(1.0)
