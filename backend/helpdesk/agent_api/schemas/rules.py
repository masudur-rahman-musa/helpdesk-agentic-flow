"""Request schemas for the admin rules route."""

from pydantic import BaseModel


class ListSodRulesArgs(BaseModel):
    """No arguments: the rules listed are the token tenant's own.

    The tool forbids extra fields, so any key sent here would fail every load.
    """
