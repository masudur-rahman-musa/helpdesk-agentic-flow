"""The two notification tools, through the transport.

Surface under test (written before it exists):

``mcp_server.tools.notifications``
    ``LIMIT_MIN = 1``, ``LIMIT_DEFAULT = 50``, ``LIMIT_MAX = 200``.
    ``ListNotificationsInput`` — a plain ``BaseModel`` (``extra="ignore"``,
    the read tools' contract) with ``unread_only: bool = False`` and
    ``limit: int = LIMIT_DEFAULT`` bounded ``ge=LIMIT_MIN, le=LIMIT_MAX``.
    NO user field at all: the caller is the token's user.
    ``NotificationRow`` — exactly eight fields, in this order:
    ``notification_id``, ``kind``, ``title``, ``body``, ``subject_type``,
    ``subject_id``, ``created_at``, ``read_at``.
    ``ListNotificationsOutput`` — exactly ``notifications: list[NotificationRow]``
    and ``unread_count: int``.
    ``MarkNotificationReadInput(MutatingToolInput)`` — the single field
    ``notification_id: uuid.UUID``, so its keys are exactly
    ``idempotency_key`` and ``notification_id``.
    ``MarkNotificationReadOutput`` — exactly four fields:
    ``notification_id``, ``kind``, ``title``, ``read_at``.
    ``list_notifications`` (a READ: registry ``event_type`` the generic
    ``tool.succeeded``, ``mutating=False``, no idempotency key) and
    ``mark_notification_read`` (MUTATING, the same generic event type —
    it adds no event type, because "a person opened their own bell"
    answers no auditor's question).

``helpdesk.auth.rbac``
    ``Tool.LIST_NOTIFICATIONS`` and ``Tool.MARK_NOTIFICATION_READ``, each
    ``self_only`` for all seven principals: a
    notification is addressed to one person and read by that person.

Refusals: ``NOT_FOUND`` for a `notification_id` belonging to another user of
this tenant, to another tenant, or to nobody (the read is
filtered by the token's user, so all three are one answer and the id stays a
non-capability); ``VALIDATION`` for a `limit` outside the inclusive bounds
and for an over-long idempotency key. An EMPTY bell is never a refusal.

Fixture cost: everything rides the session-scoped
``mcp_fixtures`` seam — one seeded throwaway database, one dedicated Redis
index flushed per test. Per-test isolation inside the shared database is a
namespace: every case plants its OWN recipient (a uuid4-suffixed user in one
of the two seeded tenants) and its own notifications, and sends a fresh
idempotency key per call, so no case can see another's rows, another's unread
count or another's audit events. The tenants are resolved from
``env.tenant_a``/``env.tenant_b`` rather than by slug, and callers are minted
for the principal a case needs rather than picked by index.

No pytest-asyncio: each case is a sync function driving one ``asyncio.run``.
"""

import asyncio
import uuid
from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

import helpdesk.mcp_server.tools.notifications as notifications
from helpdesk.shared.models.idempotency import KEY_LENGTH
from helpdesk.shared.models.notification import NotificationKind
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

LIST = "list_notifications"
MARK = "mark_notification_read"

# The audit contract as literals, never imports: reading these strings out of
# the module under test would let a rename pass unnoticed.
SUCCEEDED_EVENT = "tool.succeeded"
REPLAYED_EVENT = "tool.replayed"
# The scope the matrix gives both rows, as it lands on the audit payload.
SELF_ONLY_SCOPE = "self_only"

# All seven principals: both rows are `self_only` for every one of them, so
# every case that sweeps this axis asserts an ALLOW in each of the seven.
PRINCIPALS: tuple[str, ...] = (
    "employee",
    "manager",
    "system_owner",
    "hr",
    "it_admin",
    "auditor",
    "platform_admin",
)

# Fixed instants, never `now()`: an order or a count that depends on when the
# suite runs is a flaky test rather than a fixture.
OLDER = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
NEWER = datetime(2026, 4, 1, 9, 0, tzinfo=UTC)
ALREADY_READ_AT = datetime(2026, 2, 2, 8, 0, tzinfo=UTC)

TITLE = "fake notification title"
BODY = "fake notification body for the mcp suite"
SUBJECT_TYPE = "grant"
KIND = NotificationKind.GRANT_EXPIRED.value

ACTIVE_STATUS = "active"
PLANTED_START = date(2019, 3, 7)
# Not a hash and not a secret: these users never authenticate, tokens are
# minted directly. A literal marker makes that impossible to mistake for a
# credential.
PLANTED_PASSWORD_HASH = "not-a-real-hash-this-user-never-logs-in"

# The row shape and envelope, written out as literals: a
# key set read off the model under test would assert nothing.
ROW_KEYS = {
    "notification_id",
    "kind",
    "title",
    "body",
    "subject_type",
    "subject_id",
    "created_at",
    "read_at",
}
ENVELOPE_KEYS = {"notifications", "unread_count"}
MARK_OUTPUT_KEYS = {"notification_id", "kind", "title", "read_at"}


def _key() -> str:
    """A unique idempotency key: half the per-test namespace."""
    return f"fake-key-{uuid.uuid4().hex}"


# --- raw-SQL probes (reading and writing around the tools, never through them) --


def _rows(database_url: str, sql: str, params: dict[str, Any]) -> list[Any]:
    async def scenario() -> list[Any]:
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as conn:
                return list(await conn.execute(text(sql), params))
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _execute(database_url: str, sql: str, params: dict[str, Any]) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as conn:
                await conn.execute(text(sql), params)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def _plant_user(env: SeededMcp, *, tenant: SeededUser) -> SeededUser:
    """One fresh, token-ready recipient in the named seeded tenant.

    The per-case namespace: a recipient nobody else plants
    into means this case's `unread_count`, row set and audit events are exact
    in a database every other case shares. The tenant and department come off
    the fixture's own handle, never off a slug pinned here.
    """
    user_id = uuid.uuid4()
    _execute(
        env.database_url,
        "INSERT INTO users (id, tenant_id, department_id, email, display_name, password_hash,"
        " principals, status, start_date, end_date, created_at, updated_at)"
        " VALUES (:id, :tenant, :department, :email, :display_name, :password_hash,"
        " :principals, :status, :start_date, NULL, now(), now())",
        {
            "id": user_id,
            "tenant": tenant.tenant_id,
            "department": tenant.department_id,
            "email": f"fake.recipient.{uuid.uuid4().hex}@fake.invalid",
            "display_name": "Fake Recipient",
            "password_hash": PLANTED_PASSWORD_HASH,
            "principals": ["employee"],
            "status": ACTIVE_STATUS,
            # A `date` object, never the string: asyncpg binds by type and
            # does not coerce (a known trap).
            "start_date": PLANTED_START,
        },
    )
    return SeededUser(
        tenant_id=tenant.tenant_id,
        tenant_slug=tenant.tenant_slug,
        user_id=user_id,
        email=f"fake.recipient.{user_id}@fake.invalid",
        display_name="Fake Recipient",
        department_id=tenant.department_id,
        principals=("employee",),
        status=ACTIVE_STATUS,
    )


def _plant_notification(
    env: SeededMcp,
    *,
    recipient: SeededUser,
    created_at: datetime = NEWER,
    read_at: datetime | None = None,
    title: str = TITLE,
    subject_id: str | None = None,
) -> uuid.UUID:
    """One committed `notifications` row, written around the tools.

    `notify` is the product's writer, but it leaves `created_at` to the server
    clock and `read_at` null; these cases need a controlled order and rows
    that are already read.
    """
    notification_id = uuid.uuid4()
    _execute(
        env.database_url,
        "INSERT INTO notifications (id, tenant_id, user_id, kind, title, body, subject_type,"
        " subject_id, read_at, created_at, updated_at)"
        " VALUES (:id, :tenant, :user, :kind, :title, :body, :subject_type, :subject_id,"
        " :read_at, :created_at, now())",
        {
            "id": notification_id,
            "tenant": recipient.tenant_id,
            "user": recipient.user_id,
            "kind": KIND,
            "title": title,
            "body": BODY,
            "subject_type": SUBJECT_TYPE,
            "subject_id": subject_id,
            # `datetime` objects, never strings (same reason as above).
            "read_at": read_at,
            "created_at": created_at,
        },
    )
    return notification_id


def _read_at_of(env: SeededMcp, notification_id: uuid.UUID) -> Any:
    """The stored `read_at` of one row, read around the tools."""
    rows = _rows(
        env.database_url,
        "SELECT read_at FROM notifications WHERE id = :id",
        {"id": notification_id},
    )
    assert len(rows) == 1, f"expected exactly one notification row, got {len(rows)}"
    return rows[0][0]


def _events_of(env: SeededMcp, *, actor: SeededUser) -> list[Any]:
    """Every audit event this caller left, oldest first.

    Filtered by `actor_ref`, not only by tenant: the seeded database is shared
    and "exactly one event per call" is only exact for a freshly planted
    caller nobody else touches.
    """
    return _rows(
        env.database_url,
        "SELECT event_type, subject_type, subject_id, actor_ref, payload FROM audit_events"
        " WHERE tenant_id = :tenant AND actor_ref = :actor ORDER BY seq",
        {"tenant": actor.tenant_id, "actor": str(actor.user_id)},
    )


def _structured(result: Any) -> dict[str, Any]:
    assert result.is_error is False, result.content
    assert result.structured_content is not None, "a successful call carries structured content"
    content: dict[str, Any] = result.structured_content
    return content


def _response_text(result: Any) -> str:
    """The single text block of a successful result, for byte-identity checks."""
    assert result.is_error is False, result.content
    assert len(result.content) == 1, f"expected exactly one content block, got {result.content!r}"
    return str(result.content[0].text)


def _ids(payload: dict[str, Any]) -> list[uuid.UUID]:
    return [uuid.UUID(str(row["notification_id"])) for row in payload["notifications"]]


def _token(recipient: SeededUser, principal: str = "employee") -> dict[str, str]:
    """A bearer header for this recipient carrying ONLY the named principal."""
    return bearer(token_for(recipient, principals=(principal,)))


# --- the list: the shape of the answer ----------------------------------------


@pytest.mark.parametrize("principal", PRINCIPALS)
def test_each_principal_reads_its_own_bell_as_the_eight_key_row_and_two_key_envelope(
    mcp_env: SeededMcp,  # noqa: F811
    principal: str,
) -> None:
    """Per principal (`/notifications` belongs to
    everyone, so all seven cells are an ALLOW).

    Catches three things. A missing matrix cell for any one principal — an
    `employee` or an `auditor` copied off a review row by habit would be
    refused FORBIDDEN here and their bell would never load. A row projection
    that drops, renames or adds a field: without `read_at` the bell cannot
    tell read from unread, and an extra column (a `user_id`, the tenant) would
    put data on the wire the tool never promised. And an envelope shaped as a bare
    list, which leaves the count with nowhere to ride and forces a second
    tool.
    """
    recipient = _plant_user(mcp_env, tenant=mcp_env.tenant_a)
    subject_id = str(uuid.uuid4())
    planted = _plant_notification(mcp_env, recipient=recipient, subject_id=subject_id)

    payload = _structured(call_tool(_token(recipient, principal), LIST, {}))

    assert set(payload) == ENVELOPE_KEYS
    assert len(payload["notifications"]) == 1
    row = payload["notifications"][0]
    assert set(row) == ROW_KEYS
    assert uuid.UUID(str(row["notification_id"])) == planted
    assert row["kind"] == KIND
    assert row["title"] == TITLE
    assert row["body"] == BODY
    assert row["subject_type"] == SUBJECT_TYPE
    assert row["subject_id"] == subject_id
    assert row["read_at"] is None
    assert payload["unread_count"] == 1


def test_the_list_input_declares_no_user_field_at_all(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """The first half, as a literal key set.

    Catches a `user_id` (or a `tenant_id`) added to the read's schema: a field
    on the model is a field the wire advertises and a client will start
    sending, and identity has no argument path at all. The
    two fields that ARE there are the filter and the page size.
    """
    assert set(notifications.ListNotificationsInput.model_fields) == {"unread_only", "limit"}


def test_a_user_id_in_the_arguments_is_ignored_and_the_token_decides_whose_bell(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """With the tenant guard taken out of the picture: the caller
    and the person named in the arguments are in the SAME tenant,
    and BOTH hold a real unread notification.

    So an implementation that honoured the argument — or fell back to it —
    answers the colleague's message and its count rather than being saved by
    tenant scoping. A cross-tenant version of this case passes whether the
    rule holds or not, because honouring a foreign id merely misses and can
    silently fall back to the token user. The colleague's row is planted first
    and asserted unread, so "absent" cannot mean "never existed".
    """
    caller = _plant_user(mcp_env, tenant=mcp_env.tenant_a)
    colleague = _plant_user(mcp_env, tenant=mcp_env.tenant_a)
    assert caller.tenant_id == colleague.tenant_id, "the point of this test"
    assert caller.user_id != colleague.user_id
    theirs = _plant_notification(mcp_env, recipient=colleague)
    mine = _plant_notification(mcp_env, recipient=caller)
    assert _read_at_of(mcp_env, theirs) is None

    payload = _structured(call_tool(_token(caller), LIST, {"user_id": str(colleague.user_id)}))

    assert _ids(payload) == [mine]
    assert theirs not in _ids(payload)
    assert payload["unread_count"] == 1


def test_a_colleagues_notifications_are_absent_even_with_no_argument(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Through the transport, without the argument the case above
    sends: the handler's own read must be filtered by the token's user.

    Catches a handler filtered by TENANT alone — the filter the session guard
    already gives for free — which would show every colleague of the tenant in
    one person's bell and count them all in the badge.
    """
    caller = _plant_user(mcp_env, tenant=mcp_env.tenant_a)
    colleague = _plant_user(mcp_env, tenant=mcp_env.tenant_a)
    theirs = _plant_notification(mcp_env, recipient=colleague)
    mine = _plant_notification(mcp_env, recipient=caller)

    payload = _structured(call_tool(_token(caller), LIST, {}))

    assert _ids(payload) == [mine]
    assert theirs not in _ids(payload)
    assert payload["unread_count"] == 1


@pytest.mark.parametrize("principal", PRINCIPALS)
def test_another_tenants_notifications_are_invisible_to_every_principal(
    mcp_env: SeededMcp,  # noqa: F811
    principal: str,
) -> None:
    """The isolation row for this table, on all seven cells: every one
    of them is `self_only`, so no principal — `platform_admin` included — may
    read across the tenant boundary.

    The foreign row is REAL and unread, so the refusal has to come from the
    tenant filter rather than from an id nobody could resolve. The COUNT is
    asserted as well as the row set, because the unread total is a separate
    aggregate and a `func.count` over a hand-written query is exactly where
    the tenant filter slips past the ORM guard.
    """
    caller = _plant_user(mcp_env, tenant=mcp_env.tenant_a)
    foreigner = _plant_user(mcp_env, tenant=mcp_env.tenant_b)
    foreign_row = _plant_notification(mcp_env, recipient=foreigner)
    mine = _plant_notification(mcp_env, recipient=caller)
    assert _read_at_of(mcp_env, foreign_row) is None
    assert foreigner.tenant_id != caller.tenant_id

    payload = _structured(call_tool(_token(caller, principal), LIST, {}))

    assert _ids(payload) == [mine]
    assert foreign_row not in _ids(payload)
    assert payload["unread_count"] == 1


def test_an_empty_bell_answers_an_empty_page_and_zero_rather_than_not_found(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """The exact envelope `{"notifications": [], "unread_count": 0}`.

    Catches a handler that treats "nothing to show" as a refusal: the list
    takes no id, so there is nothing to be missing, and a `NOT_FOUND` here
    would make the notifications page of every new joiner an error screen. A
    colleague's unread row is planted in the same tenant so the empty answer
    cannot be the read returning nothing to everybody.
    """
    caller = _plant_user(mcp_env, tenant=mcp_env.tenant_a)
    colleague = _plant_user(mcp_env, tenant=mcp_env.tenant_a)
    _plant_notification(mcp_env, recipient=colleague)

    payload = _structured(call_tool(_token(caller), LIST, {}))

    assert payload == {"notifications": [], "unread_count": 0}


# --- the list: the bounds ----------------------------


def test_the_limit_bounds_are_exactly_one_fifty_and_two_hundred() -> None:
    """Pinned exactly rather than as a band:
    an unbounded `limit` is an unbounded response over the wire, and a
    default that drifts changes what every caller who omits it receives. The
    default is read OFF THE INPUT MODEL, so a constant that stops being wired
    into the field fails here while still reading 50 in the module.
    """
    assert notifications.LIMIT_MIN == 1
    assert notifications.LIMIT_DEFAULT == 50
    assert notifications.LIMIT_MAX == 200
    assert notifications.ListNotificationsInput().limit == 50
    assert notifications.ListNotificationsInput().unread_only is False


@pytest.mark.parametrize(
    ("limit", "accepted"),
    [
        pytest.param(notifications.LIMIT_MIN, True, id="limit-1-ok"),
        pytest.param(notifications.LIMIT_MAX, True, id="limit-200-ok"),
        pytest.param(notifications.LIMIT_MIN - 1, False, id="limit-0-refused"),
        pytest.param(notifications.LIMIT_MAX + 1, False, id="limit-201-refused"),
    ],
)
def test_the_limit_boundary_is_inclusive_at_1_and_200(
    mcp_env: SeededMcp,  # noqa: F811
    limit: int,
    accepted: bool,
) -> None:
    """The boundary, both ends in both directions.

    Catches bounds that are absent (201 accepted, so one caller can ask for
    the whole table) and bounds that are exclusive (1 or 200 refused, so the
    first page a bell asks for is an error). A one-sided pair cannot tell the
    two apart. No kill claim on the ORDER of the check: the refusal is
    pydantic's, in the pipeline, before the handler is entered at all, so
    "before any read" is not observable from the wire — what this pins is the
    code.
    """
    recipient = _plant_user(mcp_env, tenant=mcp_env.tenant_a)
    _plant_notification(mcp_env, recipient=recipient)

    result = call_tool(_token(recipient), LIST, {"limit": limit})

    if accepted:
        assert result.is_error is False, result.content
    else:
        assert error_code(result) == "VALIDATION"


# --- the list: the filter and the count ---------------------------------------


def test_unread_only_defaults_to_false_and_answers_read_and_unread_alike(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """The default arm, over a mixed set of two unread rows and one
    already read, with `unread_only` ABSENT from the arguments.

    Catches a default of `True` — a page that silently hides everything the
    person has already opened, so their notification history disappears the
    moment they read it — and an `unread_count` derived from the page, which
    would answer three here where two rows are unread.
    """
    recipient = _plant_user(mcp_env, tenant=mcp_env.tenant_a)
    unread_one = _plant_notification(mcp_env, recipient=recipient, created_at=NEWER)
    unread_two = _plant_notification(mcp_env, recipient=recipient, created_at=NEWER)
    read_row = _plant_notification(
        mcp_env, recipient=recipient, created_at=OLDER, read_at=ALREADY_READ_AT
    )

    payload = _structured(call_tool(_token(recipient), LIST, {}))

    assert set(_ids(payload)) == {unread_one, unread_two, read_row}
    assert payload["unread_count"] == 2


def test_unread_only_true_narrows_the_rows_without_changing_the_unread_count(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """The other arm, over the SAME mixed set, asserting both
    answers in one case so the two counts can be compared.

    Catches `unread_only` accepted and ignored (the already-read row still on
    the page, so a "what is new" view shows everything), and — the known risk —
    the filter applied to the aggregate as well as to the list,
    which makes the bell disagree with the page. The count is the same integer
    in both answers: it is the person's whole unread set, never the page's.
    """
    recipient = _plant_user(mcp_env, tenant=mcp_env.tenant_a)
    unread_one = _plant_notification(mcp_env, recipient=recipient, created_at=NEWER)
    unread_two = _plant_notification(mcp_env, recipient=recipient, created_at=NEWER)
    read_row = _plant_notification(
        mcp_env, recipient=recipient, created_at=OLDER, read_at=ALREADY_READ_AT
    )

    every = _structured(call_tool(_token(recipient), LIST, {"unread_only": False}))
    unread = _structured(call_tool(_token(recipient), LIST, {"unread_only": True}))

    assert set(_ids(every)) == {unread_one, unread_two, read_row}
    assert set(_ids(unread)) == {unread_one, unread_two}
    assert read_row not in _ids(unread)
    assert unread["unread_count"] == 2
    assert every["unread_count"] == unread["unread_count"]


def test_the_unread_count_counts_every_unread_row_and_not_only_the_page(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """`limit=1` over three unread rows answers ONE row and
    `unread_count == 3`.

    Catches a count computed from the returned page (or an aggregate that
    inherits the list's `LIMIT`) — the bell that goes quiet at fifty-one,
    wrong by two here where the arithmetic is small enough to read — and a
    second full read of the table dressed up as a count, which would still
    have to be limited to answer anything.
    """
    recipient = _plant_user(mcp_env, tenant=mcp_env.tenant_a)
    for _ in range(3):
        _plant_notification(mcp_env, recipient=recipient)

    payload = _structured(call_tool(_token(recipient), LIST, {"limit": 1}))

    assert len(payload["notifications"]) == 1
    assert payload["unread_count"] == 3


# --- the stamp ------------------------------------------------------------------


def test_the_stamp_input_declares_only_the_key_and_the_notification_id() -> None:
    """The input as a literal key set.

    Catches an input carrying a `user_id` — which is exactly the field identity
    rules forbid, and on a MUTATION an ignored one would stamp a row for
    somebody the caller did not name — and an input that forgot
    `MutatingToolInput`, which would leave the mutation with no idempotency
    key for the pipeline's guard to spend.
    """
    assert set(notifications.MarkNotificationReadInput.model_fields) == {
        "idempotency_key",
        "notification_id",
    }


@pytest.mark.parametrize("principal", PRINCIPALS)
def test_each_principal_stamps_its_own_notification_with_the_four_key_output(
    mcp_env: SeededMcp,  # noqa: F811
    principal: str,
) -> None:
    """Per principal: every one of the seven may clear their
    own bell, and the answer is exactly four keys.

    Catches a missing matrix cell (a principal whose bell can be read but
    never cleared, so their badge only ever grows), an output that carries the
    body and the subject as well — four keys is what the tool fixes, and the page
    already holds the text — and a handler that answers a success without
    writing: the row is re-read from the store and must carry the same
    `read_at` the caller was told.
    """
    recipient = _plant_user(mcp_env, tenant=mcp_env.tenant_a)
    planted = _plant_notification(mcp_env, recipient=recipient)
    assert _read_at_of(mcp_env, planted) is None

    payload = _structured(
        call_tool(
            _token(recipient, principal),
            MARK,
            {"notification_id": str(planted), "idempotency_key": _key()},
        )
    )

    assert set(payload) == MARK_OUTPUT_KEYS
    assert uuid.UUID(str(payload["notification_id"])) == planted
    assert payload["kind"] == KIND
    assert payload["title"] == TITLE
    assert payload["read_at"] is not None
    stored = _read_at_of(mcp_env, planted)
    assert stored is not None
    assert datetime.fromisoformat(str(payload["read_at"])) == stored


def test_the_same_idempotency_key_replays_the_stored_response(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Through the GUARD: one key, two calls.

    The pair of guarantees is deliberate. This one catches the tool
    registered as a READ, which skips `run_once` entirely — the response would
    then be rebuilt by the handler rather than replayed from the stored row,
    and the audit log would carry two `tool.succeeded` events for one
    intention. The event types are what separates this case from its twin
    below: `tool.replayed` is written only by the guard, so a passing
    already-read branch cannot make this case green on its own.
    """
    recipient = _plant_user(mcp_env, tenant=mcp_env.tenant_a)
    planted = _plant_notification(mcp_env, recipient=recipient)
    arguments = {"notification_id": str(planted), "idempotency_key": _key()}

    first = call_tool(_token(recipient), MARK, arguments)
    second = call_tool(_token(recipient), MARK, arguments)

    assert _response_text(first) == _response_text(second)
    assert _structured(first)["read_at"] == _structured(second)["read_at"]
    assert [str(row[0]) for row in _events_of(mcp_env, actor=recipient)] == [
        SUCCEEDED_EVENT,
        REPLAYED_EVENT,
    ]
    assert _read_at_of(mcp_env, planted) is not None


def test_two_different_keys_on_an_already_read_row_answer_the_same_read_at(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Through the ALREADY-READ BRANCH: two calls, two keys, so
    the idempotency guard never recognises the second one.

    This is the guarantee a page that mints a fresh key per click actually
    relies on. It catches a `mark_read` that calls the clock itself, or that
    has no already-read branch: the second call would re-stamp and "when did
    this person first see this message" would drift forward on every click of
    a double-clicked bell. The event types are asserted too — two
    `tool.succeeded`, no replay — so the guard cannot be what makes this pass.
    """
    recipient = _plant_user(mcp_env, tenant=mcp_env.tenant_a)
    planted = _plant_notification(mcp_env, recipient=recipient)

    first = _structured(
        call_tool(
            _token(recipient), MARK, {"notification_id": str(planted), "idempotency_key": _key()}
        )
    )
    second = _structured(
        call_tool(
            _token(recipient), MARK, {"notification_id": str(planted), "idempotency_key": _key()}
        )
    )

    assert first["read_at"] == second["read_at"]
    assert [str(row[0]) for row in _events_of(mcp_env, actor=recipient)] == [
        SUCCEEDED_EVENT,
        SUCCEEDED_EVENT,
    ]
    assert datetime.fromisoformat(str(first["read_at"])) == _read_at_of(mcp_env, planted)


def test_a_colleagues_notification_id_is_refused_not_found_and_stamps_nothing(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """With the tenant guard taken out of the
    picture: the row belongs to a colleague in the SAME tenant,
    so nothing but the user filter can refuse it.

    Catches a stamp that reads the row by id under the tenant filter alone:
    any person could then mark any colleague's message read, and the id would
    become a capability. `NOT_FOUND` rather than `FORBIDDEN`, so the argument
    does not confirm that a given uuid names a real notification somewhere;
    the row is re-read to prove the refusal came BEFORE the write.
    """
    caller = _plant_user(mcp_env, tenant=mcp_env.tenant_a)
    colleague = _plant_user(mcp_env, tenant=mcp_env.tenant_a)
    assert caller.tenant_id == colleague.tenant_id, "the point of this test"
    theirs = _plant_notification(mcp_env, recipient=colleague)

    result = call_tool(
        _token(caller), MARK, {"notification_id": str(theirs), "idempotency_key": _key()}
    )

    assert error_code(result) == "NOT_FOUND"
    assert _read_at_of(mcp_env, theirs) is None


def test_another_tenants_notification_id_is_refused_not_found_and_stamps_nothing(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """The cross-tenant half. The id names a REAL unread row in the
    other seeded tenant, so the refusal has to come from a filter rather than
    from an id nobody could resolve.

    Catches a stamp filtered by `user_id` alone (the row would be written in
    another tenant, from a token that has no business there) and a refusal
    that arrives AFTER the update — the row is re-read and must still be
    unread.
    """
    caller = _plant_user(mcp_env, tenant=mcp_env.tenant_a)
    foreigner = _plant_user(mcp_env, tenant=mcp_env.tenant_b)
    foreign_row = _plant_notification(mcp_env, recipient=foreigner)
    assert foreigner.tenant_id != caller.tenant_id

    result = call_tool(
        _token(caller), MARK, {"notification_id": str(foreign_row), "idempotency_key": _key()}
    )

    assert error_code(result) == "NOT_FOUND"
    assert _read_at_of(mcp_env, foreign_row) is None


def test_an_unknown_notification_id_is_refused_not_found(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """The third arrangement: an id that resolves nowhere reads
    exactly like a colleague's and like another tenant's.

    Catches a handler that lets a missing row reach the update (an
    `AttributeError` on `None`, answered as `INTERNAL`), which turns a stale
    bell in an open browser tab into an error the caller cannot act on, and
    any refusal code that differs from the two cases above — a distinction is
    an existence oracle.
    """
    recipient = _plant_user(mcp_env, tenant=mcp_env.tenant_a)

    result = call_tool(
        _token(recipient),
        MARK,
        {"notification_id": str(uuid.uuid4()), "idempotency_key": _key()},
    )

    assert error_code(result) == "NOT_FOUND"


@pytest.mark.parametrize(
    ("length", "accepted"),
    [
        pytest.param(KEY_LENGTH, True, id="key-at-128-ok"),
        pytest.param(KEY_LENGTH + 1, False, id="key-at-129-refused"),
    ],
)
def test_the_idempotency_key_boundary_is_inclusive_at_key_length(
    mcp_env: SeededMcp,  # noqa: F811
    length: int,
    accepted: bool,
) -> None:
    """The one client string that reaches a store is the idempotency key, and it is length-checked.

    The pair catches both sides: a check written `>=` refuses the longest key
    the `idempotency_keys` column is declared to hold, and no check at all
    lets an over-long key abort the transaction inside Postgres AFTER the row
    has been stamped. The refused arm re-reads the notification and finds it
    still unread, which is what makes "before any write" an assertion rather
    than a hope.
    """
    assert KEY_LENGTH == 128
    recipient = _plant_user(mcp_env, tenant=mcp_env.tenant_a)
    planted = _plant_notification(mcp_env, recipient=recipient)
    key = f"fake-key-{uuid.uuid4().hex}".ljust(length, "k")
    assert len(key) == length

    result = call_tool(
        _token(recipient), MARK, {"notification_id": str(planted), "idempotency_key": key}
    )

    if accepted:
        assert result.is_error is False, result.content
        assert _read_at_of(mcp_env, planted) is not None
    else:
        assert error_code(result) == "VALIDATION"
        assert _read_at_of(mcp_env, planted) is None


# --- exactly one audit event per call ----------


def test_one_audit_event_per_list_call_names_the_tool_and_the_caller(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """The read, asserting the event's SUBJECT and PAYLOAD
    KEYS rather than merely that an event exists.

    Catches a handler that appends an event of its own beside the pipeline's
    (no event type is added, and one row per bell-open would be
    measurable noise in a table whose whole value is that every row matters),
    a declared type other than the generic `tool.succeeded`, and an event that
    records the call without recording WHO made it — `actor_ref` is the only
    thing on it that answers an auditor.
    """
    recipient = _plant_user(mcp_env, tenant=mcp_env.tenant_a)
    _plant_notification(mcp_env, recipient=recipient)
    assert _events_of(mcp_env, actor=recipient) == []

    _structured(call_tool(_token(recipient), LIST, {}))

    events = _events_of(mcp_env, actor=recipient)
    assert len(events) == 1
    event_type, subject_type, subject_id, actor_ref, payload = events[0]
    assert str(event_type) == SUCCEEDED_EVENT
    assert str(subject_type) == "tool"
    assert str(subject_id) == LIST
    assert str(actor_ref) == str(recipient.user_id)
    assert set(payload) == {"tool", "scope"}
    assert payload["tool"] == LIST
    assert payload["scope"] == SELF_ONLY_SCOPE


def test_one_audit_event_per_stamp_call_names_the_tool_and_the_caller(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """The mutation, same shape.

    Catches the same three faults on the writing side, where they cost more: a
    dedicated `notification.read` event type would put one row in the hash
    chain per UI interaction (refused exactly that), and a second
    event appended by the handler would double-count every click.
    """
    recipient = _plant_user(mcp_env, tenant=mcp_env.tenant_a)
    planted = _plant_notification(mcp_env, recipient=recipient)
    assert _events_of(mcp_env, actor=recipient) == []

    _structured(
        call_tool(
            _token(recipient), MARK, {"notification_id": str(planted), "idempotency_key": _key()}
        )
    )

    events = _events_of(mcp_env, actor=recipient)
    assert len(events) == 1
    event_type, subject_type, subject_id, actor_ref, payload = events[0]
    assert str(event_type) == SUCCEEDED_EVENT
    assert str(subject_type) == "tool"
    assert str(subject_id) == MARK
    assert str(actor_ref) == str(recipient.user_id)
    assert set(payload) == {"tool", "scope"}
    assert payload["tool"] == MARK
    assert payload["scope"] == SELF_ONLY_SCOPE
