"""ORM models for the whole app."""

from helpdesk.shared.models.access import (
    PERIOD_LENGTH,
    AccessRequest,
    AccessReview,
    RequestStatus,
    ReviewDecision,
    ReviewFlag,
    ReviewItem,
    ReviewStatus,
)
from helpdesk.shared.models.audit import AuditChainAnchor, AuditEvent
from helpdesk.shared.models.base import Base, TenantScoped
from helpdesk.shared.models.conversation import (
    MAX_MESSAGE_CHARS,
    Message,
    MessageRole,
    MessageTooLongError,
    Session,
    check_message_content,
)
from helpdesk.shared.models.directory import (
    Department,
    Grant,
    Role,
    RoleTemplate,
    SodRule,
    System,
    Tenant,
    Ticket,
    User,
)
from helpdesk.shared.models.idempotency import IdempotencyKey
from helpdesk.shared.models.notification import (
    NOTIFICATION_BODY_MAX_LENGTH,
    NOTIFICATION_TITLE_MAX_LENGTH,
    Notification,
    NotificationKind,
)
from helpdesk.shared.models.numbers import ReferenceCounter
from helpdesk.shared.models.policy import PolicyChunk, PolicyDocument

__all__ = [
    "MAX_MESSAGE_CHARS",
    "NOTIFICATION_BODY_MAX_LENGTH",
    "NOTIFICATION_TITLE_MAX_LENGTH",
    "PERIOD_LENGTH",
    "AccessRequest",
    "AccessReview",
    "AuditChainAnchor",
    "AuditEvent",
    "Base",
    "Department",
    "Grant",
    "IdempotencyKey",
    "Message",
    "MessageRole",
    "MessageTooLongError",
    "Notification",
    "NotificationKind",
    "PolicyChunk",
    "PolicyDocument",
    "ReferenceCounter",
    "RequestStatus",
    "ReviewDecision",
    "ReviewFlag",
    "ReviewItem",
    "ReviewStatus",
    "Role",
    "RoleTemplate",
    "Session",
    "SodRule",
    "System",
    "Tenant",
    "TenantScoped",
    "Ticket",
    "User",
    "check_message_content",
]
