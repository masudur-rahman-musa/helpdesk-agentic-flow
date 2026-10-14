"""What the system tells a person (the design).

One writer, `notify`, beside the other engines rather than inside a service:
the scheduler's jobs, and later the web routes of row 23, both write here, and
a notification composed in a service would be a second definition of the same
row.

Two readers beside it since an earlier change: `list_for_user` answers a person's own
page with their whole unread total, and `mark_read` is the only thing that
moves `read_at`. Both take the user as an argument and the tenant off the
session, the same shape the writer has.
"""

from helpdesk.shared.notifications.list import NotificationPage, list_for_user
from helpdesk.shared.notifications.mark import mark_read
from helpdesk.shared.notifications.notify import InvalidNotificationError, notify

__all__ = [
    "InvalidNotificationError",
    "NotificationPage",
    "list_for_user",
    "mark_read",
    "notify",
]
