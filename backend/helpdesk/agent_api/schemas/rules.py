"""Request schemas for the admin rules route."""

from pydantic import BaseModel


class ListSodRulesArgs(BaseModel):
    """No arguments at all: the rules listed are the token tenant's own.

    The zero-argument form `me_routes.ListMyAccessArgs` and the kill-switch
    board already use. `ListSodRulesInput` declares no field and forbids
    extras, so a key riding along from here — a tenant, a user, a rule id —
    would answer `VALIDATION` on every load of the board rather than filtering
    anything.
    """
