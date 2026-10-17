"""What the system tells a person. `notify` is the one writer, shared by every caller."""

from helpdesk.shared.notifications.notify import InvalidNotificationError, notify

__all__ = [
    "InvalidNotificationError",
    "notify",
]
