"""The one rule for every required field.

Pure: no database, no model, no tools. The list, the ask ladder, the stored
counts and the non-answers are plain functions, so each case here calls them
directly.

Names the implementation adds (fixed by the plan):

* ``required_fields.REQUIRED_FIELDS`` (one row per field, in the order system,
  target_user, department, expiry_days, justification), ``RequiredField``,
  ``AskStep``, ``Ask``, ``next_ask``, ``parse_asks``, ``is_non_answer``.
* ``clarify_cap.stored_kind(..., asks=...)`` and ``clarify_cap.parse_asked_asks``.
* ``DETAIL_WORDS`` and ``reason_question`` leave ``clarify_cap``.

The wording below is written out in full on purpose. A test that read the line
from the list would pass whatever the list says.
"""

from datetime import UTC, datetime, timedelta

import pytest

from helpdesk.agent_api.agent import clarify_cap, gates
from helpdesk.agent_api.agent.clarify_cap import (
    CLARIFY_CAP_LIFE,
    QuestionKind,
    details_question,
    parse_asked_asks,
    stored_kind,
)
from helpdesk.agent_api.agent.required_fields import (
    REQUIRED_FIELDS,
    AskStep,
    is_non_answer,
    next_ask,
    parse_asks,
)
from helpdesk.agent_api.agent.state import Intent, IntentEntities, IntentExtraction

NOW = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)

REASON_REQUIRED = (
    "The business reason is required. Approvers need it to decide. "
    "Please give a short reason, even one line."
)
SYSTEM_REQUIRED = (
    "The system is required. I need it to find the right access. "
    "Please give the system's name, even roughly."
)
PERSON_REQUIRED = (
    "The person is required. I need it to know whose account changes. "
    "Please give their name or email."
)
DEPARTMENT_REQUIRED = (
    "The department is required. I need it to pick the right access. "
    "Please give the department's name."
)
STOP_TAIL = " You can start again when you have one."
REASON_STOP = "I can't file this without a reason." + STOP_TAIL

# name -> (first-ask words, required line, stop name)
TABLE = {
    "system": ("which system you need", SYSTEM_REQUIRED, "a system"),
    "target_user": ("who the person is", PERSON_REQUIRED, "the person"),
    "department": ("which department", DEPARTMENT_REQUIRED, "a department"),
    "justification": ("the business reason", REASON_REQUIRED, "a reason"),
}
ASKED_FIELDS = tuple(TABLE)


def _first(missing: set[str], *, length_missing: bool = False) -> str:
    ask = next_ask(missing, asks={}, details_asked=False, length_missing=length_missing)
    assert ask is not None
    return ask.reply


# --- the list -------------------------------------------------------------------


def test_the_list_holds_the_five_fields_in_the_fixed_order() -> None:
    """Catches a list in another order (the second ask names fields in list order,
    and the stop picks the first in list order, so a reorder changes both) or one
    that adds or drops a field."""
    assert [row.name for row in REQUIRED_FIELDS] == [
        "system",
        "target_user",
        "department",
        "expiry_days",
        "justification",
    ]


def test_every_required_name_has_one_row() -> None:
    """Catches a required entity with no row in the list (it would get a special
    case or no ask at all), a name listed twice, and a row with an empty
    first-ask, "is required" sentence or stop name."""
    names = [row.name for row in REQUIRED_FIELDS]
    rows = {row.name: row for row in REQUIRED_FIELDS}
    required_anywhere = set().union(*gates.REQUIRED_ENTITIES.values())

    assert required_anywhere, "the premise: some entity is required"
    for name in required_anywhere:
        assert names.count(name) == 1, name
        assert rows[name].first_ask, name
        assert rows[name].required_line, name
        assert rows[name].stop_name, name


def test_the_length_has_first_ask_words_but_is_never_required() -> None:
    """Catches a length row that carries an "is required" line or a stop name (the
    length would then be asked twice and could stop a chat, but the role's default
    length always applies)."""
    row = next(r for r in REQUIRED_FIELDS if r.name == "expiry_days")

    assert row.first_ask == "how long you need it for"
    assert row.required_line is None
    assert row.stop_name is None


@pytest.mark.parametrize("name", ASKED_FIELDS)
def test_each_row_holds_the_planned_words(name: str) -> None:
    """Catches a row whose words drifted from the plan: the first-ask words, the
    "is required" sentence (what, why, how to give it) and the stop name are the
    owner's text, pinned letter for letter."""
    row = next(r for r in REQUIRED_FIELDS if r.name == name)
    first_ask, required_line, stop_name = TABLE[name]

    assert row.first_ask == first_ask
    assert row.required_line == required_line
    assert row.stop_name == stop_name


# --- the ladder -----------------------------------------------------------------


def test_the_first_ask_is_the_normal_question() -> None:
    """Catches a first ask that jumps to the "is required" sentence: nothing was
    asked yet, so the normal question must be used, and one ask is counted."""
    ask = next_ask({"justification"}, asks={}, details_asked=False)

    assert ask is not None
    assert ask.step is AskStep.FIRST
    assert ask.reply == "Before I check this, tell me the business reason."
    assert ask.asks == {"justification": 1}


def test_the_first_ask_names_every_missing_field_and_the_length_in_list_order() -> None:
    """Catches a first ask that names fields in the order they were found, drops
    the person or the department (the old text only knew three), or leaves the
    length out when it is missing."""
    assert _first({"target_user", "department"}) == (
        "Before I check this, tell me who the person is and which department."
    )
    assert _first({"justification", "system"}, length_missing=True) == (
        "Before I check this, tell me which system you need, "
        "how long you need it for and the business reason."
    )


def test_the_second_ask_for_the_reason_is_the_owners_line() -> None:
    """Catches a second ask that repeats the first question, or words the "is
    required" line differently: the reason asked once and still missing must give
    exactly the owner's sentence, and count the second ask."""
    ask = next_ask({"justification"}, asks={"justification": 1}, details_asked=True)

    assert ask is not None
    assert ask.step is AskStep.SECOND
    assert ask.reply == REASON_REQUIRED
    assert ask.asks == {"justification": 2}


def test_two_asks_then_it_stops() -> None:
    """Catches a ladder that asks a third time, or stops with other words: the
    reason asked twice and still missing must give the stop with exactly the
    owner's line, and keep no count (the next message starts clean)."""
    ask = next_ask({"justification"}, asks={"justification": 2}, details_asked=True)

    assert ask is not None
    assert ask.step is AskStep.STOP
    assert ask.reply == REASON_STOP
    assert ask.asks == {}


@pytest.mark.parametrize("name", ASKED_FIELDS)
def test_no_field_is_asked_the_same_way_twice(name: str) -> None:
    """Catches any field whose first ask, second ask and stop line are not three
    different texts (the loop the person saw), and a field with its own special
    case: every field must walk the same three steps."""
    first = next_ask({name}, asks={}, details_asked=False)
    second = next_ask({name}, asks={name: 1}, details_asked=True)
    stop = next_ask({name}, asks={name: 2}, details_asked=True)

    assert first is not None
    assert second is not None
    assert stop is not None
    assert [first.step, second.step, stop.step] == [AskStep.FIRST, AskStep.SECOND, AskStep.STOP]
    assert len({first.reply, second.reply, stop.reply}) == 3
    assert second.reply == TABLE[name][1]
    assert stop.reply == f"I can't file this without {TABLE[name][2]}." + STOP_TAIL


def test_two_fields_missing_at_the_second_ask_name_both_in_order() -> None:
    """Catches a second ask that names only one of two missing fields, or joins
    them with something other than one space, or orders them by the stored
    counts: person and department each asked once gives the person sentence, a
    space, then the department sentence."""
    ask = next_ask(
        {"department", "target_user"},
        asks={"department": 1, "target_user": 1},
        details_asked=True,
    )

    assert ask is not None
    assert ask.step is AskStep.SECOND
    assert ask.reply == f"{PERSON_REQUIRED} {DEPARTMENT_REQUIRED}"
    assert ask.asks == {"target_user": 2, "department": 2}


def test_a_field_given_since_drops_out() -> None:
    """Catches a second ask that still names a field the person just gave, and one
    that keeps its count: the department was given after one ask of both, so only
    the person sentence is shown and only the person is counted."""
    ask = next_ask({"target_user"}, asks={"department": 1, "target_user": 1}, details_asked=True)

    assert ask is not None
    assert ask.reply == PERSON_REQUIRED
    assert ask.asks == {"target_user": 2}


def test_a_field_not_asked_yet_joins_the_second_ask_of_another() -> None:
    """Catches a ladder that asks a new field with the first-ask words while
    another is on its second ask (two different styles in one reply): the
    department was asked once, the person never, both are missing, so both get
    their "is required" sentence and the person is counted as asked once."""
    ask = next_ask({"target_user", "department"}, asks={"department": 1}, details_asked=True)

    assert ask is not None
    assert ask.step is AskStep.SECOND
    assert ask.reply == f"{PERSON_REQUIRED} {DEPARTMENT_REQUIRED}"
    assert ask.asks == {"target_user": 1, "department": 2}


def test_the_stop_names_the_first_field_asked_twice_in_list_order() -> None:
    """Catches a stop that names the wrong field when two are at the limit: the
    department comes before the reason in the list, so the department's stop line
    is the one said, even though the reason is also missing."""
    ask = next_ask(
        {"justification", "department"},
        asks={"justification": 2, "department": 2},
        details_asked=True,
    )

    assert ask is not None
    assert ask.step is AskStep.STOP
    assert ask.reply == "I can't file this without a department." + STOP_TAIL


def test_a_field_at_the_limit_stops_even_when_another_is_asked_once() -> None:
    """Catches a ladder that shows the second ask because some field is only at
    one: the reason is at two, so the turn stops, and the stop line is the
    reason's alone."""
    ask = next_ask(
        {"justification", "target_user"},
        asks={"justification": 2, "target_user": 1},
        details_asked=True,
    )

    assert ask is not None
    assert ask.step is AskStep.STOP
    assert ask.reply == REASON_STOP
    assert ask.asks == {}


def test_an_older_stored_row_counts_every_missing_field_as_asked_once() -> None:
    """Catches an older details row (no counts stored, `asks=None`) that is asked
    with the first-ask words again, or one that stops: it must be the second ask.
    Not the same as `{}`, which means nothing was asked."""
    older = next_ask({"justification"}, asks=None, details_asked=True)
    none_asked = next_ask({"justification"}, asks={}, details_asked=True)

    assert older is not None
    assert older.step is AskStep.SECOND
    assert older.reply == REASON_REQUIRED
    assert older.asks == {"justification": 2}
    assert none_asked is not None
    assert none_asked.step is AskStep.FIRST


def test_the_length_is_asked_once_and_never_required() -> None:
    """Catches a length that gets a second ask or a stop, or that is asked after
    the details question already named it: with nothing else missing the length
    alone is a first ask the first time, and no ask once details were asked."""
    first = next_ask(set(), asks={}, details_asked=False, length_missing=True)
    again = next_ask(set(), asks={}, details_asked=True, length_missing=True)

    assert first is not None
    assert first.step is AskStep.FIRST
    assert first.reply == "Before I check this, tell me how long you need it for."
    assert first.asks == {}
    assert again is None


def test_the_second_ask_does_not_name_the_length() -> None:
    """Catches a second ask that adds the length: it is never required, so with the
    reason asked once and the length also missing, the reply is the reason
    sentence alone."""
    ask = next_ask(
        {"justification"}, asks={"justification": 1}, details_asked=True, length_missing=True
    )

    assert ask is not None
    assert ask.reply == REASON_REQUIRED


def test_nothing_missing_asks_nothing() -> None:
    """Catches a ladder that asks when everything is given: no field missing and
    the length given must give no ask at all."""
    assert next_ask(set(), asks={}, details_asked=False) is None
    assert next_ask(set(), asks={"justification": 1}, details_asked=True) is None


# --- the stored counts ----------------------------------------------------------


def test_a_stored_count_of_two_is_read() -> None:
    """Catches a reader that caps counts at one (the stop would never come): two is
    the limit itself and must read back as two."""
    assert parse_asks({"justification": 2}) == {"justification": 2}
    assert parse_asks({"system": 1, "department": 2}) == {"system": 1, "department": 2}
    assert parse_asks({}) == {}


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param({"justification": 3}, id="count-over-the-limit"),
        pytest.param({"justification": 0}, id="count-zero"),
        pytest.param({"justification": -1}, id="count-negative"),
        pytest.param({"justification": "1"}, id="text-count"),
        pytest.param({"justification": 1.5}, id="float-count"),
        pytest.param({"justification": None}, id="empty-count"),
        pytest.param({"fake_field": 1}, id="unknown-name"),
        pytest.param({"expiry_days": 1}, id="the-length-is-never-counted"),
        pytest.param({"justification\n": 1}, id="name-with-trailing-newline"),
        pytest.param({"justification\r\n": 1}, id="name-with-trailing-crlf"),
        pytest.param({"justification ": 1}, id="name-with-trailing-space"),
        pytest.param({"justification": 1, "system": 3}, id="one-bad-spoils-the-whole-value"),
        pytest.param({"justification": 1, "fake_field": 1}, id="one-unknown-spoils-the-whole"),
        pytest.param(None, id="none"),
        pytest.param([["justification", 1]], id="list"),
        pytest.param("justification", id="text"),
        pytest.param(2, id="number"),
    ],
)
def test_a_bad_stored_count_reads_as_no_asks(raw: object) -> None:
    """Catches a reader that trusts the stored value: a count of 3, of 0, a text
    count, an unknown or oddly spelt name, or anything that is not a dict reads as
    no asks, and one bad entry makes the whole value read as none (never a part)."""
    assert parse_asks(raw) == {}


# --- non-answers ----------------------------------------------------------------


def _extraction(intent: Intent = Intent.REQUEST_ACCESS, **entities: object) -> IntentExtraction:
    values: dict[str, object] = {
        "system": "wiki",
        "role": None,
        "target_user": None,
        "department": None,
        "expiry_days": 10,
        "justification": None,
    }
    values.update(entities)
    return IntentExtraction(
        intent=intent,
        entities=IntentEntities.model_validate(values),
        confidence=0.95,
        clarifying_question=None,
        second_request=None,
    )


@pytest.mark.parametrize(
    "text",
    [
        "no reason",
        "I don't know",
        "IDK",
        "None.",
        " n/a ",
        "dont know",
        "dunno",
        "not sure",
        "no idea",
        "nothing",
        "no",
        "No.",
        "I don\u2019t know",
        "  No Reason...  ",
    ],
)
def test_non_answers_count_as_missing(text: str) -> None:
    """Catches a gate that counts "no reason" or "I don't know" as a given reason
    (it would be filed as the reason): each leaves `justification` in
    `missing_required_entities`, whatever the case, spaces, end dots or curly
    apostrophe."""
    extraction = _extraction(justification=text)

    assert "justification" in gates.missing_required_entities(extraction)
    assert is_non_answer(text) is True


def test_a_non_answer_counts_as_missing_at_extraction_too() -> None:
    """Catches a non-answer test added to only one of the two missing-entity
    checks: a system named "none" is missing at the intent step, in the same place
    blank text is."""
    extraction = _extraction(system="none", justification="Obviously fake reason.")

    assert gates.missing_at_extraction(extraction) == {"system"}


def test_a_non_answer_for_the_department_counts_as_missing() -> None:
    """Catches a non-answer rule that only knows the reason: it is one shared rule,
    so an onboarding with the department "I don't know" is missing it, and the
    person given is not."""
    extraction = _extraction(
        Intent.ONBOARD, target_user="fake.person@example.test", department="I don't know"
    )

    assert gates.missing_required_entities(extraction) == {"department"}


@pytest.mark.parametrize(
    "text",
    [
        "no access to the docs yet",
        "none of the above",
        "I don't know the exact date, it is for payroll",
        "no reason given by my manager",
        "Obviously fake reason.",
    ],
)
def test_a_longer_answer_with_no_in_it_is_given(text: str) -> None:
    """Catches a non-answer test that looks for the words anywhere in the text
    (a real reason that holds "no" or "none" would be thrown away): only the whole
    text counts, so a longer answer is a given reason."""
    extraction = _extraction(justification=text)

    assert "justification" not in gates.missing_required_entities(extraction)
    assert is_non_answer(text) is False


# --- the question kind carries the counts ---------------------------------------


def test_the_stored_kind_carries_the_counts_only_when_given() -> None:
    """Catches a stored kind that drops the counts (the next message could not
    stop), and one that adds an empty `asks` key to kinds that carry none (it
    would be read as a details row that has counts)."""
    with_asks = stored_kind(QuestionKind.DETAILS, now=NOW, asks={"justification": 1})
    without = stored_kind(QuestionKind.DETAILS, now=NOW)

    assert with_asks == {"kind": "details", "at": NOW.isoformat(), "asks": {"justification": 1}}
    assert without == {"kind": "details", "at": NOW.isoformat()}


def test_the_counts_read_back_from_a_stored_kind() -> None:
    """Catches a reader that drops the counts or reads them from the wrong key."""
    raw = stored_kind(QuestionKind.DETAILS, now=NOW, asks={"justification": 2})

    assert parse_asked_asks(raw, now=NOW) == {"justification": 2}


def test_a_details_row_with_no_counts_reads_as_none_not_empty() -> None:
    """Catches a reader that turns an older details row into "nothing asked" (the
    reason would then be asked with the first-ask words a second time, word for
    word): it must read as `None`, which the ladder counts as asked once."""
    raw = stored_kind(QuestionKind.DETAILS, now=NOW)

    assert parse_asked_asks(raw, now=NOW) is None


@pytest.mark.parametrize(
    "kind",
    [QuestionKind.SUGGEST, QuestionKind.NAME, QuestionKind.OTHER],
)
def test_another_kind_with_no_counts_reads_as_empty(kind: QuestionKind) -> None:
    """Catches a reader that gives `None` (asked once) to every older row: only a
    details row can have asked a required field, so any other kind with no counts
    reads as no asks."""
    assert parse_asked_asks(stored_kind(kind, now=NOW), now=NOW) == {}


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(None, id="missing"),
        pytest.param("details", id="text"),
        pytest.param(
            {"kind": "details", "at": "bogus", "asks": {"justification": 1}}, id="bad-time"
        ),
        pytest.param(
            {"kind": "bogus", "at": NOW.isoformat(), "asks": {"system": 1}}, id="bad-kind"
        ),
        pytest.param(
            {"kind": "details", "at": NOW.isoformat(), "asks": {"justification": 3}},
            id="bad-count",
        ),
        pytest.param(
            {"kind": "details", "at": (NOW - CLARIFY_CAP_LIFE).isoformat(), "asks": {"system": 1}},
            id="exactly-24-hours-old",
        ),
        pytest.param(
            {"kind": "details", "at": (NOW - CLARIFY_CAP_LIFE).isoformat()},
            id="too-old-and-no-counts",
        ),
    ],
)
def test_a_bad_or_old_stored_kind_reads_as_no_asks(raw: object) -> None:
    """Catches a reader that trusts counts from a row that is unreadable, unknown,
    badly counted or 24 hours old (the person has moved on), and one that returns
    `None` for an old details row so a stale chat is asked as if once before."""
    assert parse_asked_asks(raw, now=NOW) == {}


def test_a_row_just_under_24_hours_old_keeps_its_counts() -> None:
    """Catches a reader that expires counts early: one second inside the limit the
    counts are still read."""
    at = NOW - CLARIFY_CAP_LIFE + timedelta(seconds=1)
    raw = {"kind": "details", "at": at.isoformat(), "asks": {"justification": 1}}

    assert parse_asked_asks(raw, now=NOW) == {"justification": 1}


# --- the question words that moved ----------------------------------------------


def test_the_details_question_reads_the_list() -> None:
    """Catches a details question still built from its own word table: it keeps its
    shape (all missing fields and the length, in list order) and now also names the
    person and the department."""
    assert details_question({"justification"}, length_missing=True) == (
        "Before I check this, tell me how long you need it for and the business reason."
    )
    assert details_question({"system"}, length_missing=False) == (
        "Before I check this, tell me which system you need."
    )
    assert details_question({"target_user", "department"}, length_missing=False) == (
        "Before I check this, tell me who the person is and which department."
    )


def test_the_old_word_table_and_reason_question_are_gone() -> None:
    """Catches a second source of wording left behind: the reason question and the
    word table are replaced by the list, and a name that stays can be called by
    mistake (the same reason asked in two ways)."""
    assert not hasattr(clarify_cap, "DETAIL_WORDS")
    assert not hasattr(clarify_cap, "reason_question")
