"""The authenticated half of a resume: a decided request, and nothing else.

the design's "the resume call is authenticated: only the approval API, acting on
a recorded approver decision, may resume a run", made structural rather than
promised. A caller names a request id and supplies
nothing else. Everything the resumed run acts on is read here, from Postgres:

- the `AccessRequest` row, through the tenant-scoped repository, so a request
  outside the caller's tenant is a `NOT_FOUND` and never a resume;
- its STATUS, which `gates.decision_result` turns into the tool result the
  composition reads — a caller cannot name an outcome, so an approval API that
  is wrong or lying cannot turn a rejection into a grant;
- its `session_id`, which is the checkpoint thread (18a decision A1, a design note),
  so the run resumed is the run that filed this request and not one named by
  the caller;
- the paused checkpoint itself, whose `awaiting_request_id` must BE this
  request and whose outcome must still say the thread is paused.

Every refusal happens before any `Command` is sent, so a refused resume leaves
the thread exactly as it found it — the approver's real decision, when it
arrives, still has a pause to resume. Pinned by the four refusal cases of
`test_agent_approval_resume.py`, each of which reads the checkpoint back
afterwards or asserts the gateway was never called.

This module imports NO framework and no driver: LangGraph lives in `build.py`,
`checkpointer.py` and `await_approval.py`'s `interrupt`, and the database is
reached only through `helpdesk.core.repository`. A fourth importer would end
the design's boundary rule silently, which is why
`test_agent_approval_resume.py::test_resume_imports_no_framework` parses this
file rather than trusting the convention.
"""

import uuid
from typing import Any

from helpdesk.agent_api.checkpointer import thread_config
from helpdesk.agent_api.graph.build import Saver, TurnDeps, resume_graph
from helpdesk.agent_api.graph.gates import decision_result
from helpdesk.agent_api.graph.state import TurnOutcome, TurnState
from helpdesk.agent_api.metrics import record_turn_state
from helpdesk.shared.core.repository import Repository
from helpdesk.shared.errors import ErrorCode, NotFoundError, ToolError
from helpdesk.shared.models.access import AccessRequest

__all__ = ["ResumeRefusedError", "resume_turn"]

_OUTCOME_KEY = "outcome"
_AWAITING_KEY = "awaiting_request_id"


class ResumeRefusedError(ToolError):
    """A resume this module will not perform, raised before any `Command`.

    One class and one code for the four refusals, because they are one answer
    to the caller: the request as recorded is not something this thread is
    waiting to be told. Telling them apart across the boundary would say
    whether a given request is pending, was already resumed or belongs to a
    turn that never paused, which is more than an approval API needs and more
    than a caller with a wrong id should learn.

    `VALIDATION` is what every other "a value this component will not accept,
    checked before any effect runs" refusal in this repo answers with. Pinned
    with the untouched checkpoint beside it by
    `test_agent_approval_resume.py::test_a_still_pending_request_is_refused_and_the_checkpoint_stays_paused`,
    `::test_a_request_the_pause_is_not_waiting_on_is_refused`,
    `::test_a_request_with_no_session_id_is_refused`,
    `::test_a_thread_with_no_paused_checkpoint_is_refused` and
    `::test_a_second_resume_of_the_same_run_is_refused`.
    """

    code = ErrorCode.VALIDATION


async def _paused_channel_values(checkpointer: Saver, thread_id: str) -> dict[str, Any] | None:
    """The thread's latest checkpointed channel values, or `None` if there are none.

    Raw values rather than a validated `TurnState`: the two things read out of
    them are the outcome and the awaited request id, and a thread whose state
    cannot be validated for some unrelated reason should refuse the resume, not
    raise out of it.
    """
    saved = await checkpointer.aget_tuple(thread_config(thread_id))
    if saved is None:
        return None
    return dict(saved.checkpoint["channel_values"])


async def resume_turn(
    *,
    deps: TurnDeps,
    request_id: uuid.UUID,
    checkpointer: Saver,
) -> TurnState:
    """Resume the run that filed this request, acting on its recorded decision.

    `deps.db` is a tenant-bound session, so the repository read is the tenant
    check: a request belonging to another tenant is indistinguishable from one
    that does not exist, which is `NotFoundError`'s whole contract.

    The order below is the one the refusals need. The row is read first because
    everything else is derived from it; the decision gate comes before the
    thread is even named, so a still-pending request costs no checkpoint read;
    and the `awaiting_request_id` match is last because it is the only check
    that needs both halves. Nothing writes, and nothing invokes, until all five
    have passed.
    """
    request = await Repository(deps.db, AccessRequest).get(request_id)
    if request is None:
        raise NotFoundError(f"no access request {request_id} in this tenant")

    decision = decision_result(status=request.status, request_id=request_id)
    if decision is None:
        raise ResumeRefusedError(
            f"access request {request_id} is {request.status}, not a recorded decision"
        )

    if request.session_id is None:
        raise ResumeRefusedError(
            f"access request {request_id} was filed outside a turn and has no thread"
        )

    thread_id = str(request.session_id)
    values = await _paused_channel_values(checkpointer, thread_id)
    if values is None or str(values.get(_OUTCOME_KEY)) != TurnOutcome.AWAIT_APPROVAL.value:
        raise ResumeRefusedError(f"thread {thread_id} has no paused checkpoint to resume")

    awaiting = values.get(_AWAITING_KEY)
    if awaiting is None or str(awaiting) != str(request_id):
        raise ResumeRefusedError(
            f"thread {thread_id} is not waiting on access request {request_id}"
        )

    resumed = await resume_graph(
        deps=deps,
        thread_id=thread_id,
        decision=decision,
        checkpointer=checkpointer,
    )
    # The terminal of the resumed turn, counted here rather than inside the
    # graph: LangGraph replays the interrupting node, so a counter under a node
    # would count the pause again on every decision. A
    # refused resume above returns nothing and counts nothing -- no run was
    # resumed, so no turn ended.
    record_turn_state(resumed)
    return resumed
