"""An earlier change, sections 1 and 6: `reingest_policy_document`, through the transport.

Surface under test (written before it exists; every case here is red on one of
these names until steps 1 to 5 land):

``mcp_server.tools.admin``
    ``ReingestPolicyDocumentInput(MutatingToolInput)`` - exactly ``slug``,
    ``version`` and the ``idempotency_key`` and ``extra="forbid"`` that base
    class carries. No ``tenant_id`` and no ``source_path``: the tenant is the
    token's and the corpus layout is the pipeline's.
    ``ReingestPolicyDocumentOutput(BaseModel)`` - exactly ``slug``,
    ``version``, ``created``, ``chunk_count``, ``is_current``, ``sha256``,
    ``ingested_at`` and a human-readable ``summary`` (SPEC section 8.1: the
    summary is a FIELD, never the whole response).
    ``reingest_policy_document`` - a registered MUTATING tool whose wire name
    is ``reingest_policy_document`` and whose registry ``event_type`` is the
    declared ``policy.reingested`` (decision D).

``mcp_server.rag_clients``
    ``redactor(settings)`` - the pooled `RedactionClient` accessor (decision
    F), read through the MODULE by the handler exactly as ``qdrant`` and
    ``embedder`` are, which is what lets this file substitute it. The stub
    below is written to accept the nullary form too, so a ``redactor()``
    taking no settings is not a second red on top of the ones this file is
    for.

``helpdesk.rag.pipeline``
    ``ingest_one_document`` and ``CorpusDocumentNotFoundError``, whose own
    cases live in ``packages/helpdesk/tests/test_rag_pipeline.py`` - the path,
    the ``source_path`` string, both name checks, the boundary pair and the
    unchanged/changed pair are pinned there, against the engine. What this
    file pins is what reaches the WIRE.

``helpdesk.auth.rbac``
    ``Tool.REINGEST_POLICY_DOCUMENT`` with exactly two cells, ``it_admin``
    and ``platform_admin`` at ``Scope.ANY`` - SPEC section 14's
    ``/admin/policies`` audience, the same pair ``list_policy_documents``
    holds (a design note, cited rather than re-opened).

There is NO scope helper on this tool: both cells are ``Scope.ANY``, so the
pipeline's matrix stage IS the whole permission check and the five-principal
case below is its kill (a project rule - the handler never re-runs
``rbac.authorize``, so there is no second copy of that guard to hide behind).

The corpus root is ``POLICY_DIR``, pointed at this case's ``tmp_path``: no
test writes into the repository's own ``policies/`` tree, and the file a case
plants is the only thing on disk under the tenant it plants for.

What is faked and why (plan section 6's cost paragraph): the redaction gateway
and the embedder, both through ``rag_clients``' module attributes, so no
gateway is reached and no ONNX model is loaded. Qdrant, Postgres and Redis are
REAL and session-scoped: the mutation's whole claim is that
two stores were written, and a faked store would prove neither.

Audit deltas are asserted as exact ordered event-type lists against a before
snapshot, never as counts: a missing domain event, a duplicated one, a replay
that re-emitted ``policy.reingested`` or a denial filed as an outage is
invisible to a count.

No pytest-asyncio: each case is a sync function driving one ``asyncio.run``.
"""

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest
from mcp_fixtures import (  # noqa: F401 - fixtures are resolved by name
    RecordingEmbedder,
    SeededMcp,
    bearer,
    call_tool,
    error_code,
    mcp_env,
    mcp_pg,
    mcp_policy_collections,
    mcp_qdrant,
    mcp_redis_base_url,
    mcp_redis_url,
    seeded_mcp_db,
    token_for,
)
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from helpdesk.mcp_server import rag_clients
from helpdesk.mcp_server.registry import REGISTRY
from helpdesk.shared.rag.gateway import RedactedDocument

REINGEST = "reingest_policy_document"
SET_KILL_SWITCH = "set_kill_switch"

# The wire contract as literals, never imported from the module under test:
# reading these strings off the code would let a rename pass unnoticed.
REINGESTED_EVENT = "policy.reingested"
DENIED_EVENT = "tool.denied"
REPLAYED_EVENT = "tool.replayed"

# SPEC section 8.1's kill switch key shape, as a literal for the same reason.
KEY_PREFIX_LITERAL = "killswitch"

# The input's exact field set (criterion 2) and the output's (criterion 10):
# seven values plus the summary. Written out rather than derived from the
# models, so a field added or renamed on both sides at once is still red here.
INPUT_FIELDS = {"slug", "version", "idempotency_key"}
OUTPUT_FIELDS = {
    "slug",
    "version",
    "created",
    "chunk_count",
    "is_current",
    "sha256",
    "ingested_at",
    "summary",
}

# SPEC section 7's `reingest_policy_document` row: the two cells SPEC section
# 14 gives `/admin/policies`, the same pair `list_policy_documents` holds.
ALLOWED_PRINCIPALS = ("it_admin", "platform_admin")
DENIED_PRINCIPALS = ("employee", "manager", "system_owner", "hr", "auditor")

# Obviously fake policy prose: no real company, person or
# address, and nothing here is a secret.
SOURCE = (
    b"# Fake Reingest Policy\n\n"
    b"## 1. Purpose\n\n"
    b"Fake access to a fake system is granted by that system's owner and "
    b"reviewed every quarter.\n\n"
    b"## 2. Scope\n\n"
    b"Fake contractors follow the same fake rules as fake employees.\n"
)

VERSION = "v1"


class StubRedactor:
    """A `Redactor` that substitutes one string and files a fixed map id.

    Offline and deterministic, the shape `packages/helpdesk/tests/
    test_rag_pipeline.py::FakeRedactor` already has: no gateway is reachable
    in a test run, and what this file pins is the wire, not the redaction.
    """

    async def redact_document(self, text: str, tenant_slug: str) -> RedactedDocument:
        return RedactedDocument(
            text=text.replace("contractors", "<PERSON_1>"),
            redaction_map_id="fake-redaction-map-31a",
        )


@pytest.fixture(autouse=True)
def _stub_gateway_and_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """No gateway call and no ONNX load, for every case in this file.

    Both accessors are read through the `rag_clients` module by the handler
    (the way `tools/policy.py` reads them), so replacing the attribute is
    enough and nothing about the tool's own behaviour is faked: the corpus
    file, both stores, the idempotency guard, the kill switch and the audit
    chain are all real.

    Qdrant is deliberately NOT stubbed. Criterion 8 is a claim about what was
    written to the vector store as well as to Postgres, and a stubbed store
    would leave that half asserted against a recording object instead of
    against the platform's own.
    """
    monkeypatch.setattr(rag_clients, "embedder", lambda: RecordingEmbedder())
    monkeypatch.setattr(rag_clients, "redactor", lambda *_settings: StubRedactor())


@pytest.fixture
def corpus_root(
    mcp_env: SeededMcp,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """`POLICY_DIR` pointed at this case's own empty directory.

    Per-test isolation is a namespace, never a rebuilt anything (standing rule
    7): the database, Redis and Qdrant are the session's, and what varies per
    case is this root plus a uuid4-suffixed document slug inside it. Pointing
    the setting rather than the repository's `policies/` tree also means no
    case can ingest a real corpus file into the shared test database.
    """
    monkeypatch.setenv("POLICY_DIR", str(tmp_path))
    return tmp_path


# --- probes around the tool, never through it ------------------------------------


def _rows(database_url: str, sql: str, params: dict[str, Any]) -> list[Any]:
    async def scenario() -> list[Any]:
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as conn:
                return list(await conn.execute(text(sql), params))
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _versions(database_url: str, tenant_id: uuid.UUID, slug: str) -> list[Any]:
    """Every `policy_documents` row for one slug, oldest ingest first."""
    return _rows(
        database_url,
        "SELECT version, is_current, sha256, source_path FROM policy_documents"
        " WHERE tenant_id = :tenant AND slug = :slug ORDER BY ingested_at, version",
        {"tenant": tenant_id, "slug": slug},
    )


def _chunk_count(database_url: str, tenant_id: uuid.UUID, slug: str) -> int:
    rows = _rows(
        database_url,
        "SELECT count(*) FROM policy_chunks c JOIN policy_documents d ON d.id = c.document_id"
        " WHERE d.tenant_id = :tenant AND d.slug = :slug",
        {"tenant": tenant_id, "slug": slug},
    )
    return int(rows[0][0])


def _audit_rows(database_url: str, tenant_id: uuid.UUID) -> list[Any]:
    """Every (event_type, actor_ref, subject_type, subject_id) of one tenant."""
    return _rows(
        database_url,
        "SELECT event_type, actor_ref, subject_type, subject_id FROM audit_events"
        " WHERE tenant_id = :tenant ORDER BY seq",
        {"tenant": tenant_id},
    )


def _event_types(database_url: str, tenant_id: uuid.UUID) -> list[str]:
    return [str(row.event_type) for row in _audit_rows(database_url, tenant_id)]


def _delete_key(redis_url: str, key: str) -> None:
    """Clean-up around the tool: no case may leave a stopped tool behind."""

    async def scenario() -> None:
        client: Redis = Redis.from_url(redis_url, socket_connect_timeout=2, socket_timeout=2)
        try:
            await client.delete(key)
        finally:
            await client.aclose()

    asyncio.run(scenario())


def _tenant_key(tenant_id: uuid.UUID, tool_name: str) -> str:
    return f"{KEY_PREFIX_LITERAL}:{tenant_id}:{tool_name}"


# --- driving the tool ------------------------------------------------------------


def _slug() -> str:
    """A document slug no other case in this suite can collide with."""
    return f"fake-reingest-{uuid.uuid4().hex[:12]}"


def _key() -> str:
    """A unique idempotency key: the other half of the per-test namespace."""
    return f"fake-key-{uuid.uuid4().hex}"


def _plant_corpus_file(root: Path, env: SeededMcp, slug: str, *, source: bytes = SOURCE) -> Path:
    """The one path an earlier change fixes: `{POLICY_DIR}/{tenant}/{version}/{slug}.md`.

    The tenant directory is resolved from `env.tenant_a.tenant_slug` - the
    handle the seeding fixture read back - and never from a slug named here, so the file a case plants and the token a case calls
    with are the same tenant by construction.
    """
    path = root / env.tenant_a.tenant_slug / VERSION / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(source)
    return path


def _token(env: SeededMcp, principal: str = "it_admin") -> dict[str, str]:
    """A tenant-A token carrying ONE of SPEC section 7's cells on this row."""
    return bearer(token_for(env.tenant_a, principals=(principal,)))


def _args(slug: str, *, key: str | None = None, **extra: Any) -> dict[str, Any]:
    """Minimally valid arguments naming one document of the caller's corpus."""
    return {
        "slug": slug,
        "version": VERSION,
        "idempotency_key": key if key is not None else _key(),
        **extra,
    }


def _structured(result: Any) -> dict[str, Any]:
    assert result.is_error is False, result.content
    assert result.structured_content is not None, "a successful call carries structured content"
    content: dict[str, Any] = result.structured_content
    return content


def _response_text(result: Any) -> str:
    """The single content block of a successful result, as an exact string."""
    assert result.is_error is False, result.content
    assert len(result.content) == 1, f"expected exactly one content block, got {result.content!r}"
    block = result.content[0]
    return str(block.text)


# --- the input model (criterion 2, a project rule) ----------------------------------


def test_the_input_declares_exactly_slug_version_and_idempotency_key(
    mcp_env: SeededMcp,  # noqa: F811
    corpus_root: Path,
) -> None:
    """Criterion 2, as an EXACT field set and then at the wire.

    KILL: an input that grew a `tenant_id` (the call would then name whose
    corpus to rewrite, which only the token may decide - a project rule) or a
    `source_path` (the caller would choose which FILE on the ingest host is
    read, which is the traversal `check_slug` exists to stop, one layer above
    it). Equally an input written `BaseModel` rather than `MutatingToolInput`:
    the key would vanish, the pipeline's guard would never run, and a retried
    re-ingest would run the effect twice.

    The wire half is `extra="forbid"` and not Pydantic's default `ignore`. The
    `tenant_id` sent below is the caller's OWN, so the
    refusal cannot be the tenant guard answering on the model's behalf: this
    caller holds a cell, the corpus file exists, every other stage passes, and
    nothing but the declared field set can refuse it. Nothing is written,
    which is what separates a refusal from a refusal before any effect.
    """
    slug = _slug()
    _plant_corpus_file(corpus_root, mcp_env, slug)
    spec = REGISTRY[REINGEST]

    assert set(spec.input_model.model_fields) == INPUT_FIELDS

    result = call_tool(
        _token(mcp_env),
        REINGEST,
        _args(slug, tenant_id=str(mcp_env.tenant_a.tenant_id)),
    )

    assert error_code(result) == "VALIDATION"
    assert _versions(mcp_env.database_url, mcp_env.tenant_a.tenant_id, slug) == []
    assert _chunk_count(mcp_env.database_url, mcp_env.tenant_a.tenant_id, slug) == 0


# --- SPEC section 7's row, both directions (criterion 12) ------------------------


@pytest.mark.parametrize("principal", ALLOWED_PRINCIPALS)
def test_the_two_permitted_principals_re_ingest_and_the_other_five_are_refused(
    mcp_env: SeededMcp,  # noqa: F811
    mcp_policy_collections: tuple[str, str],  # noqa: F811
    corpus_root: Path,
    principal: str,
) -> None:
    """The two cells of SPEC section 7's row, each pinned by a SUCCESS.

    KILL: a matrix row carrying `platform_admin` alone (or `it_admin` alone).
    Either passes the denial sweep below and every other case in this file,
    because every other case calls as one fixed principal; only a success per
    cell can tell the pair apart.

    The success is asserted as a WRITE and not merely as a non-error: a
    handler that answered `created=false` for a file it never read would be a
    green call that ingested nothing, which is exactly what this tool exists
    to do.
    """
    slug = _slug()
    _plant_corpus_file(corpus_root, mcp_env, slug)

    payload = _structured(call_tool(_token(mcp_env, principal), REINGEST, _args(slug)))

    assert payload["created"] is True
    assert payload["chunk_count"] > 0
    assert payload["is_current"] is True
    versions = _versions(mcp_env.database_url, mcp_env.tenant_a.tenant_id, slug)
    assert [(row.version, row.is_current) for row in versions] == [(VERSION, True)]
    assert str(versions[0].source_path) == f"{mcp_env.tenant_a.tenant_slug}/{VERSION}/{slug}.md"


@pytest.mark.parametrize("principal", DENIED_PRINCIPALS)
@pytest.mark.parametrize("planted", [True, False], ids=["file-present", "file-absent"])
def test_the_five_denied_principals_re_ingest_nothing_whatever_is_on_disk(
    mcp_env: SeededMcp,  # noqa: F811
    corpus_root: Path,
    principal: str,
    planted: bool,
) -> None:
    """SPEC section 7's five empty cells on this row, and criterion 12's "not
    an existence oracle for a tenant's corpus".

    KILL: adding this principal to the `reingest_policy_document` cell of the
    RBAC matrix. This handler runs no scope helper and therefore never re-runs
    `rbac.authorize`, so the pipeline's matrix stage is the only guard that
    can refuse here and deleting it hands every one of these five a tool that
    rewrites the evidence every access decision cites (a project rule does
    not apply: there is no second copy of the guard). `auditor` is the cell
    that matters most - it holds one on `search_audit` and none here, because
    the corpus is configuration and not a record of what happened.

    The on-disk dimension is the existence-oracle half: the SAME code comes
    back whether the corpus file is there or not, so a denied caller cannot
    use this tool to learn which documents a tenant holds. That kills a
    handler which read the filesystem first and answered `NOT_FOUND` before
    any permission was decided.

    Every argument is valid and the key is fresh, so no validation and no
    idempotency stage can produce this answer, and the delta is exactly
    `tool.denied` - a permission decision filed as an outage (`tool.failed`)
    is a false record a count could not tell apart.
    """
    slug = _slug()
    if planted:
        _plant_corpus_file(corpus_root, mcp_env, slug)
    before = _event_types(mcp_env.database_url, mcp_env.tenant_a.tenant_id)

    result = call_tool(_token(mcp_env, principal), REINGEST, _args(slug))

    assert error_code(result) == "FORBIDDEN"
    assert _versions(mcp_env.database_url, mcp_env.tenant_a.tenant_id, slug) == []
    assert _event_types(mcp_env.database_url, mcp_env.tenant_a.tenant_id) == [
        *before,
        DENIED_EVENT,
    ]


# --- the idempotency guard (criterion 11) ----------------------------------------


def test_a_replayed_call_is_byte_identical_and_ingests_once(
    mcp_env: SeededMcp,  # noqa: F811
    mcp_policy_collections: tuple[str, str],  # noqa: F811
    corpus_root: Path,
) -> None:
    """Criterion 11's first half.

    KILL: registering the tool with `mutating=False`, which is what makes the
    pipeline run the body instead of answering from the stored idempotency
    row. The second call would then re-read the same unchanged file and answer
    `created=false, chunk_count=0` - a different response for a caller who
    already saw `created=true`, so a retry after a dropped connection would
    report that nothing was ingested.

    Byte identity is asserted and not just equality of meaning, so a response
    rebuilt rather than replayed fails; the audit delta is exactly
    `tool.replayed` after exactly one `policy.reingested`, so a second
    governance record for one re-ingest fails as well.
    """
    slug = _slug()
    _plant_corpus_file(corpus_root, mcp_env, slug)
    arguments = _args(slug)
    token = _token(mcp_env)
    before = _event_types(mcp_env.database_url, mcp_env.tenant_a.tenant_id)

    first = call_tool(token, REINGEST, arguments)
    between = _event_types(mcp_env.database_url, mcp_env.tenant_a.tenant_id)
    second = call_tool(token, REINGEST, arguments)

    assert _structured(first)["created"] is True
    assert _response_text(second) == _response_text(first)
    assert second.structured_content == first.structured_content
    assert between == [*before, REINGESTED_EVENT]
    assert _event_types(mcp_env.database_url, mcp_env.tenant_a.tenant_id) == [
        *between,
        REPLAYED_EVENT,
    ]
    assert len(_versions(mcp_env.database_url, mcp_env.tenant_a.tenant_id, slug)) == 1


def test_the_same_key_with_a_different_slug_is_an_idempotency_conflict(
    mcp_env: SeededMcp,  # noqa: F811
    mcp_policy_collections: tuple[str, str],  # noqa: F811
    corpus_root: Path,
) -> None:
    """Criterion 11's second half.

    KILL: a fingerprint built from the idempotency key alone, which would
    answer a call naming a DIFFERENT document with the first document's stored
    response - the admin would read "re-ingested, 4 chunks" for a file that
    was never opened, and the second document would stay stale for ever
    because every retry replays the first.

    Both corpus files exist and both slugs are valid, so neither the name
    checks nor the missing-file refusal can decide this row; only the
    fingerprint can. The second document is asserted absent from
    `policy_documents`, which is what separates a conflict from a quiet
    replay.
    """
    first_slug, second_slug = _slug(), _slug()
    _plant_corpus_file(corpus_root, mcp_env, first_slug)
    _plant_corpus_file(corpus_root, mcp_env, second_slug)
    key = _key()
    token = _token(mcp_env)

    first = call_tool(token, REINGEST, _args(first_slug, key=key))
    second = call_tool(token, REINGEST, _args(second_slug, key=key))

    assert _structured(first)["slug"] == first_slug
    assert error_code(second) == "IDEMPOTENCY_CONFLICT"
    assert len(_versions(mcp_env.database_url, mcp_env.tenant_a.tenant_id, first_slug)) == 1
    assert _versions(mcp_env.database_url, mcp_env.tenant_a.tenant_id, second_slug) == []


# --- the tool is stoppable like any other (criterion 13) -------------------------


def test_the_tool_is_stopped_by_its_own_kill_switch(
    mcp_env: SeededMcp,  # noqa: F811
    mcp_redis_url: str,  # noqa: F811
    corpus_root: Path,
) -> None:
    """Criterion 13: this mutation is not special, and it is not protected.

    The switch is tripped through `set_kill_switch` rather than planted in
    Redis, which is what makes this two kills in one:

    * adding `reingest_policy_document` to
      `helpdesk.killswitch.change.PROTECTED_TOOLS` - the plan adds nothing to
      that frozenset - turns the trip itself into `POLICY_BLOCKED` and this
      case red. `PROTECTED_TOOLS` is compared against its exact contents as
      well, so the membership is pinned in both directions;
    * a tool wired around the pipeline (its own session, its own handler, a
      registry entry the switch stage never sees) answers the stopped call by
      re-ingesting, where every other registered tool answers `TOOL_DISABLED`.

    The trip is made by `platform_admin`, which holds the only cell
    `set_kill_switch` has, and the stopped call by `it_admin`, which holds one
    on the tool under test: a `FORBIDDEN` here would mean the matrix ran
    instead of the switch stage. The corpus file EXISTS and the key is fresh,
    so neither the missing-file refusal nor the guard can answer this row, and
    nothing is written.
    """
    from helpdesk.shared.killswitch import PROTECTED_TOOLS

    assert set(PROTECTED_TOOLS) == {SET_KILL_SWITCH}
    assert REINGEST not in PROTECTED_TOOLS

    slug = _slug()
    _plant_corpus_file(corpus_root, mcp_env, slug)
    tenant_id = mcp_env.tenant_a.tenant_id
    key = _tenant_key(tenant_id, REINGEST)

    try:
        trip = call_tool(
            _token(mcp_env, "platform_admin"),
            SET_KILL_SWITCH,
            {
                "tool_name": REINGEST,
                "enabled": True,
                "reason": f"fake reingest stop {uuid.uuid4().hex}",
                "scope": str(tenant_id),
            },
        )
        assert trip.is_error is False, trip.content

        stopped = call_tool(_token(mcp_env), REINGEST, _args(slug))

        assert error_code(stopped) == "TOOL_DISABLED"
        assert _versions(mcp_env.database_url, tenant_id, slug) == []
    finally:
        _delete_key(mcp_redis_url, key)


# --- what the record says -------------------------------------


def test_the_audit_event_type_is_policy_reingested(
    mcp_env: SeededMcp,  # noqa: F811
    mcp_policy_collections: tuple[str, str],  # noqa: F811
    corpus_root: Path,
) -> None:
    """Decision D, and a project rule: the event type, its subject and its
    actor, for one re-ingest.

    KILL: a registry entry declaring the generic `tool.succeeded`. The corpus
    is what every access decision cites, so a change to it is a governance
    record, and `search_audit` filtered on `policy.reingested` would answer
    nothing at all for a corpus that had been rewritten - the one question an
    auditor asks of this tool. The delta is the exact ordered list, so a
    duplicated event or a domain event appended beside the declared one fails
    too.

    The subject is the TOOL name, the shape every declared event type's
    registry entry has (the pipeline files the summary against the tool), and
    the actor is the caller's own user id: an event whose actor is the tenant
    answers the wrong question for an auditor asking who re-ingested and when.
    """
    slug = _slug()
    _plant_corpus_file(corpus_root, mcp_env, slug)
    tenant_id = mcp_env.tenant_a.tenant_id
    before = _audit_rows(mcp_env.database_url, tenant_id)

    result = call_tool(_token(mcp_env), REINGEST, _args(slug))

    assert result.is_error is False, result.content
    delta = _audit_rows(mcp_env.database_url, tenant_id)[len(before) :]

    assert [str(row.event_type) for row in delta] == [REINGESTED_EVENT]
    assert str(delta[0].subject_type) == "tool"
    assert str(delta[0].subject_id) == REINGEST
    assert str(delta[0].actor_ref) == str(mcp_env.tenant_a.user_id)


# --- the output (criterion 10) ---------------------------------------------------


def test_the_output_carries_all_seven_fields_and_a_summary(
    mcp_env: SeededMcp,  # noqa: F811
    mcp_policy_collections: tuple[str, str],  # noqa: F811
    corpus_root: Path,
) -> None:
    """Criterion 10, on the MODEL and on the wire.

    KILL: an output short of `chunk_count` or `created` - the two fields that
    say whether the ingest actually landed, which is the only reason decision
    B runs this synchronously at all; short of `is_current`, which is how the
    page shows that re-ingesting an older version made it live again
    (decision E); or short of `sha256`, the only evidence on this surface that
    the file on disk differed from the registered one. An output that WAS the
    summary - a bare sentence instead of a field beside the values - is caught
    by the same exact key set, and is what SPEC section 8.1 forbids.

    The values are asserted against the row the call wrote, not merely
    present: a handler echoing its own arguments back would satisfy a key-set
    check while reporting an ingest that never happened.
    """
    slug = _slug()
    _plant_corpus_file(corpus_root, mcp_env, slug)

    payload = _structured(call_tool(_token(mcp_env), REINGEST, _args(slug)))

    assert set(payload) == OUTPUT_FIELDS
    assert set(REGISTRY[REINGEST].output_model.model_fields) == OUTPUT_FIELDS

    row = _versions(mcp_env.database_url, mcp_env.tenant_a.tenant_id, slug)[0]
    assert payload["slug"] == slug
    assert payload["version"] == VERSION
    assert payload["created"] is True
    assert payload["is_current"] is True
    assert payload["sha256"] == str(row.sha256)
    assert payload["chunk_count"] == _chunk_count(
        mcp_env.database_url, mcp_env.tenant_a.tenant_id, slug
    )
    assert isinstance(payload["summary"], str)
    assert payload["summary"].strip() != ""
