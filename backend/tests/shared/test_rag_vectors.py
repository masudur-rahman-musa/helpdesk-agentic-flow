"""An earlier change §6: the per-tenant Qdrant collections in `helpdesk/rag/vectors.py`.

Expected implementation surface (the implementer must provide exactly this):

``helpdesk.core.settings.Settings``
    ``qdrant_timeout_seconds: float`` — the explicit timeout every constructed
    Qdrant client carries.

``helpdesk.rag.vectors``
    ``DENSE_VECTOR_NAME: str`` / ``SPARSE_VECTOR_NAME: str`` — the two named
        vectors of every policy collection.
    ``VectorStoreUnavailableError(ToolError)`` — ``code = ErrorCode.INTERNAL``;
        raised when Qdrant cannot be reached, carrying neither the store's own
        wording nor its URL.
    ``def collection_name(tenant_slug: str) -> str`` — ``policy_{tenant_slug}``,
        refusing ``InvalidPolicyNameError`` (``VALIDATION``) for a slug
        ``rag.naming.check_slug`` rejects or a name over
        ``rag.naming.COLLECTION_NAME_LENGTH``. Pure: builds no request.
    ``def create_client(settings: Settings) -> AsyncQdrantClient`` — the one
        place a Qdrant client is constructed, with ``qdrant_timeout_seconds``
        applied to it.
    ``async def ensure_collection(client, tenant_slug: str) -> None`` —
        idempotent; creates the dense vector at ``DENSE_DIMENSION`` under
        cosine distance and the sparse vector for the bm25 model.
    ``async def upsert_chunks(client, tenant_slug: str, *, tenant_id,
        document_id, document_slug: str, version: str,
        chunks: Sequence[Chunk]) -> list[UUID]`` —
        one point per chunk, both named vectors, the the design payload plus
        ``document_slug`` (see ``PAYLOAD_KEYS`` below),
        ``is_current=True``, and the point ids it wrote, in chunk order. The
        ids are deterministic in ``(tenant_id, document_id, chunk_index)``
        (decision B), so a second call overwrites rather than doubles.
    ``async def supersede_points(client, tenant_slug: str, *,
        document_slug: str, keep_document_id) -> None`` — sets
        ``is_current=false`` on the points of every OTHER version of that
        document slug and deletes nothing (decision D).

a project rule: the compose Qdrant is a shared backing service, never wiped
and never rebuilt per test. Per-run isolation is a collection NAME prefix —
`qdrant_namespace` mints one tenant-slug prefix for the whole session, so
every collection this suite touches is `policy_{prefix}-...` and no case can
see another's points. The client itself is constructed inside each case's
`asyncio.run`, for the reason `test_policy_models.py` builds its engine there:
every case drives its own event loop and a pooled connection does not survive
one closing.

No pytest-asyncio: each case is a sync function driving one `asyncio.run`.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, cast

import httpx
import pytest
from pydantic import ValidationError
from qdrant_client import AsyncQdrantClient

from helpdesk.shared.core.settings import Settings, load_settings
from helpdesk.shared.errors import ErrorCode, ToolError
from helpdesk.shared.rag.chunking import Chunk
from helpdesk.shared.rag.embedding import DENSE_DIMENSION, SparseVector
from helpdesk.shared.rag.naming import COLLECTION_NAME_LENGTH, InvalidPolicyNameError
from helpdesk.shared.rag.vectors import (
    DENSE_VECTOR_NAME,
    SPARSE_VECTOR_NAME,
    VectorStoreUnavailableError,
    collection_name,
    create_client,
    ensure_collection,
    supersede_points,
    upsert_chunks,
)

# the design's payload plus `document_slug`. a project rule: the set is
# pinned as a value, not as "at least these keys" — a payload with an extra
# field is a field row 15's filters would not know about.
#
# The seventh key is an earlier change §1's two criteria resolved against each other:
# the payload was fixed as the SPEC six while `supersede_points` was fixed to
# find "every other version of that document slug" from inside the collection,
# where `document_id` differs per version and nothing else names the document
# (the project notes 2026-09-08, a project rule). Pinned as an exact set here, so
# an eighth key fails just as a missing one does.
PAYLOAD_KEYS = frozenset(
    {
        "document_id",
        "document_slug",
        "version",
        "is_current",
        "heading_path",
        "chunk_index",
        "text",
    }
)

# A port nothing listens on: `connect` is refused immediately, so the
# unreachable-store case costs no timeout wait.
CLOSED_PORT_URL = "http://127.0.0.1:1"

_UNREACHABLE_MSG = (
    "Qdrant is unreachable at the configured QDRANT_URL.\n"
    "Plan 14c tests require the compose stack: run `docker compose up -d` first.\n"
    "Original error: {error!r}"
)


# --- the shared, session-scoped backing service -------------


def _settings() -> Settings:
    try:
        return load_settings()
    except ValidationError as exc:
        pytest.fail(
            f"QDRANT_URL is not configured (env or .env). Copy .env.example to .env.\n{exc}",
            pytrace=False,
        )


@pytest.fixture(scope="session")
def qdrant_url() -> str:
    """Connectivity gate: fails loudly (never skips) when Qdrant is down.

    A silent skip would let the whole of the design's isolation go unproven
    on a green run, which is the one thing this row exists to show.
    """
    settings = _settings()
    url = settings.qdrant_url.rstrip("/")
    try:
        response = httpx.get(f"{url}/collections", timeout=5.0)
        response.raise_for_status()
    except Exception as exc:
        pytest.fail(_UNREACHABLE_MSG.format(error=exc), pytrace=False)
    return url


@pytest.fixture(scope="session")
def qdrant_namespace(qdrant_url: str) -> Any:
    """One tenant-slug prefix for the whole run; its collections are dropped after.

    The instance is shared with whatever else uses it and is never wiped: isolation is the name, so only collections this run
    created are deleted, matched on the prefix.
    """
    prefix = f"qtest-{uuid.uuid4().hex[:12]}"
    yield prefix
    listed = httpx.get(f"{qdrant_url}/collections", timeout=10.0).json()
    for entry in listed["result"]["collections"]:
        name = str(entry["name"])
        if name.startswith(f"policy_{prefix}"):
            httpx.delete(f"{qdrant_url}/collections/{name}", timeout=30.0)


def _slug(namespace: str, suffix: str) -> str:
    """A tenant slug inside this run's namespace."""
    return f"{namespace}-{suffix}"


def _drive[T](work: Callable[[AsyncQdrantClient], Awaitable[T]]) -> T:
    """Run one async scenario against a client built inside its own loop."""

    async def scenario() -> T:
        client = create_client(_settings())
        try:
            return await work(client)
        finally:
            await client.close()

    return asyncio.run(scenario())


# --- fake chunks --------------------------------------------------------------


def _dense(seed: int) -> tuple[float, ...]:
    """A deterministic dense vector of the model's own width.

    Qdrant rejects a vector of any other length, so the width is read from
    `DENSE_DIMENSION` rather than chosen here.
    """
    return tuple(((seed + index) % 17) / 17.0 + 0.01 for index in range(DENSE_DIMENSION))


def _chunk(index: int, text: str) -> Chunk:
    return Chunk(
        heading_path=("1. Purpose",),
        char_start=index * 100,
        char_end=index * 100 + len(text),
        text=text,
        dense=_dense(index),
        sparse=SparseVector(indices=(1, 5, 9), values=(0.4, 0.3, 0.3)),
    )


FAKE_CHUNKS: tuple[Chunk, ...] = (
    _chunk(0, "Fake access to the finance system is granted by its owner."),
    _chunk(1, "Fake badge loss is reported to reception the same day."),
)


class RecordingClient:
    """A stand-in that records every call a request would go through.

    Used only by the cases that must prove NO request was built: any method a
    refusal could have reached appends its name, so "the client was never
    called" is an assertion rather than an inference.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def collection_exists(self, *args: object, **kwargs: object) -> bool:
        self.calls.append("collection_exists")
        return True

    async def create_collection(self, *args: object, **kwargs: object) -> bool:
        self.calls.append("create_collection")
        return True

    async def get_collection(self, *args: object, **kwargs: object) -> object:
        self.calls.append("get_collection")
        return None

    async def upsert(self, *args: object, **kwargs: object) -> object:
        self.calls.append("upsert")
        return None

    async def set_payload(self, *args: object, **kwargs: object) -> object:
        self.calls.append("set_payload")
        return None

    async def scroll(self, *args: object, **kwargs: object) -> object:
        self.calls.append("scroll")
        return ([], None)

    async def count(self, *args: object, **kwargs: object) -> object:
        self.calls.append("count")
        return None


def _as_client(recording: RecordingClient) -> AsyncQdrantClient:
    return cast(AsyncQdrantClient, recording)


def _payload(point: Any) -> dict[str, Any]:
    """The point's payload, asserted present rather than narrowed silently.

    `scroll` types the payload optional; a point written with none at all is
    itself a defect these cases must see, so the absence fails here with the
    point named instead of as a `None` subscript further down.
    """
    assert point.payload is not None, f"point {point.id} carries no payload"
    return dict(point.payload)


# --- collection_name, at the boundary and one over ----------------------------


def test_the_collection_name_is_the_prefixed_tenant_slug() -> None:
    """Catches a name built from anything but `policy_` + the tenant slug —
    a per-tenant collection whose name does not name the tenant is a
    collection row 15 would have to guess at."""
    assert collection_name("nordlicht") == "policy_nordlicht"


def test_a_tenant_slug_at_exactly_the_slug_cap_is_accepted() -> None:
    """Catches an exclusive comparison in either gate: the 64-character slug
    is the longest `check_slug` accepts and its collection name is exactly
    `COLLECTION_NAME_LENGTH`, so a `<` anywhere on this path refuses the
    longest tenant the store can actually hold."""
    slug = ("fake-tenant-" * 6)[:64].rstrip("-").ljust(64, "x")
    assert len(slug) == 64
    name = collection_name(slug)
    assert len(name) == COLLECTION_NAME_LENGTH


def test_a_tenant_slug_one_over_the_slug_cap_builds_no_request(
    qdrant_namespace: str,
) -> None:
    """Catches a `collection_name` that formats the slug into a URL path
    segment with no pre-check: the 65-character slug is refused typed
    VALIDATION, and the recording client proves the refusal happened before
    any request was built rather than after a 4xx came back."""
    recording = RecordingClient()
    slug = ("fake-tenant-" * 6)[:65].rstrip("-").ljust(65, "x")
    assert len(slug) == 65

    with pytest.raises(InvalidPolicyNameError) as excinfo:
        collection_name(slug)
    assert excinfo.value.code is ErrorCode.VALIDATION

    with pytest.raises(ToolError):
        asyncio.run(ensure_collection(_as_client(recording), slug))
    assert recording.calls == [], "the refused slug reached the store"
    assert qdrant_namespace  # the namespace fixture keeps cleanup honest


def test_a_tenant_slug_that_is_not_a_lowercase_hyphen_slug_builds_no_request() -> None:
    """Catches a `collection_name` that only measures length: `Nordlicht Ltd`
    is well inside every cap, so only `check_slug`'s pattern can refuse it,
    and without it the space and the uppercase reach a collection name that
    can never be reproduced from the tenant row."""
    recording = RecordingClient()
    with pytest.raises(InvalidPolicyNameError) as excinfo:
        collection_name("Nordlicht Ltd")
    assert excinfo.value.code is ErrorCode.VALIDATION

    with pytest.raises(ToolError):
        asyncio.run(
            upsert_chunks(
                _as_client(recording),
                "Nordlicht Ltd",
                tenant_id=uuid.uuid4(),
                document_id=uuid.uuid4(),
                document_slug="fake-access-control-policy",
                version="v1",
                chunks=FAKE_CHUNKS,
            )
        )
    assert recording.calls == [], "the refused slug reached the store"


# --- the constructed client --------------------------------


def test_the_constructed_client_carries_the_configured_timeout() -> None:
    """Catches a client built with the library's own default timeout while a
    `qdrant_timeout_seconds` constant sits unused beside it: asserted on the
    CONSTRUCTED client, so a setting that is read but never passed fails
    here."""
    settings = _settings().model_copy(update={"qdrant_timeout_seconds": 12.0})
    client = create_client(settings)
    try:
        inner: Any = client._client
        assert float(inner._timeout) == 12.0
    finally:
        asyncio.run(client.close())


def test_the_settings_default_timeout_is_exactly_the_planned_value() -> None:
    """Catches a `qdrant_timeout_seconds` default of any other number, or one
    typed loosely enough to arrive as a string (a project rule pins the value
    exactly; a band would admit a wrong one)."""
    assert _settings().qdrant_timeout_seconds == 30.0


# --- ensure_collection --------------------------------------------------------


def test_ensure_collection_creates_both_named_vectors(qdrant_namespace: str) -> None:
    """Catches a collection created with a single unnamed vector, with the
    wrong dense width, or under a distance other than cosine — each of which
    makes row 15's hybrid search impossible to add without a re-ingest, and
    the wrong width makes the very first upsert fail with nothing to explain
    it."""
    slug = _slug(qdrant_namespace, "ensure")

    async def work(client: AsyncQdrantClient) -> Any:
        await ensure_collection(client, slug)
        return await client.get_collection(collection_name(slug))

    info = _drive(work)
    vectors = info.config.params.vectors
    assert DENSE_VECTOR_NAME in vectors
    assert vectors[DENSE_VECTOR_NAME].size == DENSE_DIMENSION
    assert str(vectors[DENSE_VECTOR_NAME].distance).lower().endswith("cosine")
    assert SPARSE_VECTOR_NAME in (info.config.params.sparse_vectors or {})


def test_ensure_collection_twice_leaves_exactly_one_collection(
    qdrant_namespace: str,
) -> None:
    """Catches an `ensure_collection` that calls `create_collection`
    unconditionally: the second call then raises a 409 (or, with
    `recreate_collection`, silently drops every point of an existing tenant)
    and re-ingesting a corpus becomes a destructive operation."""
    slug = _slug(qdrant_namespace, "twice")

    async def work(client: AsyncQdrantClient) -> list[str]:
        await ensure_collection(client, slug)
        await ensure_collection(client, slug)
        listed = await client.get_collections()
        return [entry.name for entry in listed.collections]

    names = _drive(work)
    assert names.count(collection_name(slug)) == 1


def test_ensure_collection_a_second_time_keeps_the_points_already_written(
    qdrant_namespace: str,
) -> None:
    """Catches an `ensure_collection` implemented as `recreate_collection`,
    which the previous case cannot see (one collection exists either way):
    only the surviving point count separates "created if missing" from
    "dropped and rebuilt"."""
    slug = _slug(qdrant_namespace, "keeps")
    document_id = uuid.uuid4()

    async def work(client: AsyncQdrantClient) -> int:
        await ensure_collection(client, slug)
        await upsert_chunks(
            client,
            slug,
            tenant_id=uuid.uuid4(),
            document_id=document_id,
            document_slug="fake-access-control-policy",
            version="v1",
            chunks=FAKE_CHUNKS,
        )
        await ensure_collection(client, slug)
        counted = await client.count(collection_name(slug), exact=True)
        return int(counted.count)

    assert _drive(work) == len(FAKE_CHUNKS)


# --- upsert_chunks ------------------------------------------------------------


def test_the_upserted_payload_is_exactly_the_spec_key_set(qdrant_namespace: str) -> None:
    """Catches a payload missing `is_current` (row 15's current-version filter
    then matches every version at once) or carrying an extra field SPEC
    §12.1.5 does not name; the key set is asserted as an exact value, so both
    directions fail here."""
    slug = _slug(qdrant_namespace, "payload")
    document_id = uuid.uuid4()

    async def work(client: AsyncQdrantClient) -> list[Any]:
        await ensure_collection(client, slug)
        await upsert_chunks(
            client,
            slug,
            tenant_id=uuid.uuid4(),
            document_id=document_id,
            document_slug="fake-access-control-policy",
            version="v2",
            chunks=FAKE_CHUNKS,
        )
        points, _ = await client.scroll(
            collection_name(slug), limit=10, with_payload=True, with_vectors=True
        )
        return list(points)

    points = _drive(work)
    assert len(points) == len(FAKE_CHUNKS)
    by_index = {int(_payload(point)["chunk_index"]): _payload(point) for point in points}
    assert sorted(by_index) == [0, 1]
    for index, payload in by_index.items():
        assert frozenset(payload) == PAYLOAD_KEYS
        assert payload["document_id"] == str(document_id)
        assert payload["document_slug"] == "fake-access-control-policy"
        assert payload["version"] == "v2"
        assert payload["is_current"] is True
        assert payload["heading_path"] == ["1. Purpose"]
        assert payload["text"] == FAKE_CHUNKS[index].text


def test_every_point_carries_both_named_vectors(qdrant_namespace: str) -> None:
    """Catches an upsert that writes the dense vector only: the sparse half is
    silently absent, the collection looks healthy, and row 15's lexical search
    returns nothing for every query without any error to trace."""
    slug = _slug(qdrant_namespace, "vectors")

    async def work(client: AsyncQdrantClient) -> list[Any]:
        await ensure_collection(client, slug)
        await upsert_chunks(
            client,
            slug,
            tenant_id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            document_slug="fake-access-control-policy",
            version="v1",
            chunks=FAKE_CHUNKS,
        )
        points, _ = await client.scroll(
            collection_name(slug), limit=10, with_payload=True, with_vectors=True
        )
        return list(points)

    for point in _drive(work):
        assert DENSE_VECTOR_NAME in point.vector
        assert len(point.vector[DENSE_VECTOR_NAME]) == DENSE_DIMENSION
        assert SPARSE_VECTOR_NAME in point.vector


def test_re_upserting_the_same_document_returns_the_same_ids_and_no_new_points(
    qdrant_namespace: str,
) -> None:
    """Catches decision B's rejected alternative, `uuid4` point ids: a random
    id makes the second upsert a SECOND set of points (count doubles) and
    returns ids that no longer match the `policy_chunks` rows written beside
    the first set."""
    slug = _slug(qdrant_namespace, "deterministic")
    tenant_id = uuid.uuid4()
    document_id = uuid.uuid4()

    async def work(client: AsyncQdrantClient) -> tuple[list[uuid.UUID], list[uuid.UUID], int]:
        await ensure_collection(client, slug)
        first = await upsert_chunks(
            client,
            slug,
            tenant_id=tenant_id,
            document_id=document_id,
            document_slug="fake-access-control-policy",
            version="v1",
            chunks=FAKE_CHUNKS,
        )
        second = await upsert_chunks(
            client,
            slug,
            tenant_id=tenant_id,
            document_id=document_id,
            document_slug="fake-access-control-policy",
            version="v1",
            chunks=FAKE_CHUNKS,
        )
        counted = await client.count(collection_name(slug), exact=True)
        return list(first), list(second), int(counted.count)

    first, second, count = _drive(work)
    assert first == second
    assert len(set(first)) == len(FAKE_CHUNKS)
    assert count == len(FAKE_CHUNKS)


def test_two_documents_in_one_tenant_get_different_point_ids(
    qdrant_namespace: str,
) -> None:
    """Catches a point id derived from `(tenant_id, chunk_index)` only —
    deterministic, and therefore passing the case above, while the second
    document silently overwrites the first document's points chunk for
    chunk."""
    slug = _slug(qdrant_namespace, "two-documents")
    tenant_id = uuid.uuid4()

    async def work(client: AsyncQdrantClient) -> tuple[list[uuid.UUID], list[uuid.UUID], int]:
        await ensure_collection(client, slug)
        first = await upsert_chunks(
            client,
            slug,
            tenant_id=tenant_id,
            document_id=uuid.uuid4(),
            document_slug="fake-access-control-policy",
            version="v1",
            chunks=FAKE_CHUNKS,
        )
        second = await upsert_chunks(
            client,
            slug,
            tenant_id=tenant_id,
            document_id=uuid.uuid4(),
            document_slug="fake-joiner-mover-leaver",
            version="v1",
            chunks=FAKE_CHUNKS,
        )
        counted = await client.count(collection_name(slug), exact=True)
        return list(first), list(second), int(counted.count)

    first, second, count = _drive(work)
    assert set(first).isdisjoint(second)
    assert count == 2 * len(FAKE_CHUNKS)


# --- supersede_points ---------------------------------------------------------


def test_superseding_flips_the_other_version_and_deletes_nothing(
    qdrant_namespace: str,
) -> None:
    """Catches decision D implemented as a delete: the point count is asserted
    identical before and after, so an implementation that removes the old
    version's points (losing the design's audit trail) fails even though
    every remaining point would look correct."""
    slug = _slug(qdrant_namespace, "supersede")
    tenant_id = uuid.uuid4()
    document_slug = "fake-access-control-policy"
    old_id, new_id = uuid.uuid4(), uuid.uuid4()

    async def work(client: AsyncQdrantClient) -> tuple[int, int, dict[str, list[bool]]]:
        await ensure_collection(client, slug)
        for document_id, version in ((old_id, "v1"), (new_id, "v2")):
            await upsert_chunks(
                client,
                slug,
                tenant_id=tenant_id,
                document_id=document_id,
                document_slug=document_slug,
                version=version,
                chunks=FAKE_CHUNKS,
            )
        before = int((await client.count(collection_name(slug), exact=True)).count)

        await supersede_points(client, slug, document_slug=document_slug, keep_document_id=new_id)

        after = int((await client.count(collection_name(slug), exact=True)).count)
        points, _ = await client.scroll(collection_name(slug), limit=100, with_payload=True)
        flags: dict[str, list[bool]] = {}
        for point in points:
            payload = _payload(point)
            flags.setdefault(str(payload["document_id"]), []).append(bool(payload["is_current"]))
        return before, after, flags

    before, after, flags = _drive(work)
    assert before == after == 2 * len(FAKE_CHUNKS)
    assert flags[str(old_id)] == [False] * len(FAKE_CHUNKS)
    assert flags[str(new_id)] == [True] * len(FAKE_CHUNKS)


def test_superseding_leaves_another_documents_points_current(
    qdrant_namespace: str,
) -> None:
    """Catches a supersede that flips every point whose `document_id` is not
    the kept one: the second document in the same tenant collection is a
    different policy entirely, and clearing its `is_current` makes it
    unretrievable — a case the previous test (one slug only) cannot see."""
    slug = _slug(qdrant_namespace, "supersede-other")
    tenant_id = uuid.uuid4()
    kept_id, other_id = uuid.uuid4(), uuid.uuid4()

    async def work(client: AsyncQdrantClient) -> list[bool]:
        await ensure_collection(client, slug)
        await upsert_chunks(
            client,
            slug,
            tenant_id=tenant_id,
            document_id=kept_id,
            document_slug="fake-joiner-mover-leaver",
            version="v1",
            chunks=FAKE_CHUNKS,
        )
        await upsert_chunks(
            client,
            slug,
            tenant_id=tenant_id,
            document_id=other_id,
            document_slug="fake-acceptable-use",
            version="v1",
            chunks=FAKE_CHUNKS,
        )
        await supersede_points(
            client,
            slug,
            document_slug="fake-joiner-mover-leaver",
            keep_document_id=kept_id,
        )
        points, _ = await client.scroll(collection_name(slug), limit=100, with_payload=True)
        return [
            bool(_payload(point)["is_current"])
            for point in points
            if str(_payload(point)["document_id"]) == str(other_id)
        ]

    flags = _drive(work)
    assert flags == [True] * len(FAKE_CHUNKS)


# --- the unreachable store ----------------------------------------------------


def test_an_unreachable_qdrant_is_typed_internal_without_naming_the_store() -> None:
    """Catches a `vectors.py` that lets `qdrant_client`'s own exception escape
    to the caller: the raw error carries the URL and the store's name, which
    is internal infrastructure in a message a tool boundary forwards, and it
    arrives under no the design code at all."""
    settings = _settings().model_copy(
        update={"qdrant_url": CLOSED_PORT_URL, "qdrant_timeout_seconds": 2.0}
    )

    async def scenario() -> str:
        client = create_client(settings)
        try:
            with pytest.raises(VectorStoreUnavailableError) as excinfo:
                await ensure_collection(client, "fake-unreachable-tenant")
            assert excinfo.value.code is ErrorCode.INTERNAL
            return str(excinfo.value)
        finally:
            await client.close()

    message = asyncio.run(scenario())
    lowered = message.lower()
    assert "qdrant" not in lowered
    assert "127.0.0.1" not in message
    assert "http" not in lowered
