"""The saved request draft: fill, replace, clear and read back, with no graph at all.

Surface the implementer must provide (names fixed by an earlier change):
`helpdesk.agent_api.agent.request_draft` with `DRAFT_LIFE`, `RequestDraft`,
`parse_request_draft`, `merge_draft` and `draft_dump`; `Intent.FOLLOW_UP` and
`IntentExtraction.second_request` in `state.py`.

Cost and isolation. Nothing backing: no Postgres, no gateway, no tools. Every
case calls one pure function on plain values, so the file runs in milliseconds
and shares no state.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from helpdesk.agent_api.agent import gates
from helpdesk.agent_api.agent.request_draft import (
    DRAFT_LIFE,
    RequestDraft,
    draft_dump,
    merge_draft,
    parse_request_draft,
)
from helpdesk.agent_api.agent.required_fields import next_ask
from helpdesk.agent_api.agent.state import Intent, IntentEntities, IntentExtraction

NOW = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
EARLIER = NOW - timedelta(minutes=5)

FIELDS = ("system", "role", "target_user", "department", "justification", "expiry_days")


def _draft(**given: Any) -> RequestDraft:
    """A saved draft of a wiki request, with whatever the case overrides."""
    fields: dict[str, Any] = {
        "intent": Intent.REQUEST_ACCESS,
        "saved_at": EARLIER,
        **given,
    }
    return RequestDraft(**fields)


def _message(
    intent: Intent = Intent.REQUEST_ACCESS, *, confidence: float = 0.95, **entities: Any
) -> IntentExtraction:
    """What the model made of ONE new message: only the fields it names."""
    return IntentExtraction(
        intent=intent,
        entities=IntentEntities(**{**dict.fromkeys(IntentEntities.model_fields), **entities}),
        confidence=confidence,
        clarifying_question=None,
        second_request=None,
    )


def _fields(source: RequestDraft | IntentEntities | None) -> dict[str, Any]:
    """The six request fields of a draft or an entities object, for whole-set checks."""
    if source is None:
        return dict.fromkeys(FIELDS)
    return {name: getattr(source, name) for name in FIELDS}


WIKI = _draft(
    system="internal-wiki",
    role="editor",
    expiry_days=30,
    justification="to read the runbooks",
)


# --- merging one message into the draft -----------------------------------------


def test_a_new_answer_fills_only_its_field() -> None:
    """Catches a merge that takes the model's entities as they are (the old
    behaviour): the message gives only 14 days, so the saved system and reason
    would be lost and the person asked for them again."""
    draft = _draft(system="internal-wiki", justification="to read the runbooks")

    merged, saved = merge_draft(draft, _message(expiry_days=14), now=NOW)

    expected = {
        **dict.fromkeys(FIELDS),
        "system": "internal-wiki",
        "justification": "to read the runbooks",
        "expiry_days": 14,
    }
    assert _fields(merged.entities) == expected
    assert _fields(saved) == expected
    assert merged.intent is Intent.REQUEST_ACCESS


def test_the_merged_draft_is_saved_with_the_time_of_this_turn() -> None:
    """Catches a saved draft that keeps the old `saved_at`: the 24 hours would
    run from the first message, so a chat still in use loses its draft."""
    _merged, saved = merge_draft(_draft(system="internal-wiki"), _message(expiry_days=14), now=NOW)

    assert saved is not None
    assert saved.saved_at == NOW
    assert saved.intent is Intent.REQUEST_ACCESS


def test_with_no_saved_draft_the_message_becomes_the_draft() -> None:
    """Catches a merge that returns nothing to save when there was no draft
    before: turn one would store nothing, and turn two would have nothing to
    fill from."""
    merged, saved = merge_draft(None, _message(system="internal-wiki", role="editor"), now=NOW)

    assert saved is not None
    assert _fields(saved) == {**dict.fromkeys(FIELDS), "system": "internal-wiki", "role": "editor"}
    assert saved.intent is Intent.REQUEST_ACCESS
    assert _fields(merged.entities) == _fields(saved)


def test_a_newer_answer_replaces_the_older() -> None:
    """Catches a merge that keeps the saved value when the message gives a new
    one ("make it 10 days" must win over the 30 days saved before)."""
    merged, saved = merge_draft(WIKI, _message(expiry_days=10), now=NOW)

    assert merged.entities.expiry_days == 10
    assert saved is not None
    assert saved.expiry_days == 10
    assert saved.justification == "to read the runbooks"


def test_a_new_system_drops_the_saved_role() -> None:
    """Catches a merge that keeps the "editor" role of the wiki when the person
    now names the ledger: the role would be looked up inside the wrong system.
    The message gives a system and no role, so only the new-system rule can
    empty the role."""
    merged, saved = merge_draft(WIKI, _message(system="ledger"), now=NOW)

    assert merged.entities.system == "ledger"
    assert merged.entities.role is None
    assert saved is not None
    assert saved.role is None
    assert saved.system == "ledger"
    assert saved.justification is None


@pytest.mark.parametrize("same", ["Internal-Wiki", "  internal-wiki  ", "INTERNAL-WIKI"])
def test_the_same_system_typed_another_way_keeps_the_saved_role(same: str) -> None:
    """Catches a system comparison that is case-sensitive or counts spaces:
    "Internal-Wiki" is not a new system, so dropping the role would make the
    person say it again for nothing."""
    merged, saved = merge_draft(WIKI, _message(system=same), now=NOW)

    assert merged.entities.role == "editor"
    assert saved is not None
    assert saved.role == "editor"


def test_a_new_system_with_its_own_role_keeps_that_role() -> None:
    """Catches a rule that empties the role after it merged the message in: when
    the same message names "ledger" and "approver", the role it gave stays."""
    merged, saved = merge_draft(WIKI, _message(system="ledger", role="approver"), now=NOW)

    assert merged.entities.role == "approver"
    assert saved is not None
    assert saved.role == "approver"


@pytest.mark.parametrize("non_answer", ["no reason", "I don't know", "n/a", "   "])
def test_a_non_answer_replaces_nothing(non_answer: str) -> None:
    """Catches a merge that lets "no reason" overwrite a reason saved earlier:
    the person who already said why would be asked for a reason again."""
    merged, saved = merge_draft(WIKI, _message(justification=non_answer), now=NOW)

    assert merged.entities.justification == "to read the runbooks"
    assert saved is not None
    assert saved.justification == "to read the runbooks"


# --- a change of subject ----------------------------------------------------------


@pytest.mark.parametrize("confidence", [0.7, 0.9, 1.0])
def test_a_change_of_subject_clears_the_draft(confidence: float) -> None:
    """Catches a draft that survives a clear change of subject: a password reset
    typed after a wiki request would carry the wiki system, role and reason into
    the reset. 0.7 is the inclusive edge, the same as the gate's threshold."""
    merged, saved = merge_draft(
        WIKI, _message(Intent.RESET_PASSWORD, confidence=confidence), now=NOW
    )

    assert merged.intent is Intent.RESET_PASSWORD
    assert _fields(merged.entities) == dict.fromkeys(FIELDS)
    assert _fields(saved) == dict.fromkeys(FIELDS)


def test_a_change_of_subject_keeps_what_the_new_message_gives() -> None:
    """Catches a clear that wipes the new message's own fields too: after
    "revoke my ledger access" the ledger must survive the clearing of the wiki."""
    merged, saved = merge_draft(
        WIKI, _message(Intent.REVOKE_ACCESS, confidence=0.9, system="ledger"), now=NOW
    )

    expected = {**dict.fromkeys(FIELDS), "system": "ledger"}
    assert _fields(merged.entities) == expected
    assert _fields(saved) == expected


@pytest.mark.parametrize("intent", [Intent.FOLLOW_UP, Intent.OTHER])
def test_follow_up_and_other_keep_the_draft(intent: Intent) -> None:
    """Catches a "why?" or a "thanks" that clears the draft: the clear intent is
    0.95, so only the explicit follow_up/other exception keeps every saved field,
    keeps the draft's own intent in what is saved, and still hands the turn the
    message's own label so routing sees it."""
    merged, saved = merge_draft(WIKI, _message(intent, confidence=0.95), now=NOW)

    assert merged.intent is intent
    assert _fields(merged.entities) == _fields(WIKI)
    assert saved is not None
    assert saved.intent is Intent.REQUEST_ACCESS
    assert _fields(saved) == _fields(WIKI)


@pytest.mark.parametrize("confidence", [0.69, 0.5, 0.1])
def test_an_unsure_other_intent_keeps_the_draft(confidence: float) -> None:
    """Catches a clear on any other label, sure or not: "reset_password" at
    0.69 is a guess, and one guess must not throw away a half-built request.
    0.69 is the edge just under the 0.7 gate."""
    _merged, saved = merge_draft(
        WIKI, _message(Intent.RESET_PASSWORD, confidence=confidence), now=NOW
    )

    assert saved is not None
    assert saved.intent is Intent.REQUEST_ACCESS
    assert _fields(saved) == _fields(WIKI)


# --- reading the stored value back ------------------------------------------------


def test_the_draft_lives_exactly_twenty_four_hours() -> None:
    """Catches a life of 12 or 48 hours: the plan says 24, like the open question."""
    assert timedelta(hours=24) == DRAFT_LIFE


def test_a_dumped_draft_reads_back_equal() -> None:
    """Catches a dump that cannot be stored as JSON (a raw datetime) or that
    loses a field on the way back: the chat row holds this value."""
    dumped = draft_dump(WIKI)

    assert isinstance(dumped["saved_at"], str)
    assert dumped["intent"] == "request_access"
    assert parse_request_draft(dumped, now=NOW) == WIKI


def test_a_draft_just_under_the_life_reads() -> None:
    """Catches a life check that is one step early: 23 hours 59 minutes old is
    still the person's draft."""
    old = draft_dump(_draft(system="internal-wiki", saved_at=NOW - timedelta(hours=23, minutes=59)))

    parsed = parse_request_draft(old, now=NOW)

    assert parsed is not None
    assert parsed.system == "internal-wiki"


def _stored(**changes: Any) -> dict[str, Any]:
    """A good stored draft, with the case's change applied."""
    return {**draft_dump(_draft(system="internal-wiki")), **changes}


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(None, id="nothing stored"),
        pytest.param("a string", id="a string"),
        pytest.param(["intent"], id="a list"),
        pytest.param({}, id="an empty object"),
        pytest.param(_stored(extra_field="x"), id="an unknown field"),
        pytest.param(_stored(intent="no_such_intent"), id="an unknown intent"),
        pytest.param(_stored(expiry_days="ten"), id="days that are not a number"),
        pytest.param(_stored(saved_at="not a time"), id="a time that is not a time"),
        pytest.param(
            _stored(saved_at=(NOW - timedelta(hours=24)).isoformat()), id="exactly 24 hours old"
        ),
        pytest.param(_stored(saved_at=(NOW - timedelta(days=3)).isoformat()), id="three days old"),
    ],
)
def test_a_bad_or_old_draft_reads_as_none(raw: object) -> None:
    """Catches a reader that raises on a bad value, trusts an unknown field, or
    keeps a draft at the 24-hour edge (`>=` is old): the person has moved on and
    must start clean."""
    assert parse_request_draft(raw, now=NOW) is None


# --- code picks the next ask, in list order --------------------------------------


@pytest.mark.parametrize(
    ("saved", "message", "question"),
    [
        pytest.param(
            _draft(role="editor"),
            _message(),
            "Before I check this, tell me which system you need, how long you need it for "
            "and the business reason.",
            id="system, then days, then reason, however they were saved",
        ),
        pytest.param(
            _draft(role="editor", expiry_days=14, justification="to read the runbooks"),
            _message(),
            "Before I check this, tell me which system you need.",
            id="saved days and reason are not asked again",
        ),
        pytest.param(
            _draft(intent=Intent.ONBOARD),
            _message(Intent.ONBOARD),
            "Before I check this, tell me who the person is and which department.",
            id="person before department",
        ),
    ],
)
def test_the_next_ask_follows_the_list_order(
    saved: RequestDraft, message: IntentExtraction, question: str
) -> None:
    """Catches a merge that loses a saved field (the question names it again) or
    a question that lists the missing fields in set order or in the order the
    model filled them: the one question follows `REQUIRED_FIELDS` list order."""
    merged, _saved = merge_draft(saved, message, now=NOW)

    asked = next_ask(
        gates.missing_required_entities(merged),
        asks={},
        details_asked=False,
        length_missing=gates.length_missing(merged),
    )

    assert asked is not None
    assert asked.reply == question


# --- a new system or a new role asks the reason again -----

REASONED = _draft(
    system="internal-wiki",
    role="editor",
    target_user="Dana",
    expiry_days=5,
    justification="to update docs",
)


def test_a_new_role_drops_the_saved_reason() -> None:
    """Catches the live failure of "actually make it CRM viewer instead" keeping
    "to update docs": the message names a new system and role and no reason, so
    the saved reason must go while the 5 days and the person stay."""
    merged, saved = merge_draft(REASONED, _message(system="crm", role="viewer"), now=NOW)

    assert merged.entities.role == "viewer"
    assert merged.entities.justification is None
    assert merged.entities.expiry_days == 5
    assert merged.entities.target_user == "Dana"
    assert saved is not None
    assert saved.justification is None
    assert saved.expiry_days == 5
    assert saved.target_user == "Dana"


def test_a_new_role_on_the_same_system_drops_the_saved_reason() -> None:
    """Catches a rule that fires only on a new system: the person keeps the wiki
    and changes "editor" to "viewer", which leaves the system unchanged, so only a
    check on the role itself can drop the reason."""
    merged, saved = merge_draft(REASONED, _message(role="viewer"), now=NOW)

    assert merged.entities.system == "internal-wiki"
    assert merged.entities.role == "viewer"
    assert merged.entities.justification is None
    assert saved is not None
    assert saved.justification is None
    assert saved.expiry_days == 5


def test_the_same_role_typed_again_keeps_the_saved_reason() -> None:
    """Catches a rule that drops the reason on any role the message names: saying
    "Editor" again for the saved "editor" is not a change, so the reason stays."""
    merged, saved = merge_draft(REASONED, _message(role="Editor"), now=NOW)

    assert merged.entities.justification == "to update docs"
    assert saved is not None
    assert saved.justification == "to update docs"


def test_a_new_reason_with_the_new_role_is_kept() -> None:
    """Catches a rule that empties the reason after merging the message in: when
    the same message names the CRM viewer role and a reason, that reason stays."""
    merged, saved = merge_draft(
        REASONED,
        _message(system="crm", role="viewer", justification="to check accounts"),
        now=NOW,
    )

    assert merged.entities.justification == "to check accounts"
    assert merged.entities.expiry_days == 5
    assert saved is not None
    assert saved.justification == "to check accounts"


def test_the_old_reason_repeated_with_a_new_role_is_dropped() -> None:
    """Catches the live run of 2026-10-21, where the model copied "to update
    docs" from the history into the CRM viewer message: a reason that only
    repeats the saved one for the old role is not a new reason, so it is asked
    again."""
    merged, saved = merge_draft(
        REASONED,
        _message(system="crm", role="viewer", justification="To update docs"),
        now=NOW,
    )

    assert merged.entities.justification is None
    assert saved is not None
    assert saved.justification is None
