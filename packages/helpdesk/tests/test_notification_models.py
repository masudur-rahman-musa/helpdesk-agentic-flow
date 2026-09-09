"""An earlier change §6: the `notifications` table and the constants that bound it.

Written before the implementation exists. Expected surface (the implementer
must provide exactly this):

``helpdesk.models.notification`` (re-exported from ``helpdesk.models``)
    ``NOTIFICATION_TITLE_MAX_LENGTH = 200``
    ``NOTIFICATION_BODY_MAX_LENGTH = 2000``
    ``class NotificationKind(StrEnum)`` — exactly two members after an earlier change
        (§8), ``GRANT_EXPIRED = "grant_expired"`` and
        ``GRANT_EXPIRING = "grant_expiring"`` (21a decision F: a member
        nothing writes is a value no test can reach, so each plan adds its
        own when the code that writes it lands; 21c adds the review kinds).
    ``class Notification(TenantScoped)`` — ``__tablename__ = "notifications"``;
        columns ``user_id`` (FK ``users.id``), ``kind``, ``title``, ``body``,
        ``subject_type``, ``subject_id`` and a nullable ``read_at``.

``packages/helpdesk/tests/_seed.py``
    ``TENANT_SCOPED_TABLES`` gains ``"notifications"`` (a project rule, the
    same shape ``test_conversation_models.py`` uses for `sessions`/`messages`).

No backing service is needed here: every case below reads
``Base.metadata`` or a module constant, which is why this file carries no
fixture at all.

Deliberately NOT written here: a second Alembic upgrade/downgrade round trip.
``test_models.py::test_migrated_schema_matches_metadata_and_downgrades_clean``
already compares the migrated schema against the WHOLE of ``Base.metadata``
and asserts `downgrade base` leaves only ``alembic_version``, so it covers
this plan's revision the moment the model joins the metadata. A copy of it in
this file would be red and green for exactly the same reasons as the original
— the neighbouring-guard trap — while costing a second database and two
`alembic` subprocesses per run.

Column WIDTHS are not asserted. Plan §1 fixes the two lengths as pre-check
caps, not as column types, and the tree stores a long body
either way (`tickets.body` is `Text`, `messages.content` is a sized `String`);
the boundary pair that matters is pinned against the CHECK in
`test_notifications.py`.
"""

from enum import StrEnum

from _seed import TENANT_SCOPED_TABLES
from helpdesk.models import Base, Notification, TenantScoped
from helpdesk.models.notification import (
    NOTIFICATION_BODY_MAX_LENGTH,
    NOTIFICATION_TITLE_MAX_LENGTH,
    NotificationKind,
)


def test_the_notifications_table_is_in_the_metadata() -> None:
    """Catches a model declared in its own module but never imported into
    `helpdesk.models`, which leaves `Base.metadata.create_all` and Alembic's
    autogenerate comparison both blind to the table — the migration would
    then be empty and every notification write would fail against a table
    that is not there."""
    assert "notifications" in Base.metadata.tables
    assert Notification.__tablename__ == "notifications"


def test_the_notifications_table_is_in_the_tenant_scoped_allowlist() -> None:
    """Catches the standing-rule-27 miss: a table added to the metadata
    without extending `_seed.py::TENANT_SCOPED_TABLES`, which leaves
    `test_models.py`'s schema-literal sweep (and its per-table tenant_id and
    composite-index checks) never applied to this table at all."""
    assert "notifications" in TENANT_SCOPED_TABLES


def test_the_notification_columns_are_the_planned_columns() -> None:
    """Catches a table missing the columns §1 fixes: without `subject_type`
    and `subject_id` a notification names no row an operator can open, and
    without a NULLABLE `read_at` there is no way to store one unread — the
    web route of row 23 would have nothing to mark."""
    columns = Base.metadata.tables["notifications"].c
    for name in ("tenant_id", "user_id", "kind", "title", "body"):
        assert name in columns, name
        assert not columns[name].nullable, name
    for name in ("subject_type", "subject_id"):
        assert name in columns, name
    assert "read_at" in columns
    assert columns["read_at"].nullable
    assert {fk.column.table.name for fk in columns.user_id.foreign_keys} == {"users"}


def test_the_notification_model_is_tenant_scoped_not_merely_tenant_stamped() -> None:
    """Catches a model that carries a `tenant_id` column but is declared on
    plain `Base`: the ORM guard in `helpdesk.core.db` keys on the CLASS, so
    such a table reads every tenant's notifications from any tenant's
    session. `test_models.py`'s sweep checks the column and the index and
    cannot see the base class, so this is the case that dies for it."""
    assert issubclass(Notification, TenantScoped)


def test_the_notifications_table_carries_the_composite_tenant_index() -> None:
    """Catches a table whose only index is the primary key: every read of
    this table is "one tenant's rows", and without a composite index led by
    `tenant_id` that read is a sequential scan of every tenant's rows on the
    one table a notification route hits on every page load."""
    table = Base.metadata.tables["notifications"]
    assert not table.c.tenant_id.nullable
    assert {fk.column.table.name for fk in table.c.tenant_id.foreign_keys} == {"tenants"}
    composite = [
        index
        for index in table.indexes
        if len(index.columns) >= 2 and next(iter(index.columns)).name == "tenant_id"
    ]
    assert composite, "notifications needs a composite index led by tenant_id"


def test_the_notification_kind_enum_has_exactly_the_four_planned_members() -> None:
    """An earlier change §8 widens 21b's set: `review_overdue` and `review_escalated`
    join it because 21c is the plan whose `send_review_reminders` writes them.

    Still pinned as an exact set, and still 21a decision F's claim: catches
    the rest of the lifecycle vocabulary landing early — a member no code path
    writes is a value no test can reach, and a stored row could then carry a
    kind nothing renders. Also catches any of the four values drifting from
    the literals `expire_grants`, `warn_expiring_grants` and
    `send_review_reminders` write and row 23's route reads; `grant_expiring`
    is additionally what the warning sweep's own anti-join matches on, so a
    drift there silently disables the dedupe.
    """
    assert issubclass(NotificationKind, StrEnum)
    assert {kind.value for kind in NotificationKind} == {
        "grant_expired",
        "grant_expiring",
        "review_overdue",
        "review_escalated",
    }
    assert NotificationKind.GRANT_EXPIRED.value == "grant_expired"
    assert NotificationKind.GRANT_EXPIRING.value == "grant_expiring"
    assert NotificationKind.REVIEW_OVERDUE.value == "review_overdue"
    assert NotificationKind.REVIEW_ESCALATED.value == "review_escalated"


def test_the_length_caps_are_exactly_the_planned_values() -> None:
    """a project rule: catches a cap quietly widened or narrowed, which
    moves the boundary every case in `test_notifications.py` asserts against
    and, being the only gate on a string that reaches a store, decides what
    a caller can write."""
    assert NOTIFICATION_TITLE_MAX_LENGTH == 200
    assert NOTIFICATION_BODY_MAX_LENGTH == 2000
