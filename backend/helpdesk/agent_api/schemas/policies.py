"""Request argument models for the /admin/policies routes."""

from pydantic import BaseModel, ConfigDict, Field


class ListPolicyDocumentsArgs(BaseModel):
    """No arguments at all: the corpus listed is the token tenant's own.

    The zero-argument form `rules_routes.ListSodRulesArgs` and the kill-switch
    board already use. `ListPolicyDocumentsInput` declares no field and forbids
    extras, so a key riding along from here -- a tenant, a slug, a version --
    would answer `VALIDATION` on every load of the board rather than filtering
    anything.
    """


class ReingestPolicyDocumentArgs(BaseModel):
    """Exactly the three keys `ReingestPolicyDocumentInput` declares.

    `extra="forbid"` here as well as there, so a `tenant_id` the browser sent
    is refused on this side of the boundary rather than crossing it to be
    refused on the other: the tenant whose corpus is rewritten is the token's
    and is decided behind the MCP boundary. Neither name carries
    its bound here -- `check_slug` and `check_version` run in the pipeline
    before the corpus path is composed, so the caps have one home and one
    error.

    `idempotency_key` is forwarded, never minted here: a key this route
    invented would turn every retry of a re-ingest into a second ingest
    instead of a replay. Pinned as the exact argument dict by
    `test_policies_routes.py::test_the_reingest_route_sends_its_exact_three_key_argument_set`.
    """

    model_config = ConfigDict(extra="forbid")

    slug: str = Field(min_length=1)
    version: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
