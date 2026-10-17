"""Request models for the admin audit routes."""

from pydantic import BaseModel, ConfigDict


class SearchAuditArgs(BaseModel):
    """`search_audit`'s arguments, carrying only the filters that were asked for.

    No declared fields, because `model_dump` would send `null` for every unset
    one and the tool reads an absent filter as "any". Unset filters send no key.
    """

    model_config = ConfigDict(extra="allow")
