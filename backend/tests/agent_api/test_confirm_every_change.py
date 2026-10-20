"""Every change asks first and runs once, on whole turns.

Surface the implementer must provide (names fixed by an earlier change):
`gates.held_round_outcome(*, holds_change, confirmed, already_requested=False)`;
in `confirmation.py`: `change_summary(calls, results, resolved)` (code-written
text per tool, ending `CHANGE_LINE`), `NOT_FOUND_APPROVALS_REPLY`; a held round
is any round with a tool whose arguments carry an idempotency key.

Cost and isolation. No Postgres, no socket: whole turns run on the `ChatDb`,
`Tools` and `play` fakes of `test_draft_turns.py`, with the gateway staged
answer by answer (a request past the staging fails the test). Each test builds
its own fakes and shares nothing.
"""

from typing import Any

from helpdesk.agent_api.agent.confirmation import (
    NOT_FOUND_APPROVALS_REPLY,
    NOTHING_FILED_REPLY,
    change_summary,
)
from helpdesk.agent_api.agent.state import ResolvedEntities, ToolResult, TurnOutcome, ValidatedCall
from tests.agent_api.confirm_support import (
    APPROVE,
    LIST_APPROVALS,
    REQ_6,
    REQ_13,
    REQ_19,
    REQ_UNLISTED,
    RESET,
    TICKET,
    UNLOCK,
    approval_results,
    approve_call,
    listed_approvals,
    vpn_db,
)
from tests.agent_api.test_draft_turns import (
    EMPTY_PLAN,
    Tools,
    extraction_json,
    gateway_of,
    plan_json,
    play,
    wiki_db,
)

CHANGE_LINE = "Say yes to go ahead, or no to stop."
RESET_SUMMARY = f"I'll reset your password. {CHANGE_LINE}"
UNLOCK_SUMMARY = f"I'll unlock your account. {CHANGE_LINE}"
TICKET_TITLE = "Remove VPN access"
DONE_REPLY = "Your fake request was looked at."
"""A composed reply with no decision word, so the check lets it through."""


def _tools() -> Tools:
    """Answers for every tool these turns may reach. A tool outside the list fails the test."""
    return Tools(
        {
            RESET: {"ok": True},
            UNLOCK: {"ok": True},
            TICKET: {"ticket_number": "TCK-0001"},
            LIST_APPROVALS: listed_approvals(),
            APPROVE: {"status": "approved"},
        }
    )


def _reset_turn(db: Any, tools: Tools) -> Any:
    return play(
        db,
        "I forgot my password",
        gateway_of(extraction_json("reset_password"), plan_json((RESET, {}))),
        tools,
    )


def _ticket_turn(db: Any, tools: Tools) -> Any:
    ticket = {"kind": "general", "title": TICKET_TITLE, "body": "Obviously fake: remove my VPN."}
    return play(
        db,
        "remove my VPN access",
        gateway_of(extraction_json("revoke_access", system="vpn"), plan_json((TICKET, ticket))),
        tools,
    )


# --- the person is asked first, and a yes runs the change once -------------------------


def test_password_reset_asks_then_runs_once() -> None:
    """Catches the live failure of 2026-10-21, a password reset that runs on the
    first message: "I forgot my password" must run no tool and show the fixed
    summary, and only the "yes reset it" that follows runs `reset_own_password`,
    once, with a key the agent derived."""
    db = wiki_db()
    tools = _tools()

    first = _reset_turn(db, tools)

    assert tools.names == []
    assert first.outcome is TurnOutcome.CONFIRM
    assert first.reply == RESET_SUMMARY

    second = play(db, "yes reset it", gateway_of(EMPTY_PLAN, DONE_REPLY), tools)

    assert tools.names == [RESET]
    assert tools.calls[0][1]["idempotency_key"]
    assert second.reply == DONE_REPLY


def test_unlock_asks_then_runs_once() -> None:
    """Catches an unlock that runs on "my account is locked": the first turn runs
    nothing and shows the fixed unlock summary, and the "yes" runs
    `unlock_own_account` exactly once."""
    db = wiki_db()
    tools = _tools()

    first = play(
        db,
        "my account is locked",
        gateway_of(extraction_json("unlock_account"), plan_json((UNLOCK, {}))),
        tools,
    )

    assert tools.names == []
    assert first.reply == UNLOCK_SUMMARY

    play(db, "yes", gateway_of(EMPTY_PLAN, DONE_REPLY), tools)

    assert tools.names == [UNLOCK]


def test_a_second_yes_runs_nothing() -> None:
    """Catches a held change that survives its own yes (or a yes that runs
    whatever the planner proposes): reset, yes, yes must run `reset_own_password`
    exactly once over the three turns. The third turn has the planner propose the
    reset again, as the live chat did, so only the hold on a new turn keeps it
    from running, and the reply asks again."""
    db = wiki_db()
    tools = _tools()
    _reset_turn(db, tools)
    play(db, "yes", gateway_of(EMPTY_PLAN, DONE_REPLY), tools)
    assert tools.names == [RESET], "premise: the first yes ran it"

    third = play(
        db,
        "yes",
        gateway_of(extraction_json("reset_password"), plan_json((RESET, {}))),
        tools,
    )

    assert tools.names == [RESET]
    assert third.outcome is TurnOutcome.CONFIRM
    assert third.reply == RESET_SUMMARY


def test_revoke_ticket_is_one_ticket() -> None:
    """Catches the live failure of one ticket per "yes": "remove my VPN access"
    opens none, shows the ticket in the summary, and the two yes messages after
    it call `create_ticket` exactly once between them. The third turn has the
    planner propose the ticket again, so only the hold stops a second one."""
    db = vpn_db()
    tools = _tools()

    first = _ticket_turn(db, tools)

    assert tools.names == []
    assert first.reply == f"I'll open a ticket: {TICKET_TITLE}. {CHANGE_LINE}"

    play(db, "yes", gateway_of(EMPTY_PLAN, DONE_REPLY), tools)
    assert tools.names == [TICKET], "premise: the first yes opened it"
    again = play(
        db,
        "yes",
        gateway_of(
            extraction_json("revoke_access", system="vpn"),
            plan_json((TICKET, {"kind": "general", "title": TICKET_TITLE, "body": "Fake."})),
        ),
        tools,
    )

    assert tools.names == [TICKET]
    assert again.outcome is TurnOutcome.CONFIRM


def test_no_at_a_held_change_runs_nothing() -> None:
    """Catches a "no" that still runs the held change, or asks the model what to
    do: after the reset is held, a bare no ends on the fixed nothing-filed reply
    with no tool and no model request (none is staged)."""
    db = wiki_db()
    tools = _tools()
    _reset_turn(db, tools)

    answer = play(db, "no", gateway_of(), tools)

    assert tools.names == []
    assert answer.reply == NOTHING_FILED_REPLY


# --- approvals: list first, ask, then approve what was listed ---------------------------


def _approve_all_turn(db: Any, tools: Tools, *round_two: tuple[str, dict[str, Any]]) -> Any:
    """ "approve all of them": the planner lists first, then proposes the approvals."""
    return play(
        db,
        "approve all of them",
        gateway_of(
            extraction_json("approvals"),
            plan_json((LIST_APPROVALS, {})),
            plan_json(*round_two),
        ),
        tools,
    )


def test_approve_all_lists_and_asks() -> None:
    """Catches the live failure of three requests approved with nothing listed:
    "approve all of them" must run only the listing, name REQ-0006, REQ-0013 and
    REQ-0019 with their plain role in the reply, and say how many; the "yes" then
    runs three `approve_request` calls, one per listed request, though that yes
    turn has no listing of its own."""
    db = wiki_db()
    tools = _tools()

    first = _approve_all_turn(
        db, tools, approve_call(REQ_6), approve_call(REQ_13), approve_call(REQ_19)
    )

    assert tools.names == [LIST_APPROVALS]
    assert first.outcome is TurnOutcome.CONFIRM
    assert first.reply is not None
    assert first.reply.startswith("I'll approve these 3 requests: ")
    assert "REQ-0006 Data Warehouse engineer" in first.reply
    assert "REQ-0013 Data Warehouse analyst" in first.reply
    assert "REQ-0019 Data Warehouse admin" in first.reply
    assert first.reply.endswith(CHANGE_LINE)

    play(db, "yes", gateway_of(EMPTY_PLAN, DONE_REPLY), tools)

    approved = [arguments["request_id"] for name, arguments in tools.calls if name == APPROVE]
    assert sorted(approved) == sorted(str(request_id) for request_id in (REQ_6, REQ_13, REQ_19))


def test_approve_unlisted_request_is_dropped() -> None:
    """Catches an approval of a request the listing never showed (a made-up or
    stale id): the only approve in the round is not in this turn's
    `list_pending_approvals` result, so nothing is held, nothing runs and the
    reply is the fixed "couldn't find" line. No model answer is staged for a
    reply, so the line must be code-written."""
    db = wiki_db()
    tools = _tools()

    turn = _approve_all_turn(db, tools, approve_call(REQ_UNLISTED))

    assert tools.names == [LIST_APPROVALS]
    assert turn.reply == NOT_FOUND_APPROVALS_REPLY


def test_a_listed_approval_beside_an_unlisted_one_keeps_only_the_listed() -> None:
    """Catches a check that drops the whole round, or none of it, when one id is
    unlisted: the listed REQ-0013 stays held and runs on the yes, and the
    unlisted id never reaches `approve_request`."""
    db = wiki_db()
    tools = _tools()

    first = _approve_all_turn(db, tools, approve_call(REQ_13), approve_call(REQ_UNLISTED))

    assert first.outcome is TurnOutcome.CONFIRM
    assert first.reply is not None
    assert "REQ-0013" in first.reply
    assert tools.names == [LIST_APPROVALS]

    play(db, "yes", gateway_of(EMPTY_PLAN, DONE_REPLY), tools)

    approved = [arguments["request_id"] for name, arguments in tools.calls if name == APPROVE]
    assert approved == [str(REQ_13)]


# --- the fixed summaries, one per tool (direct) -----------------------------------------


def _call(tool: str, **arguments: Any) -> ValidatedCall:
    return ValidatedCall(tool=tool, arguments={"idempotency_key": "fake-key", **arguments})


def test_summaries_are_the_exact_fixed_words() -> None:
    """Catches a summary reworded, or missing the closing line: each tool's
    summary is pinned word for word, including the ticket title, because the
    person says yes to exactly this text."""
    no_results: tuple[ToolResult, ...] = ()
    resolved = ResolvedEntities()

    assert change_summary([_call(RESET)], no_results, resolved) == RESET_SUMMARY
    assert change_summary([_call(UNLOCK)], no_results, resolved) == UNLOCK_SUMMARY
    ticket = _call(TICKET, kind="general", title="Remove VPN access", body="Fake.")
    assert (
        change_summary([ticket], no_results, resolved)
        == f"I'll open a ticket: Remove VPN access. {CHANGE_LINE}"
    )


def test_the_approval_summary_counts_and_names_each_listed_request() -> None:
    """Catches a count taken from the listing instead of the held calls: two
    approvals are held out of three listed, so the summary must say 2 and name
    only REQ-0006 and REQ-0019."""
    calls = [
        _call(APPROVE, request_id=str(REQ_6)),
        _call(APPROVE, request_id=str(REQ_19)),
    ]

    summary = change_summary(calls, approval_results(), ResolvedEntities())

    assert summary.startswith("I'll approve these 2 requests: ")
    assert "REQ-0006 Data Warehouse engineer" in summary
    assert "REQ-0019 Data Warehouse admin" in summary
    assert "REQ-0013" not in summary
    assert summary.endswith(CHANGE_LINE)


def test_the_not_found_line_is_the_fixed_words() -> None:
    """Catches a reworded fallback: the line for an approval that is not among
    the listed ones is pinned word for word."""
    assert NOT_FOUND_APPROVALS_REPLY == (
        "I couldn't find those requests among the ones waiting for you."
    )
