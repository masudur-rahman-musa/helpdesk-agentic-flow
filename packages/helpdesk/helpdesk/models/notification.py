"""The `notifications` table: what the system tells a person (the design).

Named by the design's prose and absent from §6's table list, so it lands here
under §11. One row is one message to one person: the scheduler's
expiry sweep writes one for the holder and one for their department manager
rather than a single row carrying both, because every read of this table is
"what is unread for THIS user" and a list inside a row answers no such query.

`read_at` is nullable and the only mutable field a reader ever writes: a row
is stored unread, and row 23's route stamps it when the person opens it.

`NOTIFICATION_TITLE_MAX_LENGTH` and `NOTIFICATION_BODY_MAX_LENGTH` are the
caps AND the column widths, the arrangement `models/conversation.py` already
has with `MAX_MESSAGE_CHARS`: one number shared by the pre-check in
`helpdesk.notifications.notify` and the column it guards, so widening one
without the other cannot happen. a project rule — the check runs before any
row is added, because the sweep has already marked a grant expired and
appended its audit event by the time it composes this text, and a column error
here would abort that transaction. The boundary pair is pinned by
`test_notifications.py::test_a_title_of_exactly_the_cap_is_accepted` and
`::test_a_title_one_character_over_the_cap_is_refused_before_any_row_is_written`,
and the same pair for the body.
"""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from helpdesk.models.base import TenantScoped

NOTIFICATION_TITLE_MAX_LENGTH = 200
"""The cap on a notification's title, and the `notifications.title` width.

Pinned exactly by
`test_notification_models.py::test_the_length_caps_are_exactly_the_planned_values`."""

NOTIFICATION_BODY_MAX_LENGTH = 2000
"""The cap on a notification's body, and the `notifications.body` width.

Ten times the title, because the body carries the composed sentence a person
reads; pinned by the same test as the title."""


class NotificationKind(StrEnum):
    """What happened, as a closed vocabulary row 23's route renders by.

    Exactly two members, one per sweep that writes one (an earlier change decision F,
    an earlier change decision F): a member nothing writes is a value no test can
    reach, so 21c's review reminders add theirs when the code that writes them
    lands, and 21b's leaver sweep adds none at all. Pinned as a set by
    `test_notification_models.py::test_the_notification_kind_enum_has_exactly_the_two_planned_members`.
    """

    GRANT_EXPIRED = "grant_expired"
    GRANT_EXPIRING = "grant_expiring"


class Notification(TenantScoped):
    """One message to one person of one tenant.

    `TenantScoped` rather than a plain `Base` with a `tenant_id` column: the
    ORM guard in `helpdesk.core.db` keys on the CLASS, so a merely
    tenant-stamped table would read every tenant's notifications from any
    tenant's session. Pinned by
    `test_notification_models.py::test_the_notification_model_is_tenant_scoped_not_merely_tenant_stamped`.

    `subject_type` and `subject_id` name the row the message is about (a
    grant, here) so a route can link to it; they are nullable because a later
    kind may be about nothing in particular.
    """

    __tablename__ = "notifications"
    __table_args__ = (
        # Leads on (tenant_id, user_id, read_at): every read of this table is
        # one person's unread list.
        Index("ix_notifications_tenant_id_user_id_read_at", "tenant_id", "user_id", "read_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    kind: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(NOTIFICATION_TITLE_MAX_LENGTH))
    body: Mapped[str] = mapped_column(String(NOTIFICATION_BODY_MAX_LENGTH))
    subject_type: Mapped[str | None] = mapped_column(String(64))
    subject_id: Mapped[str | None] = mapped_column(String(256))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
