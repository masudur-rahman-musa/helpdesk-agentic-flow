"""`list_policy_documents`, through the transport.

Surface under test (written before it exists):

``mcp_server.tools.policy_docs``
    ``ListPolicyDocumentsInput(BaseModel)`` - NO fields at all: no tenant, no
    slug, no version. The tenant is the token's and the corpus is that
    tenant's own rows, so there is nothing for a caller to
    choose, and ``extra="forbid"`` makes a key riding along a ``VALIDATION``
    rather than a silently ignored argument.
    ``PolicyVersionRow(BaseModel)`` - exactly ``version``, ``is_current``,
    ``ingested_at``, ``chunk_count``, ``source_path`` and ``sha256``.
    ``PolicyDocumentRow(BaseModel)`` - exactly ``slug``, ``title`` and
    ``versions``.
    ``ListPolicyDocumentsOutput(BaseModel)`` - one field, ``documents``.
    ``list_policy_documents`` - a registered READ tool (never mutating, no
    idempotency key) whose wire name is ``list_policy_documents`` and whose
    registry ``event_type`` is the generic ``tool.succeeded``: the board
    writes nothing, so there is no domain event to append.

``helpdesk.rag.documents``
    ``read_policy_documents(session)``, the one read behind it. Its own cases
    live in ``packages/helpdesk/tests/test_rag_documents.py`` - the two
    orders, the title rule, the two statements and the zero chunk count are
    pinned there, against the engine, and this file pins what reaches the
    WIRE.

The ``/admin/policies`` page belongs to ``it_admin`` and
``platform_admin``, so the matrix row for this tool has exactly those
two cells at ``Scope.ANY``. There is NO scope helper on this tool, so the
pipeline's matrix stage IS the whole permission check and the five-principal
case below is its kill (the handler never re-runs
``rbac.authorize``, so there is no second copy of that guard).

Everything here is a READ: the tool takes no arguments, no key is derived from
client input and nothing is written.

Fixture cost: everything rides the session-scoped
``mcp_fixtures`` seam - one seeded throwaway database, one dedicated Redis
index flushed per test. The seeded tenants are SHARED with every other file in
this suite and several of them plant policy documents of their own, so the
cases that assert a whole corpus exactly plant a tenant of their OWN inside
that one database and remove it again: per-test isolation is a namespace, never
a rebuilt database.

The corpus is planted as raw rows rather than through
``mcp_fixtures.plant_policy_document``, which wraps
``register_document_version``: that writer always writes ``is_current=True``
and flips every other version of the slug, so the states this board exists to
show - a version with no chunk at all, a document with no current version -
are exactly the ones it cannot produce.

No pytest-asyncio: each case is a sync function driving one ``asyncio.run``.
"""

import asyncio
import uuid
from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from helpdesk.mcp_server.tools.policy_docs import (
    ListPolicyDocumentsOutput,
    PolicyDocumentRow,
    PolicyVersionRow,
)
from helpdesk.mcp_server.tools.registry import REGISTRY
from tests.support.mcp import (  # noqa: F401 - fixtures are resolved by name
    SeededMcp,
    SeededUser,
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

LIST_POLICY_DOCUMENTS = "list_policy_documents"

# The envelope, the row and the version block, written out:
# what the agent-api route forwards and the page renders. An extra key is as much a
# defect as a missing one.
ENVELOPE_KEYS = {"documents"}
DOCUMENT_KEYS = {"slug", "title", "versions"}
VERSION_KEYS = {
    "version",
    "is_current",
    "ingested_at",
    "chunk_count",
    "source_path",
    "sha256",
}

# The `list_policy_documents` matrix row: the two cells that
# `/admin/policies` needs. Checked against `list_sod_rules`' row in
# `helpdesk/auth/rbac.py`, which carries the same pair.
ALLOWED_PRINCIPALS = ("it_admin", "platform_admin")
DENIED_PRINCIPALS = ("employee", "manager", "system_owner", "hr", "auditor")

# The three keys a caller must not be able to send. Each is a
# plausible one: a `tenant_id` asks for another tenant's corpus, a `slug` and a
# `version` ask the board to narrow itself.
FORBIDDEN_ARGUMENT_KEYS = ("tenant_id", "slug", "version")

BASE_INSTANT = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)

# Obviously fake digests, 64 hex characters each (nothing here is
# a real document's digest and nothing is a secret).
DIGEST_ACCESS_V1 = "a1" * 32
DIGEST_ACCESS_V2 = "b2" * 32
DIGEST_LEAVE_V1 = "c3" * 32

_INSERT_DOCUMENT = (
    "INSERT INTO policy_documents"
    " (id, tenant_id, slug, title, version, is_current, source_path, sha256, ingested_at)"
    " VALUES (:id, :tenant, :slug, :title, :version, :is_current, :source_path,"
    " :sha256, :ingested_at)"
)

_INSERT_CHUNK = (
    "INSERT INTO policy_chunks"
    " (id, tenant_id, document_id, chunk_index, heading_path, char_start, char_end,"
    " qdrant_point_id, text)"
    " VALUES (:id, :tenant, :document, :index, ARRAY['1. Purpose']::text[], :start, :end,"
    " :point, :text)"
)


def _structured(result: Any) -> dict[str, Any]:
    assert result.is_error is False, result.content
    assert result.structured_content is not None, "a successful call carries structured content"
    content: dict[str, Any] = result.structured_content
    return content


def _documents(user: SeededUser, principal: str = "it_admin") -> list[dict[str, Any]]:
    """The whole board as one permitted principal of that tenant reads it."""
    payload = _structured(
        call_tool(bearer(token_for(user, principals=(principal,))), LIST_POLICY_DOCUMENTS, {})
    )
    assert set(payload) == ENVELOPE_KEYS, payload
    rows: list[dict[str, Any]] = payload["documents"]
    return rows


# --- probes around the tool, never through it ------------------------------------


def _execute(database_url: str, statements: list[tuple[str, dict[str, Any]]]) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as conn:
                for sql, params in statements:
                    await conn.execute(text(sql), params)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def _plant_tenant(database_url: str, label: str, principal: str = "it_admin") -> SeededUser:
    """A tenant, a department and one admin, and no corpus at all.

    The seeded tenants are shared with every other file in this suite, several
    of which plant policy documents into them, so a case asserting a WHOLE
    corpus exactly needs a namespace of its own inside the one session-scoped
    database rather than a second database.
    """
    marker = uuid.uuid4().hex[:10]
    tenant_id, department_id, user_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    slug = f"fake-{label}-{marker}"
    email = f"fake.admin.{marker}@fake.invalid"
    _execute(
        database_url,
        [
            (
                "INSERT INTO tenants (id, slug, name, residency, settings)"
                " VALUES (:id, :slug, :name, 'eu', '{}'::jsonb)",
                {"id": tenant_id, "slug": slug, "name": "Fake Tenant (policy board)"},
            ),
            (
                "INSERT INTO departments (id, tenant_id, name, manager_user_id)"
                " VALUES (:id, :tenant, :name, NULL)",
                {"id": department_id, "tenant": tenant_id, "name": "Fake Department"},
            ),
            (
                "INSERT INTO users (id, tenant_id, department_id, email, display_name,"
                " password_hash, principals, status, start_date, end_date)"
                " VALUES (:id, :tenant, :department, :email, :display_name, :password_hash,"
                " ARRAY[:principal]::varchar[], 'active', :start_date, NULL)",
                {
                    "id": user_id,
                    "tenant": tenant_id,
                    "department": department_id,
                    "email": email,
                    "display_name": "Fake Policy Admin",
                    # Not a hash and not a secret: this user never
                    # authenticates, tokens are minted directly.
                    "password_hash": "not-a-real-hash-this-user-never-logs-in",
                    "principal": principal,
                    "start_date": date(2026, 1, 1),
                },
            ),
        ],
    )
    return SeededUser(
        tenant_id=tenant_id,
        tenant_slug=slug,
        user_id=user_id,
        email=email,
        display_name="Fake Policy Admin",
        department_id=department_id,
        principals=(principal,),
        status="active",
    )


def _plant_version(
    database_url: str,
    tenant: SeededUser,
    *,
    slug: str,
    title: str,
    version: str,
    is_current: bool,
    ingested_at: datetime,
    sha256: str,
    chunks: int,
) -> uuid.UUID:
    """One `policy_documents` row and `chunks` `policy_chunks` rows."""
    document_id = uuid.uuid4()
    statements: list[tuple[str, dict[str, Any]]] = [
        (
            _INSERT_DOCUMENT,
            {
                "id": document_id,
                "tenant": tenant.tenant_id,
                "slug": slug,
                "title": title,
                "version": version,
                "is_current": is_current,
                "source_path": f"policies/{tenant.tenant_slug}/{version}/{slug}.md",
                "sha256": sha256,
                "ingested_at": ingested_at,
            },
        )
    ]
    statements.extend(
        (
            _INSERT_CHUNK,
            {
                "id": uuid.uuid4(),
                "tenant": tenant.tenant_id,
                "document": document_id,
                "index": index,
                "start": index * 200,
                "end": index * 200 + 120,
                "point": uuid.uuid4(),
                "text": f"Obviously fake policy span {index} of {slug} {version}.",
            },
        )
        for index in range(chunks)
    )
    _execute(database_url, statements)
    return document_id


def _remove_tenant(database_url: str, tenant: SeededUser) -> None:
    """Take the planted tenant back out, children first."""
    _execute(
        database_url,
        [
            (
                "DELETE FROM policy_chunks WHERE tenant_id = :tenant",
                {"tenant": tenant.tenant_id},
            ),
            (
                "DELETE FROM policy_documents WHERE tenant_id = :tenant",
                {"tenant": tenant.tenant_id},
            ),
            ("DELETE FROM audit_events WHERE tenant_id = :tenant", {"tenant": tenant.tenant_id}),
            ("DELETE FROM users WHERE tenant_id = :tenant", {"tenant": tenant.tenant_id}),
            ("DELETE FROM departments WHERE tenant_id = :tenant", {"tenant": tenant.tenant_id}),
            ("DELETE FROM tenants WHERE id = :tenant", {"tenant": tenant.tenant_id}),
        ],
    )


def _plant_corpus(database_url: str, tenant: SeededUser) -> None:
    """Two documents, three versions, one of them with no chunk at all.

    `fake-access-policy` carries v2 (current, newer, 3 chunks) and v1 (older,
    0 chunks); `fake-leave-policy` carries one current version with 2 chunks.
    Planted so the slug order and the version order are both the REVERSE of
    insertion order.
    """
    _plant_version(
        database_url,
        tenant,
        slug="fake-leave-policy",
        title="Fake Leave Policy",
        version="v1",
        is_current=True,
        ingested_at=BASE_INSTANT,
        sha256=DIGEST_LEAVE_V1,
        chunks=2,
    )
    _plant_version(
        database_url,
        tenant,
        slug="fake-access-policy",
        title="Fake Access Policy",
        version="v1",
        is_current=False,
        ingested_at=BASE_INSTANT,
        sha256=DIGEST_ACCESS_V1,
        chunks=0,
    )
    _plant_version(
        database_url,
        tenant,
        slug="fake-access-policy",
        title="Fake Access Policy",
        version="v2",
        is_current=True,
        ingested_at=datetime(2026, 4, 1, 9, 0, tzinfo=UTC),
        sha256=DIGEST_ACCESS_V2,
        chunks=3,
    )


# --- the input model ----------------------------------


def test_the_tool_declares_no_field_at_all(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """The input model is pinned as an exactly empty field set.

    KILL: an input that grew a `slug`, a `version`, a `limit` or a
    `tenant_id`, so a caller could choose what the board answers or whose
    corpus it reads. The declared field set is asserted EMPTY, which no later
    field can ride along into - an OPTIONAL field would pass every other case
    in this file, because every other case sends `{}`.
    """
    spec = REGISTRY[LIST_POLICY_DOCUMENTS]

    assert spec.input_model.model_fields == {}


@pytest.mark.parametrize("key", FORBIDDEN_ARGUMENT_KEYS)
def test_an_argument_riding_along_is_refused_validation(
    mcp_env: SeededMcp,  # noqa: F811
    key: str,
) -> None:
    """The wire half: `extra="forbid"` and not `ignore`.

    KILL: `model_config = ConfigDict(extra="ignore")` (Pydantic's default is
    to ignore unknown keys, so writing no config at all is the same defect).
    An ignored `tenant_id` is a caller who believes they are reading another
    tenant's corpus and is being answered with their own, and an ignored
    `slug` is a page that believes it filtered.

    The `tenant_id` sent below is the caller's OWN, so the
    refusal cannot be the tenant guard answering on the model's behalf: this
    caller holds a cell the matrix gives the row, every other stage
    passes, and nothing but the empty model can refuse it.
    """
    values = {
        "tenant_id": str(mcp_env.tenant_a.tenant_id),
        "slug": "fake-access-policy",
        "version": "v1",
    }

    result = call_tool(
        bearer(token_for(mcp_env.tenant_a, principals=("it_admin",))),
        LIST_POLICY_DOCUMENTS,
        {key: values[key]},
    )

    assert error_code(result) == "VALIDATION"


# --- the output models ---------------------------------------------


def test_the_output_models_carry_exactly_the_planned_field_sets(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """The field sets, read off the models rather than off a payload.

    KILL: an envelope keyed `policies` or `rows` rather than `documents` (the
    web parser is written against that key and would refuse a payload the
    server answered correctly); a version row short of `sha256` or
    `source_path`, which is carried because the digest is the only
    evidence on this surface that two versions differ at all; and a row that
    grew a `document_id` or a `chunk_ids` list, which this page has no use for
    and which a later caller would start depending on.

    Asserted on the MODELS as well as on a payload below, because a payload
    whose extra field happens to be absent from the fixture proves nothing
    about the model.
    """
    assert set(ListPolicyDocumentsOutput.model_fields) == ENVELOPE_KEYS
    assert set(PolicyDocumentRow.model_fields) == DOCUMENT_KEYS
    assert set(PolicyVersionRow.model_fields) == VERSION_KEYS


# --- the board itself (on the wire) ----------------


@pytest.mark.parametrize("principal", ALLOWED_PRINCIPALS)
def test_the_board_answers_the_whole_corpus_grouped_and_in_the_helpers_order(
    mcp_env: SeededMcp,  # noqa: F811
    principal: str,
) -> None:
    """The handler maps and reorders NOTHING,
    for BOTH cells of the matrix row.

    KILL: a handler that re-sorts what the helper handed it - by `version`
    string (`v2` would lead `v1` here only by luck, so the slug order is what
    catches it), by title, or reversed; one that FLATTENS the versions into
    one row per version, which is wrong; one that drops the version
    with no chunks (`chunk_count` 0 is asserted present, which is the ingest
    failure this page exists to show); and one that opens its own session or
    binds a tenant from anywhere but the token - the planted tenant's corpus
    is exactly two slugs, so any leakage from the two seeded tenants (which
    other files in this suite plant documents into) fails the exact list.

    Parametrised over both permitted principals so the second cell is pinned
    by a SUCCESS and not only by the denial sweep: a matrix row carrying
    `platform_admin` alone passes every other case in this file.
    """
    tenant = _plant_tenant(mcp_env.database_url, "corpus", principal=principal)
    try:
        _plant_corpus(mcp_env.database_url, tenant)

        documents = _documents(tenant, principal)

        assert [document["slug"] for document in documents] == [
            "fake-access-policy",
            "fake-leave-policy",
        ]
        for document in documents:
            assert set(document) == DOCUMENT_KEYS, document
            for version in document["versions"]:
                assert set(version) == VERSION_KEYS, version

        access, leave = documents
        assert access["title"] == "Fake Access Policy"
        assert [version["version"] for version in access["versions"]] == ["v2", "v1"]
        assert [version["is_current"] for version in access["versions"]] == [True, False]
        assert [version["chunk_count"] for version in access["versions"]] == [3, 0]
        assert [version["sha256"] for version in access["versions"]] == [
            DIGEST_ACCESS_V2,
            DIGEST_ACCESS_V1,
        ]
        assert access["versions"][0]["source_path"].endswith("/v2/fake-access-policy.md")
        assert [version["version"] for version in leave["versions"]] == ["v1"]
        assert [version["chunk_count"] for version in leave["versions"]] == [2]
    finally:
        _remove_tenant(mcp_env.database_url, tenant)


def test_a_tenant_with_no_corpus_answers_an_empty_list_as_a_success(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """An un-ingested tenant is answered, never refused.

    KILL: a handler that turns the empty read into `NOT_FOUND` (the shape
    every id-taking tool in this suite uses, and the easiest thing to copy),
    or one that lets the empty read raise. Either gives a new tenant's admin a
    broken `/admin/policies` page instead of an empty one.

    The envelope is asserted present with an empty list rather than merely
    "not an error": a success carrying no `documents` key at all would leave
    the web parser reading `undefined`.
    """
    tenant = _plant_tenant(mcp_env.database_url, "empty")
    try:
        payload = _structured(call_tool(bearer(token_for(tenant)), LIST_POLICY_DOCUMENTS, {}))

        assert set(payload) == ENVELOPE_KEYS, payload
        assert payload["documents"] == []
    finally:
        _remove_tenant(mcp_env.database_url, tenant)


# --- the five empty matrix cells ---------------


@pytest.mark.parametrize("principal", DENIED_PRINCIPALS)
@pytest.mark.parametrize("corpus", [True, False], ids=["with-corpus", "empty-corpus"])
def test_the_five_denied_principals_are_refused_whatever_the_corpus_holds(
    mcp_env: SeededMcp,  # noqa: F811
    principal: str,
    corpus: bool,
) -> None:
    """The five empty matrix cells on this row, and the
    "not an existence oracle" rule.

    KILL: adding this principal to the `list_policy_documents` cell of the
    RBAC matrix. This handler runs no scope helper and therefore never re-runs
    `rbac.authorize`, so the pipeline's matrix stage is the only guard that
    can refuse here and deleting it hands every one of these five the tenant's
    whole corpus listing (there is no second
    copy of the guard). `auditor` is the cell that matters most - it holds one
    on `search_audit` and none here, because the corpus is configuration and
    not a record of what happened.

    The corpus dimension matters too: the SAME code comes
    back whether the tenant has been ingested or not, so the refusal cannot be
    read as an answer to "does this tenant have a corpus". That kills a
    handler which read the corpus first and answered `NOT_FOUND` for an empty
    one before any permission was decided.

    The call carries no arguments at all, which are the only valid arguments
    this tool has, so no validation stage can produce this answer.
    """
    tenant = _plant_tenant(mcp_env.database_url, "denied", principal=principal)
    try:
        if corpus:
            _plant_corpus(mcp_env.database_url, tenant)

        result = call_tool(bearer(token_for(tenant)), LIST_POLICY_DOCUMENTS, {})

        assert error_code(result) == "FORBIDDEN"
    finally:
        _remove_tenant(mcp_env.database_url, tenant)
