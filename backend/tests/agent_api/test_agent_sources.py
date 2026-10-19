"""Which passages the Sources box shows: only the ones the reply marked as used.

Expected surface: ``helpdesk.agent_api.agent.sources.pick_used(citations, marks)``
(marks are 1-based, out of range ignored, repeats count once, search order kept)
and ``shown_citations(state)`` (``state.reply_citations`` when known, else
``state.citations``). Both the ``/chat`` body (``services.chat._rendered``) and
the stream's last event (``agent.graph._reply_data``) read ``shown_citations``.

Cost: nothing. Pure functions and plain objects.
"""

import uuid
from datetime import UTC, datetime

from helpdesk.agent_api.agent.graph import _reply_data
from helpdesk.agent_api.agent.sources import pick_used, shown_citations
from helpdesk.agent_api.agent.state import Citation, TurnOutcome, TurnState
from helpdesk.agent_api.services.chat import _rendered


def _cite(name: str) -> Citation:
    return Citation(
        document=f"fake-doc-{name}",
        title=f"Fake {name}",
        version="v1",
        heading_path=("Fake Section",),
        chunk_id=uuid.uuid5(uuid.NAMESPACE_URL, name),
        text=f"Obviously fake text {name}.",
        score=0.5,
    )


A, B, C = _cite("a"), _cite("b"), _cite("c")


def _state(
    citations: tuple[Citation, ...], reply_citations: tuple[Citation, ...] | None
) -> TurnState:
    return TurnState(
        tenant_id=uuid.UUID("22222222-2222-5222-8222-222222222222"),
        user_id=uuid.UUID("33333333-3333-5333-8333-333333333333"),
        session_id=uuid.UUID("11111111-1111-5111-8111-111111111111"),
        tenant_slug="sources-tenant",
        user_message="a fake question",
        outcome=TurnOutcome.DONE,
        reply="a fake reply",
        citations=citations,
        reply_citations=reply_citations,
        started_at=datetime(2026, 6, 2, 10, 15, tzinfo=UTC),
    )


def test_only_marked_passages_are_kept() -> None:
    """Catches an implementation that returns every passage whatever the marks."""
    assert pick_used([A, B, C], [2]) == (B,)


def test_kept_passages_stay_in_search_order() -> None:
    """Catches an implementation that returns passages in the order of the marks."""
    assert pick_used([A, B, C], [3, 1]) == (A, C)


def test_a_mark_marked_twice_counts_once() -> None:
    """Catches an implementation that returns a passage once per mark."""
    assert pick_used([A, B, C], [1, 1]) == (A,)


def test_the_last_number_is_kept() -> None:
    """Catches an off-by-one that drops mark N of N passages (exclusive upper limit)."""
    assert pick_used([A, B, C], [3]) == (C,)


def test_a_number_past_the_end_is_ignored() -> None:
    """Catches an implementation that raises, or wraps round, on mark N+1,
    which the writer can produce by miscounting."""
    assert pick_used([A, B, C], [4, 2]) == (B,)


def test_zero_and_below_are_ignored() -> None:
    """Catches an implementation that treats the marks as 0-based, or lets -1
    index from the end of the list: 0 would pick A and -1 would pick C."""
    assert pick_used([A, B, C], [0, -1]) == ()


def test_no_marks_shows_nothing() -> None:
    """Catches an implementation that falls back to every passage on an empty
    list: an empty list means the reply used none."""
    assert pick_used([A, B, C], []) == ()


def test_unknown_marks_show_every_passage() -> None:
    """Catches an implementation that shows nothing when `reply_citations` is
    None, which would hide every source on the fixed-sentence path."""
    assert shown_citations(_state((A, B), None)) == (A, B)


def test_known_marks_show_only_those() -> None:
    """Catches an output that still reads `state.citations`: the shown function
    would be right but the /chat body or the stream's last event would not."""
    state = _state((A, B), (B,))

    assert shown_citations(state) == (B,)
    assert _rendered(state).citations == [B.model_dump(mode="json")]
    assert _reply_data(state)["citations"] == [B.model_dump(mode="json")]


def test_known_empty_marks_show_nothing_on_both_outputs() -> None:
    """Catches `reply_citations or citations`, which treats the known-empty
    tuple as unknown and shows every passage."""
    state = _state((A, B), ())

    assert shown_citations(state) == ()
    assert _rendered(state).citations == []
    assert _reply_data(state)["citations"] == []
