"""`read_policies`, through the transport.

Every signed-in person may read their company's policy documents in full:
the list of current documents, and one document's text, stitched back from
its stored chunks. The text is the same redacted text the agent cites.

Each case runs in its own copy of the clean seeded database (`mcp_pg.clone`),
so the tenants and documents planted here never reach a database another file
counts rows in. Rows are planted as raw SQL through the shared probe engine,
so a case can choose the chunk offsets, the heading paths and which version is
current. Postgres and Redis are the session's; no model and no vector store is
touched, since this tool reads Postgres only.

No pytest-asyncio: each case is a sync function driving one `asyncio.run`.
"""

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import pytest

from tests.support.mcp import (
    TEST_JWT_SIGNING_PHRASE,
    TEST_QDRANT_URL,
    SeededMcp,
    SeededUser,
    TempDatabaseFactory,
    bearer,
    call_tool,
    error_code,
    sql_execute,
    token_for,
)

pytestmark = pytest.mark.db

READ_POLICIES = "read_policies"

INGESTED_AT = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)

SOURCE = (
    "# Fake Access Policy\n"
    "\n"
    "## 1. Purpose\n"
    "\n"
    "Fake sentence one. Fake sentence two. Fake sentence three.\n"
    "\n"
    "## 2. Scope\n"
    "\n"
    "- Fake item one\n"
    "- Fake item two\n"
)
"""One redacted document, as the chunker would see it. Offsets below are
read off this string, so they point at the right words."""


@dataclass(frozen=True)
class World:
    """One cloned database and the tenants planted in it."""

    database_url: str
    real: SeededUser
    other: SeededUser


def _plant_tenant(database_url: str, *, slug: str) -> SeededUser:
    """A tenant with a department and one employee."""
    tenant_id, department_id, user_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    email = f"fake.{uuid.uuid4().hex[:10]}@fake.invalid"
    sql_execute(
        database_url,
        "INSERT INTO tenants (id, slug, name, settings) VALUES (:id, :slug, :name, '{}'::jsonb)",
        {"id": tenant_id, "slug": slug, "name": f"Fake {slug}"},
    )
    sql_execute(
        database_url,
        "INSERT INTO departments (id, tenant_id, name, manager_user_id)"
        " VALUES (:id, :tenant, 'Fake Department', NULL)",
        {"id": department_id, "tenant": tenant_id},
    )
    sql_execute(
        database_url,
        "INSERT INTO users (id, tenant_id, department_id, email, display_name,"
        " password_hash, principals, status, start_date, end_date)"
        " VALUES (:id, :tenant, :department, :email, 'Fake Person', 'not-a-real-hash',"
        " ARRAY['employee']::varchar[], 'active', :start, NULL)",
        {
            "id": user_id,
            "tenant": tenant_id,
            "department": department_id,
            "email": email,
            "start": date(2026, 1, 1),
        },
    )
    return SeededUser(
        tenant_id=tenant_id,
        tenant_slug=slug,
        user_id=user_id,
        email=email,
        display_name="Fake Person",
        department_id=department_id,
        principals=("employee",),
        status="active",
    )


@dataclass(frozen=True)
class PlantedChunk:
    chunk_index: int
    heading_path: tuple[str, ...]
    char_start: int
    char_end: int


def _span(start_text: str, end_text: str) -> tuple[int, int]:
    """The offsets in `SOURCE` from the start of one piece to the end of another."""
    start = SOURCE.index(start_text)
    end = SOURCE.index(end_text) + len(end_text)
    return start, end


def _plant_document(
    world: World,
    tenant: SeededUser,
    *,
    slug: str,
    title: str,
    version: str = "v1",
    is_current: bool = True,
    chunks: tuple[PlantedChunk, ...] = (),
) -> None:
    """One `policy_documents` row and its chunk rows, cut from `SOURCE`."""
    document_id = uuid.uuid4()
    sql_execute(
        world.database_url,
        "INSERT INTO policy_documents"
        " (id, tenant_id, slug, title, version, is_current, source_path, sha256, ingested_at)"
        " VALUES (:id, :tenant, :slug, :title, :version, :is_current, :path, :sha, :at)",
        {
            "id": document_id,
            "tenant": tenant.tenant_id,
            "slug": slug,
            "title": title,
            "version": version,
            "is_current": is_current,
            "path": f"policies/{tenant.tenant_slug}/{version}/{slug}.md",
            "sha": uuid.uuid4().hex * 2,
            "at": INGESTED_AT,
        },
    )
    for chunk in chunks:
        sql_execute(
            world.database_url,
            "INSERT INTO policy_chunks"
            " (id, tenant_id, document_id, chunk_index, heading_path, char_start, char_end,"
            " qdrant_point_id, text)"
            " VALUES (:id, :tenant, :document, :index, :path, :start, :end, :point, :text)",
            {
                "id": uuid.uuid4(),
                "tenant": tenant.tenant_id,
                "document": document_id,
                "index": chunk.chunk_index,
                "path": list(chunk.heading_path),
                "start": chunk.char_start,
                "end": chunk.char_end,
                "point": uuid.uuid4(),
                "text": SOURCE[chunk.char_start : chunk.char_end],
            },
        )


def _simple_chunks() -> tuple[PlantedChunk, ...]:
    start, end = _span("## 1. Purpose", "Fake sentence three.\n")
    return (PlantedChunk(0, ("Fake Access Policy", "1. Purpose"), start, end),)


@pytest.fixture
def world(
    seeded_mcp_db: SeededMcp,
    mcp_pg: TempDatabaseFactory,
    mcp_redis_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[World]:
    """A cloned database with two tenants."""
    url = mcp_pg.clone(seeded_mcp_db.clean_database_url)
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("REDIS_URL", mcp_redis_url)
    monkeypatch.setenv("JWT_SECRET", TEST_JWT_SIGNING_PHRASE)
    monkeypatch.setenv("QDRANT_URL", TEST_QDRANT_URL)
    marker = uuid.uuid4().hex[:8]
    try:
        yield World(
            database_url=url,
            real=_plant_tenant(url, slug=f"fake-real-{marker}"),
            other=_plant_tenant(url, slug=f"fake-other-{marker}"),
        )
    finally:
        mcp_pg.drop(url)


def _read(
    user: SeededUser, arguments: dict[str, Any], principals: tuple[str, ...] = ("employee",)
) -> dict[str, Any]:
    result = call_tool(bearer(token_for(user, principals=principals)), READ_POLICIES, arguments)
    assert result.is_error is False, error_code(result)
    assert result.structured_content is not None
    payload: dict[str, Any] = result.structured_content
    return payload


def _listed(payload: dict[str, Any]) -> list[tuple[str, str, str]]:
    return [(row["slug"], row["title"], row["version"]) for row in payload["documents"]]


@pytest.mark.parametrize("principals", [("employee",), ("manager",), ("it_admin",)])
def test_every_principal_may_read(world: World, principals: tuple[str, ...]) -> None:
    """Catches a matrix row copied from `list_policy_documents`, which gives the
    rules to admins only: an employee, an approver and an admin all read them."""
    _plant_document(
        world, world.real, slug="fake-access", title="Fake Access", chunks=_simple_chunks()
    )

    payload = _read(world.real, {}, principals)

    assert _listed(payload) == [("fake-access", "Fake Access", "v1")]
    assert payload["document"] is None


def test_the_list_holds_each_current_document_once(world: World) -> None:
    """Catches a list that shows every version (the old v1 beside the live v2),
    one that keeps a document with no live version, and one sorted by slug
    rather than by title: the slugs here sort the other way round."""
    _plant_document(world, world.real, slug="fake-a", title="Zed Fake Rules", is_current=False)
    _plant_document(world, world.real, slug="fake-a", title="Zed Fake Rules", version="v2")
    _plant_document(world, world.real, slug="fake-b", title="Alpha Fake Rules")
    _plant_document(world, world.real, slug="fake-c", title="Gone Fake Rules", is_current=False)

    payload = _read(world.real, {})

    assert _listed(payload) == [
        ("fake-b", "Alpha Fake Rules", "v1"),
        ("fake-a", "Zed Fake Rules", "v2"),
    ]


def test_a_document_reads_whole_with_no_overlap(world: World) -> None:
    """Catches a stitch that joins chunk texts as they are, so the overlap
    sentence shows twice; one that orders by insertion rather than by
    `chunk_index` (rows go in backwards here); and one that keeps the heading
    line in the body, which the page already draws from `heading_path`."""
    purpose = ("Fake Access Policy", "1. Purpose")
    scope = ("Fake Access Policy", "2. Scope")
    first = _span("## 1. Purpose", "Fake sentence two. ")
    second = _span("Fake sentence two. ", "Fake sentence three.\n\n")
    third = _span("## 2. Scope", "- Fake item one\n")
    fourth = _span("- Fake item two", "- Fake item two\n")
    chunks = (
        PlantedChunk(3, scope, *fourth),
        PlantedChunk(2, scope, *third),
        PlantedChunk(1, purpose, *second),
        PlantedChunk(0, purpose, *first),
    )
    _plant_document(
        world, world.real, slug="fake-access", title="Fake Access Policy", chunks=chunks
    )

    payload = _read(world.real, {"slug": "fake-access"})

    document = payload["document"]
    assert document["slug"] == "fake-access"
    assert document["title"] == "Fake Access Policy"
    assert document["version"] == "v1"
    assert document["sections"] == [
        {
            "heading_path": list(purpose),
            "text": "Fake sentence one. Fake sentence two. Fake sentence three.",
        },
        {"heading_path": list(scope), "text": "- Fake item one\n- Fake item two"},
    ]
    assert _listed(payload) == [("fake-access", "Fake Access Policy", "v1")]


@pytest.mark.parametrize("slug", ["fake-missing", "fake-retired"])
def test_an_unknown_slug_is_not_found(world: World, slug: str) -> None:
    """Catches a tool that answers an empty document, or the old version, for
    a slug with no current version, instead of a typed `NOT_FOUND`."""
    _plant_document(world, world.real, slug="fake-retired", title="Fake Old", is_current=False)

    result = call_tool(bearer(token_for(world.real)), READ_POLICIES, {"slug": slug})

    assert error_code(result) == "NOT_FOUND"


@pytest.mark.parametrize(
    "arguments",
    [
        {"slug": "Not A Slug!"},
        {"slug": "fake-access\n"},
        {"slug": ""},
        {"slug": "a" * 65},
        {"tenant_id": "11111111-1111-5111-8111-111111111111"},
    ],
)
def test_a_bad_slug_is_refused(world: World, arguments: dict[str, Any]) -> None:
    """Catches a slug passed to the query unchecked (a trailing newline is the
    sneaky one, which `pattern=` would let through) and an input that quietly
    ignores a stray `tenant_id` instead of refusing it."""
    result = call_tool(bearer(token_for(world.real)), READ_POLICIES, arguments)

    assert error_code(result) == "VALIDATION"


def test_another_tenants_policy_is_never_read(world: World) -> None:
    """Catches a read that misses the tenant filter: the other tenant's slug is
    answered `NOT_FOUND`, the same as a slug that does not exist, and the list
    stays empty."""
    _plant_document(
        world, world.real, slug="fake-secret", title="Fake Secret", chunks=_simple_chunks()
    )

    listed = _read(world.other, {})
    asked = call_tool(bearer(token_for(world.other)), READ_POLICIES, {"slug": "fake-secret"})

    assert listed["documents"] == []
    assert listed["document"] is None
    assert error_code(asked) == "NOT_FOUND"
