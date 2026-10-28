"""Reading a yes or a no, and wording the question, with no graph at all.

Cost and isolation. Nothing backing: no Postgres, no gateway, no tools. Every
case calls one pure function in `helpdesk.agent_api.agent.confirmation` on plain
values, so the file runs in milliseconds and shares no state.

Surface the implementer must provide (names fixed by the requirements):
`Answer` (YES, NO, OTHER), `read_answer`, `OPEN_QUESTION_LIFE`, `OpenQuestion`,
`parse_open_question`, `summary_reply` (it replaces `question_reply`;
`summary_reply(resolved, results, held_calls)`), `next_step`, and
`ResolvedEntities.system_name` / `role_name` in `state.py`.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from helpdesk.agent_api.agent.confirmation import (
    OPEN_QUESTION_LIFE,
    Answer,
    OpenQuestion,
    next_step,
    parse_open_question,
    read_answer,
    summary_reply,
)
from helpdesk.agent_api.agent.state import (
    Intent,
    IntentEntities,
    IntentExtraction,
    ResolvedEntities,
    ToolResult,
    ValidatedCall,
)

CLASSIFY = "classify_access_request"
CREATE = "create_access_request"

NOW = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)

# Obviously fake ids, tests only.
USER_ID = uuid.UUID("44444444-4444-5444-8444-444444444444")
ROLE_ID = uuid.UUID("55555555-5555-5555-8555-555555555555")
SYSTEM_ID = uuid.UUID("66666666-6666-5666-8666-666666666666")

YES_WORDS = ["yes", "yep", "yeah", "correct", "right", "sure", "ok", "okay", "confirm"]
NO_WORDS = ["no", "nope", "wrong"]


# --- reading the answer ---------------------------------------------------------


@pytest.mark.parametrize("word", YES_WORDS)
def test_every_listed_yes_word_alone_reads_as_yes(word: str) -> None:
    """Catches a yes list that leaves out one of the nine words, so that person's
    "okay" would be read as something else and nothing would be filed."""
    assert read_answer(word) is Answer.YES


@pytest.mark.parametrize(
    "message",
    ["yes", "Yes, that is right.", "ok", "Sure.", "  YES  ", "Yes!\n", "okay, go ahead"],
)
def test_yes_words_read_as_yes(message: str) -> None:
    """Catches a reader that is case-sensitive, trips on punctuation or on
    surrounding whitespace, so a natural "Yes, that is right." files nothing."""
    assert read_answer(message) is Answer.YES


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("yes please do that now", Answer.YES),
        ("yes please do that right now", Answer.OTHER),
    ],
)
def test_a_yes_of_five_words_is_yes_and_six_is_not(message: str, expected: Answer) -> None:
    """Catches a yes length cap that is off by one: a cap of 4 turns the five-word
    row into other, and a cap of 6 or none turns the six-word row into yes, which
    would file on a long message that only starts with "yes"."""
    assert read_answer(message) is expected


@pytest.mark.parametrize("word", NO_WORDS)
def test_every_listed_no_word_alone_reads_as_no(word: str) -> None:
    """Catches a no list that leaves out one of the three words, such as "wrong"."""
    assert read_answer(word) is Answer.NO


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("No, that's wrong", Answer.NO),
        ("no, I meant CRM", Answer.OTHER),
    ],
)
def test_a_bare_no_of_three_words_is_no_and_four_is_other(message: str, expected: Answer) -> None:
    """Catches a no length cap that is off by one: a cap of 4 or more reads the
    correction "no, I meant CRM" as a bare no and drops what the person wanted
    instead of asking again with it, and a cap of 2 refuses a plain "No, wrong"."""
    assert read_answer(message) is expected


@pytest.mark.parametrize(
    "message",
    [
        "How do I reset my password?",
        "yesterday I asked",
        "okayish",
        "nobody told me",
        "please yes",
        "I said yes",
        "",
        "   ",
        "yes no no no no no",
    ],
)
def test_an_unrelated_message_is_other(message: str) -> None:
    """Catches a reader that matches a prefix instead of a whole first word
    ("yesterday", "okayish", "nobody"), one that looks for a yes word anywhere in
    the message ("please yes", "I said yes"), and one that raises on an empty
    message. Each of those would file, or crash, on something that is not a yes."""
    assert read_answer(message) is Answer.OTHER


# --- wording the question -------------------------------------------------------


def _resolved() -> ResolvedEntities:
    return ResolvedEntities(
        system_id=SYSTEM_ID,
        system_slug="internal-wiki",
        system_name="Internal Wiki",
        role_id=ROLE_ID,
        role_slug="editor",
        role_name="Internal Wiki editor",
    )


def _classified(decision: str, *slots: str, ok: bool = True, days: int = 30) -> ToolResult:
    if not ok:
        return ToolResult(tool=CLASSIFY, ok=False)
    return ToolResult(
        tool=CLASSIFY,
        ok=True,
        payload={
            "decision": decision,
            "risk_class": "low",
            "reasons": [],
            "required_approvers": [{"slot": slot, "user_id": None} for slot in slots],
            "effective_expiry_days": days,
        },
    )


def _held(
    reason: str = "Obviously fake reason.", days: int | None = None
) -> tuple[ValidatedCall, ...]:
    """The held round: a classify call and the create call a yes would file."""
    return (
        ValidatedCall(
            tool=CLASSIFY,
            arguments={"role_id": str(ROLE_ID), "requested_expiry_days": 99},
        ),
        ValidatedCall(
            tool=CREATE,
            arguments={
                "target_user_id": str(USER_ID),
                "role_id": str(ROLE_ID),
                "justification": reason,
                "requested_expiry_days": days,
                "citations": [],
                "idempotency_key": "fake-key-1",
            },
        ),
    )


FILE_LINE = "Say yes to file, or tell me what to change."


def test_the_question_names_role_and_system() -> None:
    """Catches a summary that is still the old "Do you mean ...?" question, or
    one worded with the slug or the stored name instead of the plain name: the
    reply must open with "I'll file this:" and the plain role name."""
    reply = summary_reply(_resolved(), (_classified("auto", days=10),), _held())

    assert reply.startswith("I'll file this: Internal Wiki editor, ")
    assert "Do you mean" not in reply


def test_the_question_ends_with_the_next_step() -> None:
    """Catches a summary that stops after the owner's line: the sentence about
    what happens after yes, read from the classify result, must follow it,
    last, once."""
    results = (_classified("approval", "department_manager", "system_owner"),)

    reply = summary_reply(_resolved(), results, _held())

    assert reply.endswith(f"{FILE_LINE} {next_step(results)}")
    assert reply.count(next_step(results)) == 1


def test_the_summary_names_role_days_and_reason() -> None:
    """Catches a summary that leaves out the days or the reason, or words them
    differently: classify days 10 and the held reason must give the exact
    sentence the owner wrote, the reason between straight quotes."""
    reply = summary_reply(
        _resolved(),
        (_classified("auto", days=10),),
        _held("Writing the onboarding guide"),
    )

    assert reply.startswith(
        "I'll file this: Internal Wiki editor, 10 days, "
        'reason "Writing the onboarding guide". '
        "Say yes to file, or tell me what to change."
    )


def test_the_summary_ends_with_what_happens_after_yes() -> None:
    """Catches a summary built without the next step: an auto classification
    must end the reply with the grant-right-away sentence."""
    reply = summary_reply(_resolved(), (_classified("auto", days=10),), _held())

    assert reply.endswith("If you say yes, it is granted right away.")


def test_a_template_role_shows_its_default_length_and_reason() -> None:
    """Catches a summary that hides the length or reason of a template role
    because the person gave none: the classify days (the role's default, 365)
    and the template reason filled in on the held call must both show."""
    template_reason = "Part of my department's starting access."

    reply = summary_reply(
        _resolved(), (_classified("auto", days=365),), _held(template_reason, days=None)
    )

    assert "365 days" in reply
    assert f'reason "{template_reason}"' in reply


def test_one_day_reads_as_a_day() -> None:
    """Catches a summary that always pluralises: one day reads "1 day," and
    never "1 days"."""
    reply = summary_reply(_resolved(), (_classified("auto", days=1),), _held())

    assert "1 day," in reply
    assert "1 days" not in reply


def test_two_days_still_read_as_days() -> None:
    """Catches a singular rule that fires for every small number or for any
    number that ends in 1: 2 days and 11 days both stay plural."""
    two = summary_reply(_resolved(), (_classified("auto", days=2),), _held())
    eleven = summary_reply(_resolved(), (_classified("auto", days=11),), _held())

    assert "2 days," in two
    assert "11 days," in eleven


def test_the_classified_days_win_over_the_asked_days() -> None:
    """Catches a summary that quotes the days the person asked for when the
    policy shortened them: asked 30, classified 7, so the reply says 7 days,
    which is what a yes would actually grant."""
    reply = summary_reply(_resolved(), (_classified("auto", days=7),), _held(days=30))

    assert "7 days," in reply
    assert "30 days" not in reply


def test_no_classification_falls_back_to_the_asked_days_then_the_usual_length() -> None:
    """Catches a summary that invents a number with no classify result, reads
    the days from the held classify call (99 here) instead of the create call,
    or leaves a hole: with no classification the asked 30 days show, and with
    none asked the words are "its usual length"."""
    asked = summary_reply(_resolved(), (), _held(days=30))
    unasked = summary_reply(_resolved(), (), _held(days=None))
    failed = summary_reply(_resolved(), (_classified("auto", ok=False),), _held(days=None))

    assert "I'll file this: Internal Wiki editor, 30 days, reason " in asked
    assert "99" not in asked
    assert "I'll file this: Internal Wiki editor, its usual length, reason " in unasked
    assert "99" not in unasked
    assert "its usual length" in failed


def test_the_next_step_names_the_seats_for_approval() -> None:
    """Catches a next step that says only "needs approval" without naming who, and
    one that prints the raw slot names ("department_manager") instead of the
    words a person uses."""
    step = next_step((_classified("approval", "department_manager", "system_owner"),))

    assert "your manager and the system owner" in step
    assert "department_manager" not in step
    assert "system_owner" not in step


@pytest.mark.parametrize(
    ("slot", "words"),
    [("it_admin", "IT admin"), ("hr", "HR"), ("system_owner", "the system owner")],
)
def test_the_next_step_names_every_other_seat(slot: str, words: str) -> None:
    """Catches a seat table that misses it_admin, hr or system_owner, so an
    approval chain through them is described with a blank or a raw slot name."""
    assert words in next_step((_classified("approval", slot),))


def test_the_next_step_for_auto_blocked_and_unknown() -> None:
    """Catches a next step that gives the same sentence for every decision: auto
    must promise the grant right away, blocked must say the rules block it, and
    with no classify result nothing may be promised, only that the policy
    decides once the request is filed."""
    auto = next_step((_classified("auto"),))
    blocked = next_step((_classified("blocked"),))
    unknown = next_step(())

    assert "granted right away" in auto
    assert "rules block it" in blocked
    assert "policy decides" in unknown
    assert "filed" in unknown
    assert "granted right away" not in blocked
    assert "granted right away" not in unknown
    assert "rules block it" not in auto


def test_the_next_step_reads_the_last_ok_classify_result() -> None:
    """Catches a reader that takes the first classify result instead of the last,
    one that lets a failed classify call or a create result stand in for the
    verdict, and one that reads a failed call's empty payload as "unknown" when an
    earlier ok result exists."""
    results = (
        _classified("auto"),
        _classified("approval", "hr"),
        _classified("blocked", ok=False),
        ToolResult(tool=CREATE, ok=True, payload={"decision": "blocked"}),
    )

    step = next_step(results)

    assert "HR" in step
    assert "granted right away" not in step
    assert "rules block it" not in step


def test_a_create_result_alone_does_not_count_as_a_classification() -> None:
    """Catches a reader that scans every result for a `decision` key: only
    `classify_access_request` gives the verdict this sentence is about."""
    only_create = (ToolResult(tool=CREATE, ok=True, payload={"decision": "auto"}),)

    assert next_step(only_create) == next_step(())


# --- the stored question --------------------------------------------------------


def test_an_open_question_lives_exactly_24_hours() -> None:
    """Catches a changed life: the requirements fix it at 24
    hours, and the boundary tests below are measured against this value."""
    assert timedelta(hours=24) == OPEN_QUESTION_LIFE


def _question(asked_at: datetime) -> OpenQuestion:
    return OpenQuestion(
        asked_at=asked_at,
        message="I need viewer access to the wiki",
        calls=(
            ValidatedCall(
                tool=CREATE,
                arguments={
                    "target_user_id": str(USER_ID),
                    "role_id": str(ROLE_ID),
                    "justification": "Obviously fake justification.",
                    "requested_expiry_days": None,
                    "citations": [],
                    "idempotency_key": "fake-key-1",
                },
            ),
        ),
        extraction=IntentExtraction(
            intent=Intent.REQUEST_ACCESS,
            entities=IntentEntities(
                system="internal-wiki",
                role="wiki-viewer",
                target_user=None,
                department=None,
                expiry_days=None,
                justification=None,
            ),
            confidence=0.95,
            clarifying_question=None,
            second_request=None,
        ),
        resolved=_resolved(),
    )


def _raw(asked_at: datetime) -> dict[str, Any]:
    """The question as it comes back out of the JSONB column: plain JSON."""
    return _question(asked_at).model_dump(mode="json")


def test_a_stored_question_round_trips_through_plain_json() -> None:
    """Catches a model that cannot be rebuilt from what Postgres hands back (ids
    and dates as strings), which would make every reload forget the question."""
    parsed = parse_open_question(_raw(NOW - timedelta(minutes=5)), now=NOW)

    assert parsed == _question(NOW - timedelta(minutes=5))


@pytest.mark.parametrize(
    ("age", "kept"),
    [
        (OPEN_QUESTION_LIFE - timedelta(seconds=1), True),
        (OPEN_QUESTION_LIFE, False),
        (OPEN_QUESTION_LIFE + timedelta(seconds=1), False),
    ],
)
def test_a_question_is_kept_just_under_24_hours_and_dropped_at_24(
    age: timedelta, kept: bool
) -> None:
    """Catches an age check that is off by one at the edge: `>` instead of `>=`
    keeps a question at exactly 24 hours, and a shorter life drops one at 24
    hours minus a second."""
    parsed = parse_open_question(_raw(NOW - age), now=NOW)

    assert (parsed is not None) is kept


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "text",
        42,
        [],
        {},
        {"calls": "x"},
    ],
)
def test_a_bad_shape_counts_as_no_question(raw: object) -> None:
    """Catches a reader that raises, or builds a half question, on a missing or
    badly shaped value: the question would then crash the turn, or file from a
    guess."""
    assert parse_open_question(raw, now=NOW) is None


def test_a_question_with_a_missing_field_counts_as_no_question() -> None:
    """Catches a model whose fields have defaults: a stored question that lost its
    calls must not read as a question with nothing to file."""
    raw = _raw(NOW)
    del raw["calls"]

    assert parse_open_question(raw, now=NOW) is None
