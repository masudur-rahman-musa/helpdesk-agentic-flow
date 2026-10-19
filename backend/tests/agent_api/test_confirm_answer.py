"""Reading a yes or a no, and wording the question, with no graph at all.

Cost and isolation. Nothing backing: no Postgres, no gateway, no tools. Every
case calls one pure function in `helpdesk.agent_api.agent.confirmation` on plain
values, so the file runs in milliseconds and shares no state.

Surface the implementer must provide (names fixed by the plan):
`Answer` (YES, NO, OTHER), `read_answer`, `OPEN_QUESTION_LIFE`, `OpenQuestion`,
`parse_open_question`, `question_reply`, `next_step`, `NOTHING_FILED_REPLY`, and
`ResolvedEntities.system_name` / `role_name` in `state.py`.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from helpdesk.agent_api.agent.confirmation import (
    NOTHING_FILED_REPLY,
    OPEN_QUESTION_LIFE,
    Answer,
    OpenQuestion,
    next_step,
    parse_open_question,
    question_reply,
    read_answer,
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
        role_slug="wiki-viewer",
        role_name="Viewer",
    )


def _classified(decision: str, *slots: str, ok: bool = True) -> ToolResult:
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
            "effective_expiry_days": 30,
        },
    )


def test_the_question_names_role_and_system() -> None:
    """Catches a question worded with the slugs or ids ("wiki-viewer") instead of
    the names the person knows, or with the role and system swapped."""
    reply = question_reply(_resolved(), ())

    assert reply.startswith("Do you mean Viewer access in Internal Wiki?")


def test_the_question_ends_with_the_next_step() -> None:
    """Catches a question that stops after "Do you mean ...?": the plan says the
    reply also tells the person what happens next, read from the classify result."""
    results = (_classified("approval", "department_manager", "system_owner"),)

    reply = question_reply(_resolved(), results)

    assert reply.startswith("Do you mean Viewer access in Internal Wiki?")
    assert reply.removeprefix("Do you mean Viewer access in Internal Wiki?").strip() != ""
    assert next_step(results) in reply


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


def test_the_nothing_filed_reply_says_nothing_was_filed() -> None:
    """Catches an empty or unrelated constant: the person who says "no" must be
    told in words that nothing was filed, and be asked what they need instead."""
    assert "nothing" in NOTHING_FILED_REPLY.lower()
    assert "filed" in NOTHING_FILED_REPLY.lower()


# --- the stored question --------------------------------------------------------


def test_an_open_question_lives_exactly_24_hours() -> None:
    """Catches a changed life: the plan and the placeholders list fix it at 24
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
