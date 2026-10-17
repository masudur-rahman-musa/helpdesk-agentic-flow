"""The redaction client: the only part of RAG ingest that opens a socket.

One call per whole document, since the gateway numbers placeholders per
call. No gateway text or URL reaches a caller's message.
"""

from collections.abc import Mapping
from dataclasses import dataclass

import httpx

from helpdesk.shared.core.config import Settings
from helpdesk.shared.core.errors import ErrorCode, ToolError
from helpdesk.shared.gateway.keys import checked_tenant_key

MAX_REDACTION_CHARS = 16_384
"""The gateway's per-string cap, copied from the console (a separate repo)."""

CONNECT_TIMEOUT_SECONDS = 5.0
"""Long enough for a TLS handshake on a slow link, short enough that an
unreachable gateway is an error rather than a hang."""

READ_TIMEOUT_SECONDS = 30.0
"""Redaction is slow on big documents, so reads wait longer."""

DATA_CLASS = "internal"
"""Policy documents are internal company documents, not `confidential`
like chat messages."""


class GatewayForbiddenError(ToolError):
    """The gateway refused the key, or the key does not own the tenant."""

    code = ErrorCode.FORBIDDEN


class GatewayRequestError(ToolError):
    """This request is malformed. Covers local checks and the gateway's 422:
    retrying unchanged cannot help."""

    code = ErrorCode.VALIDATION


class GatewayUnavailableError(ToolError):
    """The gateway could not be reached, or failed. Retry later."""

    code = ErrorCode.INTERNAL


@dataclass(frozen=True)
class RedactedDocument:
    """A redacted document and the id of the map filed for it."""

    text: str
    redaction_map_id: str


class RedactionClient:
    """One httpx client, one call, typed refusals. The `X-Tenant` slug is the
    one the key was looked up by."""

    def __init__(
        self,
        *,
        base_url: str,
        api_keys: Mapping[str, str],
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_keys = dict(api_keys)
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=CONNECT_TIMEOUT_SECONDS,
                read=READ_TIMEOUT_SECONDS,
                write=READ_TIMEOUT_SECONDS,
                pool=CONNECT_TIMEOUT_SECONDS,
            ),
            transport=transport,
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> "RedactionClient":
        """Build the client from loaded configuration."""
        return cls(base_url=settings.gateway_url, api_keys=settings.gateway_api_keys)

    async def aclose(self) -> None:
        """Close the underlying connection pool."""
        await self.client.aclose()

    def _checked(self, tenant_slug: str) -> tuple[str, str]:
        """The accepted slug and its key, or this client's own refusal."""
        try:
            return checked_tenant_key(tenant_slug, self._api_keys)
        except ToolError as exc:
            raise GatewayRequestError(str(exc)) from exc

    async def redact_document(self, text: str, tenant_slug: str) -> RedactedDocument:
        """Redact one whole document and return it with the filed map's id."""
        slug, key = self._checked(tenant_slug)
        if len(text) > MAX_REDACTION_CHARS:
            raise GatewayRequestError(
                f"a document longer than {MAX_REDACTION_CHARS} characters cannot be redacted: "
                f"{len(text)}"
            )

        try:
            response = await self.client.post(
                f"{self._base_url}/redact",
                json={"text": text, "data_class": DATA_CLASS},
                headers={
                    "Authorization": f"Bearer {key}",
                    "X-Tenant": slug,
                    "X-Data-Class": DATA_CLASS,
                },
            )
        except httpx.HTTPError as exc:
            # Its message has the URL, so keep it in the cause, not in ours.
            raise GatewayUnavailableError("the redaction gateway could not be reached") from exc

        return self._parsed(response)

    def _parsed(self, response: httpx.Response) -> RedactedDocument:
        """Map a response onto the typed codes, or onto a redacted document."""
        if response.status_code in (401, 403):
            raise GatewayForbiddenError("the redaction gateway refused this tenant's API key")
        if response.status_code == 422:
            raise GatewayRequestError("the redaction gateway refused this document")
        if response.status_code != 200:
            raise GatewayUnavailableError("the redaction gateway failed to redact this document")

        try:
            body = response.json()
            text = body["text"]
            map_id = body["redaction_map_id"]
        except (ValueError, KeyError, TypeError) as exc:
            raise GatewayUnavailableError(
                "the redaction gateway returned a body this client cannot read"
            ) from exc
        if not isinstance(text, str) or not isinstance(map_id, str):
            raise GatewayUnavailableError(
                "the redaction gateway returned a body this client cannot read"
            )
        return RedactedDocument(text=text, redaction_map_id=map_id)
