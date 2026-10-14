"""Authentication helpers: argon2 hashing, JWT issue/verify, RBAC matrix."""

from helpdesk.shared.auth.jwt import TokenClaims, TokenVerificationError, issue_token, verify_token
from helpdesk.shared.auth.passwords import hash_password, verify_password
from helpdesk.shared.auth.rbac import (
    Authorization,
    Principal,
    RbacDenied,
    Scope,
    Tool,
    UnknownToolError,
    authorize,
)

__all__ = [
    "Authorization",
    "Principal",
    "RbacDenied",
    "Scope",
    "TokenClaims",
    "TokenVerificationError",
    "Tool",
    "UnknownToolError",
    "authorize",
    "hash_password",
    "issue_token",
    "verify_password",
    "verify_token",
]
