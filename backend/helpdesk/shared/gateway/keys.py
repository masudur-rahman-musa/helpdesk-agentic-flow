"""The per-tenant gateway key lookup, in ONE place for both clients.

a design note's whole control is that the slug in `X-Tenant` and the key in
`Authorization` come from the same lookup: the gateway compares the two and
answers 403 when they disagree, so two copies of this rule would be two places
for it to stop being true. `checked_tenant_key` returns both halves together,
which is what makes drifting them apart impossible rather than merely
unlikely.

Raises a plain `ToolError` with `ErrorCode.VALIDATION`. Each client wraps it in
its own error class, so `helpdesk.mcp_server.services.rag.redaction_client`
keeps raising `GatewayRequestError` with the message it has always raised
(`test_rag_gateway.py::test_a_tenant_with_no_configured_key_is_refused_before_any_request`)
and `helpdesk.gateway.client` raises `ModelRequestError`
(`test_gateway_client.py`'s missing-key row).

Pure apart from the mapping it is handed: no I/O, no settings read here.
"""

from collections.abc import Mapping

from helpdesk.shared.core.errors import ErrorCode, ToolError
from helpdesk.shared.core.naming import check_slug


class TenantKeyError(ToolError):
    """No usable key for this tenant, or a slug that cannot be a header value.

    `VALIDATION`: nothing about the request will succeed on a retry until
    either the slug or the configuration changes.
    """

    code = ErrorCode.VALIDATION


def checked_tenant_key(tenant_slug: str, api_keys: Mapping[str, str]) -> tuple[str, str]:
    """The accepted slug and the key configured for it, as one pair.

    `check_slug` runs first (14a's pre-check), so an over-long or malformed
    slug is refused before it can reach a header at all: a header value is one
    of the unsized stores a project rule names. The returned slug is the one
    the lookup used, so `X-Tenant` and `Authorization` cannot come from
    different places. Pinned by
    `test_rag_gateway.py::test_the_other_tenants_key_is_used_for_the_other_tenant`
    and `test_gateway_client.py`'s a design note pair.
    """
    try:
        slug = check_slug(tenant_slug)
    except ToolError as exc:
        raise TenantKeyError(str(exc)) from exc
    key = api_keys.get(slug)
    if key is None:
        raise TenantKeyError(f"no gateway API key is configured for tenant {slug!r}")
    return slug, key
