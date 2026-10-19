"""Argument models for the admin tickets route."""

from pydantic import BaseModel


class ListOpenTicketsArgs(BaseModel):
    """`list_open_tickets` with no page size, so the tool's default applies.

    The limit lives on a subclass, not a nullable field, because `model_dump`
    would send `limit: null` and the tool refuses it.
    """


class ListOpenTicketsArgsWithLimit(ListOpenTicketsArgs):
    """`list_open_tickets` with the caller's own page size."""

    limit: int
