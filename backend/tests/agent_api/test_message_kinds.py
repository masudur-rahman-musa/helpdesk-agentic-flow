"""Whole conversations for an earlier change: the message is understood before the intent.

Each test runs real turns through the real graph on the fakes of
`test_draft_turns.py` (`ChatDb`, `Tools`, `play`, `wiki_db`, `extraction_json`,
`gateway_of`). Cost: nothing real. No `db` test, no network, no sleep.

Surface the implementer must provide:

* `understand.fix_spelling` / `understand.read_pick` run in `extract_intent` and
  `load_session`; `confirmation.NOTHING_WAITING_REPLY`; `QuestionKind.DECLINED`;
  `TurnState.asked_options`.
* `helpdesk.shared.crud.users.list_user_display_names` (a `select(User)` with no
  where clause) and `directory.fetch_live_role_ids_for_user` (a `select(Grant)`)
  are read through the session, so `KindsDb` below answers both.
"""

import json
import uuid
from collections.abc import Iterator
from datetime import date
from typing import Any

import httpx
import pytest

from helpdesk.agent_api.agent.clarify_cap import QuestionKind, parse_asked_options
from helpdesk.agent_api.agent.confirmation import (
    NOTHING_FILED_REPLY,
    NOTHING_WAITING_REPLY,
    Answer,
    read_answer,
)
from helpdesk.agent_api.agent.prompts import load_prompt
from helpdesk.agent_api.agent.state import TurnOutcome, TurnState
from helpdesk.agent_api.agent.tool_args import SEARCH_POLICY, SUGGEST_ROLES
from helpdesk.shared.crud.sessions import ASKED_KIND_KEY, REQUEST_DRAFT_KEY
from helpdesk.shared.models import Grant, Role, System, User
from helpdesk.shared.models.conversation import MessageRole
from tests.agent_api.test_agent_compose import StagedGateway, marked
from tests.agent_api.test_agent_confirm import _classify_payload
from tests.agent_api.test_draft_turns import (
    CLASSIFY,
    CREATE,
    EDITOR_ID,
    EMPTY_PLAN,
    FILED_REPLY,
    NOW,
    TENANT_ID,
    USER_ID,
    ChatDb,
    Tools,
    _Rows,
    ask_for_details,
    extraction_json,
    filing_tools,
    gateway_of,
    hold_plan,
    plan_json,
    play,
    reach_the_summary,
    search_page,
    wiki_db,
)

CRM_ID = uuid.UUID("66666666-6666-5666-8666-666666666601")
CRM_EDITOR_ID = uuid.UUID("55555555-5555-5555-8555-555555555601")
CRM_VIEWER_ID = uuid.UUID("55555555-5555-5555-8555-555555555602")
WAREHOUSE_ID = uuid.UUID("66666666-6666-5666-8666-666666666602")
WAREHOUSE_ENGINEER_ID = uuid.UUID("55555555-5555-5555-8555-555555555603")
MAIL_ID = uuid.UUID("66666666-6666-5666-8666-666666666603")
MAIL_USER_ID = uuid.UUID("55555555-5555-5555-8555-555555555604")
VPN_ID = uuid.UUID("66666666-6666-5666-8666-666666666604")
VPN_USER_ID = uuid.UUID("55555555-5555-5555-8555-555555555605")

HELD_VPN_REPLY = "You already hold VPN user."


# --- the fakes ------------------------------------------------------------------


class _IterRows(_Rows):
    """`_Rows` that also iterates, since the grant and user reads loop over `scalars()`."""

    def __iter__(self) -> Iterator[Any]:
        return iter(self.all())


class KindsDb(ChatDb):
    """`ChatDb` that also answers a read of grants and of the whole tenant's users.

    A read of one user by id still finds no row (`find_user_by_id` has a where
    clause), so every other test's principals stay empty. Only a `select(User)`
    with no where clause, which is how the tenant's display names are read,
    returns the people planted here.
    """

    def __init__(self) -> None:
        super().__init__()
        self.grants: list[Grant] = []
        self.users: list[User] = []

    async def execute(self, statement: Any) -> _Rows:
        entity = statement.column_descriptions[0]["entity"]
        if entity is Grant:
            return _IterRows(self.grants)
        if entity is User and statement.whereclause is None:
            return _IterRows(self.users)
        return await super().execute(statement)

    def hold(self, role_id: uuid.UUID) -> None:
        """The speaker holds this role right now."""
        self.grants.append(
            Grant(
                id=uuid.uuid4(),
                tenant_id=TENANT_ID,
                user_id=USER_ID,
                role_id=role_id,
                granted_at=NOW,
                expires_at=None,
                revoked_at=None,
                status="active",
            )
        )

    def add_person(self, display_name: str) -> None:
        self.users.append(
            User(
                id=uuid.uuid4(),
                tenant_id=TENANT_ID,
                email=f"{display_name.split()[0].lower()}@example.invalid",
                display_name=display_name,
                password_hash="fake",
                principals=["employee"],
                status="active",
                start_date=date(2026, 1, 5),
            )
        )


def kinds_db() -> KindsDb:
    """The wiki directory plus CRM, Data Warehouse, Mail and Files and VPN."""
    base = wiki_db()
    db = KindsDb()
    db.systems.extend(base.systems)
    db.roles.extend(base.roles)
    extra = (
        (CRM_ID, "crm", "CRM", ((CRM_EDITOR_ID, "editor"), (CRM_VIEWER_ID, "viewer"))),
        (WAREHOUSE_ID, "data-warehouse", "Data Warehouse", ((WAREHOUSE_ENGINEER_ID, "engineer"),)),
        (MAIL_ID, "mail-and-files", "Mail and Files", ((MAIL_USER_ID, "user"),)),
        (VPN_ID, "vpn", "VPN", ((VPN_USER_ID, "user"),)),
    )
    for system_id, slug, name, roles in extra:
        db.systems.append(
            System(
                id=system_id,
                tenant_id=TENANT_ID,
                slug=slug,
                name=name,
                sensitivity="low",
                owner_user_id=None,
            )
        )
        for role_id, role_slug in roles:
            db.roles.append(
                Role(
                    id=role_id,
                    tenant_id=TENANT_ID,
                    system_id=system_id,
                    slug=role_slug,
                    name=role_slug.title(),
                    privilege="read",
                    default_expiry_days=90,
                )
            )
    return db


class BySchemaGateway(StagedGateway):
    """A model that answers by the schema it is asked for, not by call order.

    `IntentExtraction` gets `extract`; a function of the messages lets a test play
    a model that understands only what the code put in front of it. The planner
    gets an empty plan and the writer gets `reply`. Every request is recorded.
    """

    def __init__(self, extract: Any, reply: str = "Fine.") -> None:
        super().__init__(answers=[])
        self._extract = extract
        self._reply = reply

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = json.loads(request.content)
        name = body["response_format"]["json_schema"]["name"]
        messages = [message["content"] for message in body["messages"]]
        if name == "IntentExtraction":
            text = self._extract(messages)
        elif name == "ToolPlanAnswer":
            text = EMPTY_PLAN
        else:
            text = marked(self._reply, asks=False)
        usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        return httpx.Response(
            200, json={"choices": [{"message": {"content": text}}], "usage": usage}
        )

    def contents(self, index: int) -> list[str]:
        """The message texts of one recorded request, in order."""
        body = json.loads(self.requests[index].content)
        return [message["content"] for message in body["messages"]]


SUGGESTED = [
    {
        "system_slug": "internal-wiki",
        "system_name": "Internal Wiki",
        "role_slug": "editor",
        "role_name": "wiki.editor",
        "score": 0.9,
    },
    {
        "system_slug": "mail-and-files",
        "system_name": "Mail and Files",
        "role_slug": "user",
        "role_name": "mail.user",
        "score": 0.8,
    },
]


def suggesting_tools() -> Tools:
    return Tools({SUGGEST_ROLES: {"roles": SUGGESTED}})


def docs_turn(db: KindsDb, tools: Tools) -> TurnState:
    """Turn one of a pick: "i need docs access" names no role, so two are offered."""
    return play(
        db,
        "i need docs access",
        gateway_of(extraction_json("request_access", system="docs")),
        tools,
    )


def option_model(messages: list[str]) -> str:
    """A model that understands only a message that IS an option's plain name."""
    if "Internal Wiki editor" in messages:
        return extraction_json("request_access", system="internal-wiki", role="editor")
    if "Mail and Files user" in messages:
        return extraction_json("request_access", system="mail-and-files", role="user")
    return extraction_json("request_access")


# --- spelling ---------------------------------------------------------------------


def test_typo_request_finds_the_role() -> None:
    """Catches a chat that hands "dta warhous eng" to the model as typed: the
    model here answers a blank extraction (it cannot place the typos), so only
    the code's spelling fix (the fixed message the model reads, and the system
    and role it fills in) can reach Data Warehouse engineer. The reply must be
    the details question for that role, never "I could not find anything by that
    name"."""
    db = kinds_db()
    gateway = gateway_of(extraction_json("request_access"))

    state = play(db, "give me access to dta warhous eng", gateway, Tools())

    sent = json.loads(gateway.requests[0].content)["messages"]
    assert sent[-1]["content"] == "give me access to Data Warehouse engineer"
    assert state.outcome is TurnOutcome.CLARIFY
    assert state.reply is not None
    assert state.reply.startswith("That's Data Warehouse engineer.")
    assert "I could not find anything by that name" not in state.reply


@pytest.mark.parametrize("people", [["Jon Smith", "Dana Fakename"], []])
def test_people_names_come_from_the_tenant_directory(people: list[str]) -> None:
    """Catches a spelling fix that ignores the tenant's people, and one that
    leaks them: with Jon Smith in the directory "jon smyth" reaches the model as
    "Jon Smith"; with nobody listed it reaches it as typed. Dana Fakename, whom
    nobody typed, is in no message of any request."""
    db = kinds_db()
    for person in people:
        db.add_person(person)
    gateway = BySchemaGateway(lambda _messages: extraction_json("other"))

    play(db, "what can jon smyth see", gateway, Tools())

    sent = gateway.contents(0)
    assert sent[-1] == ("what can Jon Smith see" if people else "what can jon smyth see")
    assert not any(
        "Dana Fakename" in text
        for index in range(len(gateway.requests))
        for text in gateway.contents(index)
    )


# --- yes, no and pick -------------------------------------------------------------


@pytest.mark.parametrize("word", ["y", "yep", "yup", "ya", "yea", "alright"])
def test_y_files_at_a_confirm(word: str) -> None:
    """Catches a yes list that misses the short and casual yeses ("y", "yup",
    "ya", "yea", "alright"): at the file summary each runs `create_access_request`
    exactly once, never twice and never zero times."""
    db = wiki_db()
    tools = filing_tools()
    ask_for_details(db, tools)
    reach_the_summary(db, tools)
    assert CREATE not in tools.names, "premise: the summary filed nothing"

    final = play(db, word, gateway_of(EMPTY_PLAN, FILED_REPLY), tools)

    assert tools.names.count(CREATE) == 1
    assert final.outcome is not TurnOutcome.CLARIFY


def test_the_new_yes_words_read_as_yes() -> None:
    """Catches a yes list change that breaks a neighbour: the five new words are
    a yes, "no" is still a no, and a long message that starts with "y" is not
    one."""
    for word in ("y", "yup", "ya", "yea", "alright"):
        assert read_answer(word) is Answer.YES, word
    assert read_answer("no") is Answer.NO
    assert read_answer("y but make it for a whole year please") is Answer.OTHER


def test_nothing_waiting_line_is_exact() -> None:
    """Pins the fixed line a lone yes gets when nothing is waiting, word for word."""
    assert NOTHING_WAITING_REPLY == (
        "Nothing is waiting for a yes. Tell me what you need, and I'll check it with you first."
    )


def test_no_then_do_it_files_nothing() -> None:
    """Catches a "no" that leaves the old request alive: after the summary, "no"
    (filed nothing, stored as the declined kind) and then "ok do it now" must run
    no tool and no model call, end the turn on the nothing-waiting line, and leave
    no draft. A no that stored a plain question kind would let the yes fall to the
    model, which has the old request in its history."""
    db = wiki_db()
    tools = filing_tools()
    ask_for_details(db, tools)
    reach_the_summary(db, tools)

    declined = play(db, "no", gateway_of(), tools)
    assert declined.reply == NOTHING_FILED_REPLY
    assert db.metadata[ASKED_KIND_KEY]["kind"] == QuestionKind.DECLINED.value
    calls_before = len(tools.calls)

    final = play(db, "ok do it now", gateway_of(), tools)

    assert final.outcome is TurnOutcome.STOP
    assert final.reply == NOTHING_WAITING_REPLY
    assert db.stored(MessageRole.ASSISTANT)[-1] == NOTHING_WAITING_REPLY
    assert len(tools.calls) == calls_before
    assert tools.names.count(CREATE) == 0
    assert REQUEST_DRAFT_KEY not in db.metadata


def test_a_yes_after_another_question_still_goes_to_the_model() -> None:
    """Unhappy path. Catches a nothing-waiting reply given to every yes that has no
    held request: here the last question was the details question, not a no, so
    "yes" is an ordinary message and the model reads it (exactly one request)."""
    db = wiki_db()
    tools = filing_tools()
    ask_for_details(db, tools)
    gateway = gateway_of(extraction_json("request_access"))

    state = play(db, "yes", gateway, tools)

    assert len(gateway.requests) == 1
    assert state.reply != NOTHING_WAITING_REPLY


def test_a_new_request_drops_the_draft() -> None:
    """Unhappy path. Catches a "no" that keeps the old draft: the turn after it
    loads no draft, and "I need CRM viewer for 7 days to check accounts" ends on
    the summary for CRM viewer, with nothing of the Internal Wiki in it."""
    db = kinds_db()
    tools = filing_tools()
    ask_for_details(db, tools)
    reach_the_summary(db, tools)
    play(db, "no", gateway_of(), tools)
    shared = {"target_user_id": str(USER_ID), "role_id": str(CRM_VIEWER_ID)}
    reason = "to check accounts"
    plan = plan_json(
        (CLASSIFY, {**shared, "requested_expiry_days": 7}),
        (
            CREATE,
            {**shared, "justification": reason, "requested_expiry_days": 7, "citations": []},
        ),
    )

    state = play(
        db,
        "I need CRM viewer for 7 days to check accounts",
        gateway_of(
            extraction_json(
                "request_access",
                system="crm",
                role="viewer",
                expiry_days=7,
                justification=reason,
            ),
            plan,
        ),
        tools,
    )

    assert state.request_draft is None, "the draft as loaded: the no cleared it"
    assert state.outcome is TurnOutcome.CONFIRM
    assert state.reply is not None
    assert state.reply.startswith(f'I\'ll file this: CRM viewer, 7 days, reason "{reason}".')
    assert "Internal Wiki" not in state.reply


def test_the_stored_question_keeps_the_options() -> None:
    """Catches a suggestion that forgets what it offered: the chat row holds the
    two plain names, in the order shown, so a pick can be read next turn."""
    db = kinds_db()
    docs_turn(db, suggesting_tools())

    assert parse_asked_options(db.metadata[ASKED_KIND_KEY], now=NOW) == (
        "Internal Wiki editor",
        "Mail and Files user",
    )


@pytest.mark.parametrize(
    ("pick", "role_name"),
    [
        pytest.param("1", "Internal Wiki editor", id="number"),
        pytest.param("the first one", "Internal Wiki editor", id="the-first-one"),
        pytest.param("mail", "Mail and Files user", id="word"),
        pytest.param("last", "Mail and Files user", id="last"),
    ],
)
def test_pick_by_number_and_word(pick: str, role_name: str) -> None:
    """Catches a pick left for the model to guess: the model here understands only
    a message that IS an option's plain name, so the answer resolves the role only
    when the code rewrote "1", "the first one", "mail" or "last" to that name
    before the model read it. The turn ends asking the details of that role."""
    db = kinds_db()
    tools = suggesting_tools()
    docs_turn(db, tools)
    gateway = BySchemaGateway(option_model)

    state = play(db, pick, gateway, tools)

    assert role_name in gateway.contents(0)
    assert state.user_message == role_name
    assert state.outcome is TurnOutcome.CLARIFY
    assert state.reply is not None
    assert state.reply.startswith(f"That's {role_name}.")


def test_a_word_in_two_options_is_not_a_pick() -> None:
    """Unhappy path. Catches a pick that takes the first option when the typed
    word fits two: both offered roles are on the wiki, so "wiki" fits both and
    must reach the model as typed, not as the first option's name."""
    db = kinds_db()
    tools = Tools(
        {
            SUGGEST_ROLES: {
                "roles": [
                    {**SUGGESTED[0]},
                    {**SUGGESTED[0], "role_slug": "viewer", "role_name": "wiki.viewer"},
                ]
            }
        }
    )
    docs_turn(db, tools)
    gateway = BySchemaGateway(option_model)

    state = play(db, "wiki", gateway, tools)

    assert state.user_message == "wiki"


def test_the_model_reads_the_last_question_as_data() -> None:
    """Catches a model that is not told what the assistant just asked: on the turn
    after a suggestion, one `user` message holds the kind ("suggest") and the
    options, apart from the history lines. A first turn has no such message."""
    db = kinds_db()
    tools = suggesting_tools()
    first = BySchemaGateway(lambda _messages: extraction_json("request_access", system="docs"))
    play(db, "i need docs access", first, tools)
    assert not any(
        "suggest" in text and "Mail and Files user" in text for text in first.contents(0)
    )
    second = BySchemaGateway(lambda _messages: extraction_json("follow_up"))

    play(db, "what do you mean", second, tools)

    data = [
        text
        for text in second.contents(0)
        if "suggest" in text and "Internal Wiki editor" in text and "Mail and Files user" in text
    ]
    assert len(data) == 1
    assert not data[0].startswith(("user:", "assistant:"))


# --- held roles -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("held", "stops"),
    [
        pytest.param(VPN_USER_ID, True, id="holds-vpn-user"),
        pytest.param(MAIL_USER_ID, False, id="holds-another-role"),
        pytest.param(None, False, id="holds-nothing"),
    ],
)
def test_held_role_stops_with_held_line(held: uuid.UUID | None, stops: bool) -> None:
    """Catches a request for a role the person already holds that goes on to ask
    for days and a reason (or to file): a holder of VPN user gets "You already
    hold VPN user." with no tool call. Someone holding a different role, or
    nothing, gets the details question instead, so only the held role stops it."""
    db = kinds_db()
    if held is not None:
        db.hold(held)
    tools = Tools()

    state = play(
        db,
        "give me VPN user access",
        gateway_of(extraction_json("request_access", system="vpn", role="user")),
        tools,
    )

    assert tools.calls == []
    if stops:
        assert state.outcome is TurnOutcome.STOP
        assert state.reply == HELD_VPN_REPLY
    else:
        assert state.outcome is TurnOutcome.CLARIFY
        assert state.reply != HELD_VPN_REPLY


def test_suggestions_leave_out_held_roles() -> None:
    """Catches suggestions that offer a role the person has: a holder of Mail and
    Files user who asks for "docs access" is offered the wiki editor only, in
    the reply and in the stored options. The tool's own list holds both roles, so
    only the agent's filter can drop one."""
    db = kinds_db()
    db.hold(MAIL_USER_ID)

    state = docs_turn(db, suggesting_tools())

    assert state.outcome is TurnOutcome.DISAMBIGUATE
    assert state.options == ("Internal Wiki editor",)
    assert state.reply is not None
    assert "Mail and Files" not in state.reply
    assert "Internal Wiki" in state.reply


# --- a repeat, who approves ------------------------------------------------------


def test_repeat_names_its_number() -> None:
    """Catches a repeat of a request still waiting that is filed again, or told
    without its number: after the first request is filed, the same one finds an
    open request, files nothing, and the reply names REQ-0001. The writer here is
    wrong twice running ("granted" with nothing granted), so the number can only
    come from the code-built last resort."""
    db = wiki_db()
    first = filing_tools()
    ask_for_details(db, first)
    reach_the_summary(db, first)
    play(db, "yes", gateway_of(EMPTY_PLAN, FILED_REPLY), first)
    assert first.names.count(CREATE) == 1, "premise: the first request was filed"
    open_request = {
        **_classify_payload(),
        "already_requested": True,
        "request_number": "REQ-0001",
        "status": "pending",
    }
    tools = Tools({SEARCH_POLICY: search_page(), CLASSIFY: open_request, CREATE: open_request})
    shared = {"target_user_id": str(USER_ID), "role_id": str(EDITOR_ID)}
    wrong = marked("Your access is granted.", asks=False)

    state = play(
        db,
        "I need wiki editor access for 14 days to read the runbooks",
        gateway_of(
            extraction_json(
                "request_access",
                system="internal-wiki",
                role="editor",
                expiry_days=14,
                justification="to read the runbooks",
            ),
            plan_json((CLASSIFY, {**shared, "requested_expiry_days": 14})),
            hold_plan(14, "to read the runbooks"),
            wrong,
            wrong,
        ),
        tools,
    )

    assert tools.names.count(CREATE) == 0
    assert state.reply is not None
    assert "You already asked for this (REQ-0001)" in state.reply


def test_who_approves_is_answered() -> None:
    """Catches an `ask_policy` turn that cannot classify: "who approves CRM
    editor?" must run `classify_access_request` (read only) and hand its result,
    the approver seats, to the writer. With no classify call the writer would
    have the policy text alone."""
    db = kinds_db()
    tools = Tools({SEARCH_POLICY: search_page(), CLASSIFY: _classify_payload()})
    classify = plan_json(
        (CLASSIFY, {"target_user_id": str(USER_ID), "role_id": str(CRM_EDITOR_ID)})
    )
    reply = "CRM editor needs approval from your manager and the system owner."

    state = play(
        db,
        "who approves CRM editor?",
        gateway_of(
            extraction_json("ask_policy", system="crm", role="editor"),
            classify,
            EMPTY_PLAN,
            reply,
        ),
        tools,
    )

    assert tools.names == [SEARCH_POLICY, CLASSIFY]
    assert CREATE not in tools.names
    assert state.reply == reply
    assert any(result.tool == CLASSIFY and result.ok for result in state.tool_results)


def test_the_planner_is_told_who_approves_uses_classify() -> None:
    """Catches a planning prompt that leaves the new tool to chance: the text must
    tie "who approves" to `classify_access_request` in the same passage."""
    text = load_prompt("tool_planning").text
    where = text.lower().index("who approves")
    assert "classify_access_request" in text[max(0, where - 300) : where + 300]


def test_a_miss_naming_a_held_role_says_it_is_held() -> None:
    """Catches "mail" after a docs suggestion that asks for the exact name: Mail
    and Files user is held, so it was never offered and "mail" picks nothing,
    but its words fit that one held role, so the reply is the held line with no
    tool call."""
    db = kinds_db()
    db.hold(MAIL_USER_ID)
    tools = suggesting_tools()

    state = play(
        db,
        "mail",
        gateway_of(extraction_json("request_access", system="mail")),
        tools,
    )

    assert state.outcome is TurnOutcome.STOP
    assert state.reply == "You already hold Mail and Files user."
    assert tools.calls == []
