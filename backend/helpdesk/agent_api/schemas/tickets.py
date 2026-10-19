"""Argument models for the admin tickets routes."""

import uuid

from pydantic import BaseModel, ConfigDict, Field


class ListOpenTicketsArgs(BaseModel):
    """`list_open_tickets` with no page size, so the tool's default applies.

    The limit lives on a subclass, not a nullable field, because `model_dump`
    would send `limit: null` and the tool refuses it.
    """


class ListOpenTicketsArgsWithLimit(ListOpenTicketsArgs):
    """`list_open_tickets` with the caller's own page size."""

    limit: int


class SolveTicketArgs(BaseModel):
    """The two keys the solve tool takes.

    Extra fields are refused here so a browser-sent `tenant_id` or `user_id`
    never travels on. The key is forwarded, never made here, so a retry
    replays instead of solving twice.
    """

    model_config = ConfigDict(extra="forbid")

    ticket_id: uuid.UUID
    idempotency_key: str = Field(min_length=1)
