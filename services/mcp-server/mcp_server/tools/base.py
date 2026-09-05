"""What every mutating tool's input shares, in one place.

Lived in `tools/helpdesk.py` until an earlier change: with a second tool module
(`tools/access.py`) registering a mutating tool, leaving the base class in
the first module would force every future tool module to import the helpdesk
one for a class that has nothing to do with helpdesk tickets (10b owed item
(a), a project rule — both import sites moved in the same edit).

Mutating inputs FORBID extra fields where read inputs ignore them. On a
mutation an unexpected `user_id` in the arguments is a caller who believes
they are acting on someone else, and dropping it silently would run the
effect on a different user than the one the caller named. Pinned by
`test_mcp_helpdesk_tools.py::test_a_user_id_in_the_arguments_is_refused_with_no_effect`
and, for the access tool, by
`test_mcp_create_access_request.py::test_an_extra_argument_is_refused_with_no_row_and_no_grant`.
"""

from pydantic import BaseModel, ConfigDict


class MutatingToolInput(BaseModel):
    """Base for every mutating tool's input.

    The idempotency key rides on this model, so every handler can see it.
    That is deliberate rather than tolerated: the design
    gives `access_requests` an `idempotency_key` column, so a handler that
    could not read the key could not persist the value the spec stores.
    What no handler may do is SPEND the key — claiming it, comparing
    fingerprints and replaying is `helpdesk.idempotency.guard.run_once`,
    which the pipeline alone calls, pinned by
    `test_mcp_create_access_request.py::test_a_replayed_call_is_byte_identical_and_files_one_row_and_one_grant`.

    The key stays `str`: the design's own signature says the UUID shape is
    the agent's promise, not a server constraint; the guard
    length-checks it against the column before any effect runs.
    """

    model_config = ConfigDict(extra="forbid")

    idempotency_key: str


__all__ = ["MutatingToolInput"]
