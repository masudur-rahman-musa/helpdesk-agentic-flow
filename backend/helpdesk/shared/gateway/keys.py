"""The per-tenant gateway key lookup, shared by both gateway clients.

The gateway refuses a request whose `X-Tenant` and key do not match, so both
come from one lookup and are returned together.
"""

from collections.abc import Mapping

from helpdesk.shared.core.errors import ErrorCode, ToolError
from helpdesk.shared.core.naming import check_slug


class TenantKeyError(ToolError):
    """No key for this tenant, or a slug unfit for a header. Retrying will not help."""

    code = ErrorCode.VALIDATION


def checked_tenant_key(tenant_slug: str, api_keys: Mapping[str, str]) -> tuple[str, str]:
    """The checked slug and its key, as one pair. A bad slug never reaches a header."""
    try:
        slug = check_slug(tenant_slug)
    except ToolError as exc:
        raise TenantKeyError(str(exc)) from exc
    key = api_keys.get(slug)
    if key is None:
        raise TenantKeyError(f"no gateway API key is configured for tenant {slug!r}")
    return slug, key
