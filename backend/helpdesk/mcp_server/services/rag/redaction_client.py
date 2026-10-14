"""The redaction client: the only thing in `helpdesk.rag` that opens a socket.

the design's ingestion order is redact first, then split, so this runs before
`markdown.split_sections` and everything downstream — offsets, chunk text,
Qdrant payloads — indexes the REDACTED document. The
`sha256` the store records stays the source bytes, because that digest answers
"has this file changed", which is a question about the file.

One call per document rather than one per section (decision F). The gateway
numbers placeholders from 1 on every call, so per-section calls would make
`<EMAIL_1>` name a different person in two sections of the same policy and the
filed map would stop being an inverse.

Three pre-checks run BEFORE any request is built — the slug's shape, the
document's length, the tenant's key — so a refusal is a typed `VALIDATION`
with nothing sent, rather than a 4xx read back from the gateway. Pinned by
`test_rag_gateway.py::test_a_document_one_character_over_the_cap_is_refused_before_any_request` and
`test_rag_gateway.py::test_a_tenant_with_no_configured_key_is_refused_before_any_request`,
each asserting the transport was never called.

No gateway wording and no URL reaches a caller's message: the gateway's body
can quote the document it was asked to redact, which is the one string on this
path that may hold personal data, and the URL names internal infrastructure.
Pinned by `test_rag_gateway.py::test_no_gateway_wording_and_no_url_reaches_the_message`.
"""

from collections.abc import Mapping
from dataclasses import dataclass

import httpx

from helpdesk.shared.core.settings import Settings
from helpdesk.shared.errors import ErrorCode, ToolError
from helpdesk.shared.gateway.keys import checked_tenant_key

MAX_REDACTION_CHARS = 16_384
"""The gateway's per-string cap, copied from `opsconsole.redaction`'s
`MAX_REDACTION_INPUT_CHARS`.

A COPY, not an import: the console is a separate repository and nothing here
depends on it at build time. The value is pinned exactly by
`test_rag_gateway.py::test_the_gateway_constants_are_exactly_the_planned_values`, and this
docstring names the module it mirrors, so a console change surfaces as a 422
whose cause is one grep away. The largest corpus file is well under it.
"""

CONNECT_TIMEOUT_SECONDS = 5.0
"""Long enough for a TLS handshake on a slow link, short enough that an
unreachable gateway is an error rather than a hang."""

READ_TIMEOUT_SECONDS = 30.0
"""Redaction is a CPU scan over the whole document on the gateway side, so the
read half is generous where the connect half is not. Both halves are asserted
on the constructed client by
`test_rag_gateway.py::test_the_constructed_client_carries_both_timeout_halves`."""

DATA_CLASS = "internal"
"""the design's class for policy ingestion. Policy documents are internal
company documents, not the `confidential` class chat messages carry."""


class GatewayForbiddenError(ToolError):
    """The gateway refused the key, or the key does not own the tenant.

    A 403 here is the control a design note exists for: the tenant in the header
    and the tenant the key owns disagreed, which means this process was about
    to file one tenant's redaction map under another's name.
    """

    code = ErrorCode.FORBIDDEN


class GatewayRequestError(ToolError):
    """This request is malformed, or would be. Raised for the local
    pre-checks and for the gateway's own 422 alike, because both say the same
    thing to a caller: fix the input, retrying unchanged cannot help."""

    code = ErrorCode.VALIDATION


class GatewayUnavailableError(ToolError):
    """The gateway could not be reached, or failed on its side.

    Deliberately not `VALIDATION`: an ingest run that stops here should be
    retried unchanged once the dependency is back, and telling that apart
    from a bad document is the only thing an operator asks at 3am.
    """

    code = ErrorCode.INTERNAL


@dataclass(frozen=True)
class RedactedDocument:
    """A document as the gateway returned it, and the map it filed for it.

    Frozen: `text` is what gets split, embedded and stored, and
    `redaction_map_id` is the only handle that can turn a placeholder back
    into what it replaced. Anything able to rewrite one without the other
    would leave a stored chunk whose map does not describe it.
    """

    text: str
    redaction_map_id: str


class RedactionClient:
    """One httpx client, one call, typed refusals.

    The key is looked up per tenant and the slug that goes into
    `X-Tenant` is the same one the lookup used, so the two halves the gateway
    compares can never come from different places. Pinned by
    `test_rag_gateway.py::test_the_other_tenants_key_is_used_for_the_other_tenant`.
    """

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
        """Build the client from loaded configuration.

        The key map is already parsed and already validated by `Settings`, so
        a malformed pair has failed at load time rather than here.
        """
        return cls(base_url=settings.gateway_url, api_keys=settings.gateway_api_keys)

    async def aclose(self) -> None:
        """Close the underlying connection pool."""
        await self.client.aclose()

    def _checked(self, tenant_slug: str) -> tuple[str, str]:
        """The accepted slug and the key configured for it, or a typed refusal.

        Delegates to `helpdesk.gateway.keys.checked_tenant_key`, which both
        gateway clients share so a design note's pairing lives in one place (plan
        16 decision G). The wrapping preserves this client's own error class
        and its messages unchanged, which is what
        `test_rag_gateway.py::test_a_tenant_with_no_configured_key_is_refused_before_any_request`
        asserts.
        """
        try:
            return checked_tenant_key(tenant_slug, self._api_keys)
        except ToolError as exc:
            raise GatewayRequestError(str(exc)) from exc

    async def redact_document(self, text: str, tenant_slug: str) -> RedactedDocument:
        """Redact one whole document and return it with the filed map's id.

        The length check is inclusive at `MAX_REDACTION_CHARS` and runs before
        the request is built, so an over-long document costs no round trip and
        answers `VALIDATION` rather than the gateway's 422
        (`test_rag_gateway.py::test_a_document_of_exactly_the_cap_is_accepted`,
        `test_rag_gateway.py::test_a_document_one_character_over_the_cap_is_refused_before_any_request`).
        """
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
            # The exception's own message carries the URL, so it stays in the
            # chained cause where a log line reads it, and never in ours.
            raise GatewayUnavailableError("the redaction gateway could not be reached") from exc

        return self._parsed(response)

    def _parsed(self, response: httpx.Response) -> RedactedDocument:
        """Map a response onto the typed codes, or onto a redacted document.

        401 and 403 are one answer here rather than two: both mean the key
        this process holds cannot redact for this tenant, and neither is
        something a retry fixes. Every other non-200 is `INTERNAL`, which is
        the honest answer for a status this client has no rule for.
        """
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
