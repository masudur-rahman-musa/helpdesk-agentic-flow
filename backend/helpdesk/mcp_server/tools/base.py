"""What every mutating tool's input shares.

Mutating inputs refuse extra fields, where read inputs ignore them. An
unexpected `user_id` on a mutation means the caller thinks they act for
someone else; dropping it quietly would run the change for a different user
than the one they named.
"""

from pydantic import BaseModel, ConfigDict


class MutatingToolInput(BaseModel):
    """Base for every mutating tool's input.

    Handlers can read the idempotency key because the request row stores it.
    Only the tool pipeline spends it: claiming the key, comparing fingerprints
    and replaying the stored answer all happen in
    `helpdesk.idempotency.guard.run_once`. The key stays a plain `str`; the
    guard checks its length before any change runs.
    """

    model_config = ConfigDict(extra="forbid")

    idempotency_key: str


__all__ = ["MutatingToolInput"]
