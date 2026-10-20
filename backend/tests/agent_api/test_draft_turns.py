"""The saved draft across whole turns, with a fake chat row and no database.

Surface the implementer must provide (names fixed by an earlier change):
`helpdesk.shared.crud.sessions` with `REQUEST_DRAFT_KEY = "request_draft"`,
`get_request_draft` and `set_request_draft`; `TurnState.request_draft`;
`load_session` reads and clears the draft, `extract_intent` merges and saves
it, `stop_asking` clears it.

Cost and isolation. `ChatDb` is a plain object standing in for the tenant-bound
session: it holds the one chat row, its messages and the directory rows in
memory, so a whole turn runs in milliseconds. The gateway is an
`httpx.MockTransport` and the tools are a recording stub. Every test makes its
own `ChatDb`, so nothing is shared. Only `test_draft_round_trips_on_the_chat_row`
reaches Postgres (marker `db`), because only Postgres shows that the JSONB key
is really written and read back.

The fakes here (`ChatDb`, `Tools`, `play`) are imported by `test_follow_up.py`
and `test_second_request.py`.
"""

import asyncio
import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, cast

import httpx
import pytest
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from helpdesk.agent_api.agent.confirmation import FILE_LINE, NOTHING_FILED_REPLY
from helpdesk.agent_api.agent.graph import TurnDeps, run_turn
from helpdesk.agent_api.agent.request_draft import RequestDraft, draft_dump, parse_request_draft
from helpdesk.agent_api.agent.state import Intent, IntentEntities, TurnOutcome, TurnState
from helpdesk.agent_api.agent.tool_args import SEARCH_POLICY
from helpdesk.agent_api.services.mcp_client import ToolCaller
from helpdesk.agent_api.services.model_gateway.client import ModelClient
from helpdesk.shared.core.db import _TENANT_INFO_KEY, tenant_session
from helpdesk.shared.crud.sessions import (
    REQUEST_DRAFT_KEY,
    get_open_question,
    get_or_create_session,
    get_request_draft,
    set_open_question,
    set_request_draft,
)
from helpdesk.shared.models import Message, Role, Session, System
from helpdesk.shared.models.conversation import MessageRole
from tests.agent_api.test_agent_compose import StagedAnswer, StagedGateway
from tests.agent_api.test_agent_confirm import _classify_payload, _create_payload
from tests.support.seed import seed_tenant

NOW = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
GATEWAY_URL = "http://gateway.invalid:8080"
TENANT_SLUG = "draft-tenant"
# Obviously fake, tests only: this is not a credential.
API_KEYS: dict[str, str] = {TENANT_SLUG: f"fake-key-{TENANT_SLUG}"}

TENANT_ID = uuid.UUID("22222222-2222-5222-8222-222222222222")
USER_ID = uuid.UUID("33333333-3333-5333-8333-333333333333")
SESSION_ID = uuid.UUID("11111111-1111-5111-8111-111111111111")
SYSTEM_ID = uuid.UUID("66666666-6666-5666-8666-666666666666")
EDITOR_ID = uuid.UUID("55555555-5555-5555-8555-555555555555")
VIEWER_ID = uuid.UUID("55555555-5555-5555-8555-555555555556")

CLASSIFY = "classify_access_request"
CREATE = "create_access_request"

WIKI_REASON = "to read the runbooks"
FILED_REPLY = "Your fake wiki request was filed and is with the right people."
"""A composed reply that uses no decision word, so the check lets it through."""

EMPTY_PLAN = json.dumps({"calls": []})


# --- the fake chat row ------------------------------------------------------------


class _Rows:
    """The answer to one read: a list of rows, read the way the helpers read it."""

    def __init__(self, rows: Sequence[Any]) -> None:
        self._rows = list(rows)

    def scalars(self) -> "_Rows":
        return self

    def one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None

    def all(self) -> list[Any]:
        return list(self._rows)


class ChatDb:
    """An `AsyncSession` stand-in holding one chat, bound to a tenant the way
    `tenant_session` binds one.

    A node that went through the repository helpers works on it unchanged; one
    that reached past them for raw SQL does not. It answers a read by the model
    the statement selects: the chat row, the messages (newest first, 20 at
    most, as `list_recent_messages` asks), the systems and the roles. Anything
    else, such as a directory user, reads as no rows.
    """

    HISTORY_LIMIT = 20

    def __init__(self) -> None:
        self.info: dict[str, Any] = {_TENANT_INFO_KEY: TENANT_ID}
        self.chat: Session | None = None
        self.messages: list[Message] = []
        self.systems: list[System] = []
        self.roles: list[Role] = []

    def add(self, row: Any) -> None:
        if isinstance(row, Message):
            row.ordinal = len(self.messages) + 1
            self.messages.append(row)
        elif isinstance(row, Session):
            self.chat = row
        else:
            raise AssertionError(f"a node added a row this fake does not hold: {row!r}")

    async def flush(self) -> None:
        return None

    async def execute(self, statement: Any) -> _Rows:
        entity = statement.column_descriptions[0]["entity"]
        if entity is Session:
            return _Rows([] if self.chat is None else [self.chat])
        if entity is Message:
            newest_first = sorted(self.messages, key=lambda row: row.ordinal, reverse=True)
            return _Rows(newest_first[: self.HISTORY_LIMIT])
        if entity is System:
            return _Rows(self.systems)
        if entity is Role:
            return _Rows(self.roles)
        return _Rows([])

    @property
    def metadata(self) -> dict[str, Any]:
        """What the chat row holds beside its messages, as stored."""
        assert self.chat is not None, "no turn has made the chat row yet"
        return dict(self.chat.session_metadata or {})

    def plant(self, role: MessageRole, content: str) -> None:
        """Put an earlier message in the chat, as a turn before this one would."""
        self.add(
            Message(
                tenant_id=TENANT_ID,
                session_id=SESSION_ID,
                role=role.value,
                content=content,
                tool_calls=None,
                citations=None,
            )
        )

    def stored(self, role: MessageRole) -> list[str]:
        """The contents stored under one role, oldest first."""
        return [row.content for row in self.messages if row.role == role.value]


def wiki_db() -> ChatDb:
    """A chat in a tenant whose directory holds Internal Wiki with two roles."""
    db = ChatDb()
    db.systems.append(
        System(
            id=SYSTEM_ID,
            tenant_id=TENANT_ID,
            slug="internal-wiki",
            name="Internal Wiki",
            sensitivity="low",
            owner_user_id=None,
        )
    )
    for role_id, slug in ((EDITOR_ID, "editor"), (VIEWER_ID, "viewer")):
        db.roles.append(
            Role(
                id=role_id,
                tenant_id=TENANT_ID,
                system_id=SYSTEM_ID,
                slug=slug,
                name=slug.title(),
                privilege="read",
                default_expiry_days=90,
            )
        )
    return db


class Tools:
    """A tool caller that records every call and answers by tool name.

    A tool nobody scripted fails the test, which is how "no tools ran" is
    asserted as a hard stop and not only as a count read afterwards.
    """

    def __init__(self, answers: Mapping[str, Mapping[str, Any]] | None = None) -> None:
        self._answers = dict(answers or {})
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self) -> ToolCaller:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def call_tool(self, name: str, arguments: BaseModel) -> Mapping[str, Any]:
        self.calls.append((name, arguments.model_dump(mode="json")))
        if name not in self._answers:
            raise AssertionError(f"the turn called a tool nobody scripted: {name}")
        return self._answers[name]

    @property
    def names(self) -> list[str]:
        return [name for name, _arguments in self.calls]


def search_page() -> dict[str, Any]:
    """One `search_policy` page with a single obviously fake passage."""
    return {
        "citations": [
            {
                "document": "fake-access-policy",
                "title": "Fake Access Policy",
                "version": "v1",
                "heading_path": ["Fake Section"],
                "chunk_id": str(uuid.uuid4()),
                "text": "Obviously fake policy text about fake wiki access.",
                "score": 0.8,
            }
        ],
        "refused": False,
    }


def filing_tools() -> Tools:
    """Tools for a request that is searched, held, confirmed and filed."""
    return Tools(
        {
            SEARCH_POLICY: search_page(),
            CLASSIFY: _classify_payload(),
            CREATE: _create_payload(),
        }
    )


# --- staging the model ------------------------------------------------------------


def extraction_json(
    intent: str,
    *,
    confidence: float = 0.95,
    second_request: str | None = None,
    clarifying_question: str | None = None,
    **entities: Any,
) -> str:
    """The model's intent answer, in the schema's shape, with only the named
    fields filled."""
    return json.dumps(
        {
            "intent": intent,
            "entities": {**dict.fromkeys(IntentEntities.model_fields), **entities},
            "confidence": confidence,
            "clarifying_question": clarifying_question,
            "second_request": second_request,
        }
    )


def plan_json(*calls: tuple[str, dict[str, Any]]) -> str:
    """One planning answer; each call's arguments travel as a JSON string."""
    return json.dumps(
        {"calls": [{"tool": tool, "arguments": json.dumps(args)} for tool, args in calls]}
    )


def hold_plan(days: int, reason: str) -> str:
    """One round that classifies and files the editor request: it is held for a yes."""
    shared = {"target_user_id": str(USER_ID), "role_id": str(EDITOR_ID)}
    return plan_json(
        (CLASSIFY, {**shared, "requested_expiry_days": days}),
        (
            CREATE,
            {**shared, "justification": reason, "requested_expiry_days": days, "citations": []},
        ),
    )


def gateway_of(*texts: str) -> StagedGateway:
    """A gateway that gives exactly these answers, in order, and fails the test
    on one more request."""
    return StagedGateway(answers=[StagedAnswer(text=text) for text in texts])


def play(
    db: ChatDb,
    message: str,
    gateway: StagedGateway,
    tools: Tools | None = None,
) -> TurnState:
    """One whole turn through the real graph on the fake chat row."""

    async def scenario() -> TurnState:
        client = ModelClient(
            base_url=GATEWAY_URL,
            api_keys=API_KEYS,
            transport=httpx.MockTransport(gateway.handler),
        )
        try:
            deps = TurnDeps(
                db=cast(AsyncSession, db),
                model=client,
                tools=tools if tools is not None else Tools(),
                now=lambda: NOW,
            )
            return await run_turn(
                deps=deps,
                tenant_id=TENANT_ID,
                user_id=USER_ID,
                session_id=SESSION_ID,
                tenant_slug=TENANT_SLUG,
                message=message,
            )
        finally:
            await client.aclose()

    return asyncio.run(scenario())


def ask_for_details(db: ChatDb, tools: Tools) -> TurnState:
    """Turn one: "wiki editor access". Nothing else is said, so the details are asked."""
    return play(
        db,
        "I need wiki editor access",
        gateway_of(extraction_json("request_access", system="internal-wiki", role="editor")),
        tools,
    )


def reach_the_summary(db: ChatDb, tools: Tools) -> TurnState:
    """Turn two: the answer names only the days and the reason, and the turn ends
    on the "I'll file this" summary."""
    return play(
        db,
        "two weeks, to read the runbooks",
        gateway_of(
            extraction_json("request_access", expiry_days=14, justification=WIKI_REASON),
            hold_plan(14, WIKI_REASON),
        ),
        tools,
    )


# --- the draft carries an answer into the next turn ---------------------------------


def test_the_draft_carries_the_answer_into_the_next_turn() -> None:
    """Catches a turn that forgets what the person already said: the second
    answer names only days and a reason (the model's entities for system and
    role are empty), so without the saved draft the summary would be a question
    about the system. With it, the summary names Internal Wiki editor, 14 days and
    the reason, and files nothing."""
    db = wiki_db()
    tools = filing_tools()

    first = ask_for_details(db, tools)
    second = reach_the_summary(db, tools)

    assert first.outcome is TurnOutcome.CLARIFY
    assert second.outcome is TurnOutcome.CONFIRM
    assert second.reply is not None
    assert second.reply.startswith(
        f'I\'ll file this: Internal Wiki editor, 14 days, reason "{WIKI_REASON}". {FILE_LINE}'
    )
    assert CREATE not in tools.names


def test_the_first_turn_saves_what_it_learned_on_the_chat_row() -> None:
    """Catches a turn that merges the draft in memory and never stores it: the
    chat row must hold the system and the role after turn one, or turn two has
    nothing to fill from."""
    db = wiki_db()

    ask_for_details(db, filing_tools())

    saved = parse_request_draft(db.metadata.get(REQUEST_DRAFT_KEY), now=NOW)
    assert saved is not None
    assert saved.intent is Intent.REQUEST_ACCESS
    assert (saved.system, saved.role) == ("internal-wiki", "editor")
    assert (saved.expiry_days, saved.justification) == (None, None)


def test_turn_two_loads_the_draft_the_first_turn_saved() -> None:
    """Catches a `load_session` that never reads the draft onto the state: the
    turn's `request_draft` is the draft as stored, before this message changes
    it, and the merged result is what the extraction holds."""
    db = wiki_db()
    tools = filing_tools()
    ask_for_details(db, tools)

    second = reach_the_summary(db, tools)

    assert second.request_draft is not None
    assert second.request_draft.system == "internal-wiki"
    assert second.request_draft.expiry_days is None, "the draft as loaded, not as merged"
    assert second.extraction is not None
    assert second.extraction.entities.system == "internal-wiki"
    assert second.extraction.entities.expiry_days == 14


def test_a_draft_a_day_old_is_not_used() -> None:
    """Catches a turn that merges a draft whose 24 hours are over: the person
    asked for the wiki yesterday, so today's "two weeks, to read the runbooks"
    must be asked about the system again. The old value is planted on the chat
    row, so only the life check can ignore it."""
    db = wiki_db()
    tools = filing_tools()
    ask_for_details(db, tools)
    assert db.chat is not None
    old = RequestDraft(
        intent=Intent.REQUEST_ACCESS,
        system="internal-wiki",
        role="editor",
        saved_at=datetime(2026, 10, 19, 9, 0, tzinfo=UTC),  # exactly 24 hours before NOW
    )
    db.chat.session_metadata = {**db.metadata, REQUEST_DRAFT_KEY: draft_dump(old)}

    second = play(
        db,
        "two weeks, to read the runbooks",
        gateway_of(extraction_json("request_access", expiry_days=14, justification=WIKI_REASON)),
        tools,
    )

    assert second.outcome is TurnOutcome.CLARIFY
    assert second.request_draft is None
    assert second.reply is not None
    assert "which system you need" in second.reply


# --- a yes, a no and a stop clear the draft -----------------------------------------


@pytest.mark.parametrize("ending", ["yes", "no", "stop"])
def test_a_yes_a_no_and_a_stop_clear_the_draft(ending: str) -> None:
    """Catches a draft that outlives the request it described: after a yes that
    files, a bare no, or the stop line, the chat row must hold no `request_draft`,
    or the next request would start with the old system, role and reason. The
    draft is checked to be there just before the ending, so the ending is what
    removed it."""
    db = wiki_db()
    tools = filing_tools()

    if ending == "stop":
        own: dict[str, Any] = {"system": "internal-wiki", "role": "editor", "expiry_days": 14}
        play(
            db,
            "wiki editor for 14 days",
            gateway_of(extraction_json("request_access", **own)),
            tools,
        )
        play(
            db,
            "no reason",
            gateway_of(extraction_json("request_access", justification="no reason")),
            tools,
        )
        assert REQUEST_DRAFT_KEY in db.metadata, "premise: the draft is there before the stop"
        final = play(
            db, "dunno", gateway_of(extraction_json("request_access", justification="dunno")), tools
        )
        assert final.outcome is TurnOutcome.STOP
    else:
        ask_for_details(db, tools)
        reach_the_summary(db, tools)
        assert REQUEST_DRAFT_KEY in db.metadata, "premise: the draft is there before the answer"
        if ending == "yes":
            final = play(db, "yes", gateway_of(EMPTY_PLAN, FILED_REPLY), tools)
            assert CREATE in tools.names, "premise: the yes filed the request"
        else:
            final = play(db, "no", gateway_of(), tools)
            assert final.reply == NOTHING_FILED_REPLY

    assert REQUEST_DRAFT_KEY not in db.metadata


def test_a_request_after_a_filed_one_starts_from_nothing() -> None:
    """Catches a leftover draft reaching the next request: after the wiki request
    is filed, a new message that names only a reason must ask for the system
    again, not file against the wiki."""
    db = wiki_db()
    tools = filing_tools()
    ask_for_details(db, tools)
    reach_the_summary(db, tools)
    play(db, "yes", gateway_of(EMPTY_PLAN, FILED_REPLY), tools)

    later = play(
        db,
        "I need it to read the runbooks",
        gateway_of(extraction_json("request_access", justification=WIKI_REASON)),
        tools,
    )

    assert later.outcome is TurnOutcome.CLARIFY
    assert later.reply is not None
    assert "which system you need" in later.reply


# --- the real chat row -------------------------------------------------------------


@pytest.mark.db
def test_draft_round_trips_on_the_chat_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Catches a draft that is not really written to, or not really read back
    from, the JSONB column (a dict assigned in place that SQLAlchemy never sees
    change), and a clear that wipes the other keys on the row: setting `None`
    must remove only `request_draft` and keep the open question beside it."""
    draft = RequestDraft(
        intent=Intent.REQUEST_ACCESS,
        system="internal-wiki",
        role="editor",
        expiry_days=14,
        justification=WIKI_REASON,
        saved_at=NOW,
    )
    question = {"asked_at": NOW.isoformat(), "message": "a fake open question"}

    async def scenario() -> tuple[object, object, object, object]:
        seeded = await seed_tenant(session_factory, "draft-round-trip")
        chat_id = uuid.uuid4()
        async with tenant_session(session_factory, seeded.tenant_id) as db:
            await get_or_create_session(db, session_id=chat_id, user_id=seeded.user_id, now=NOW)
            await set_open_question(db, session_id=chat_id, value=question)
            await set_request_draft(db, session_id=chat_id, value=draft_dump(draft))
        async with tenant_session(session_factory, seeded.tenant_id) as db:
            read_back = await get_request_draft(db, session_id=chat_id)
        async with tenant_session(session_factory, seeded.tenant_id) as db:
            await set_request_draft(db, session_id=chat_id, value=None)
        async with tenant_session(session_factory, seeded.tenant_id) as db:
            after_clear = await get_request_draft(db, session_id=chat_id)
            kept_question = await get_open_question(db, session_id=chat_id)
        async with tenant_session(session_factory, seeded.tenant_id) as db:
            absent = await get_request_draft(db, session_id=uuid.uuid4())
        return read_back, after_clear, kept_question, absent

    read_back, after_clear, kept_question, absent = asyncio.run(scenario())

    assert read_back == draft_dump(draft)
    assert parse_request_draft(read_back, now=NOW) == draft
    assert after_clear is None
    assert kept_question == question
    assert absent is None
