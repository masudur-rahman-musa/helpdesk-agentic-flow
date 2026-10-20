"""Did you mean one of these? `resolve_entities` offers roles after a first miss.

Surface under test (written before it exists; the implementer must provide
exactly this):

When the typed system or role matches nothing (today's CLARIFY outcome) and
the last question was not a suggestion or the exact-name question
(``state.asked_kind``), ``resolve_entities`` calls
``deps.tools.call_tool("suggest_roles", SuggestRolesArgs(query=<the unmatched
terms joined by one space>))`` and validates the payload as
``SuggestRolesResult(roles=[...])``.

* Roles came back: outcome ``DISAMBIGUATE``; ``options`` is one plain name per
  role, in the tool's order, ``"<system_name> <role_slug words>"``;
  ``reply`` is ``"Did you mean one of these? "`` and the roles grouped by system
  (``"Internal Wiki: editor or viewer; Mail and Files: user"``).
* No roles, or the tool raised a ``ToolError``: today's ``CLARIFY`` with
  ``NOTHING_MATCHED_QUESTION``. Never ``FAILED``.
* After a ``suggest`` question: the tool is not called; the exact-name question is
  asked (``CLARIFY``). After a ``name`` question the outcome is ``ANSWER``.
* A name that matches never calls the tool.
* ``suggest_roles`` is in no ``INTENT_TOOLS`` row and not in ``TOOL_INPUTS``.

The database and the tool caller are plain fakes; nothing here touches a
service. Built the way ``test_resolve_entities_roles.py`` builds its deps.
"""

import asyncio
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.agent_api.agent import gates
from helpdesk.agent_api.agent.clarify_cap import QuestionKind
from helpdesk.agent_api.agent.nodes.resolve_entities import (
    DISAMBIGUATION_PREFIX,
    NOTHING_MATCHED_QUESTION,
    resolve_entities,
)
from helpdesk.agent_api.agent.state import (
    Intent,
    IntentEntities,
    IntentExtraction,
    TurnOutcome,
    TurnState,
)
from helpdesk.agent_api.agent.tool_args import TOOL_INPUTS
from helpdesk.agent_api.agent.turn_deps import TurnDeps
from helpdesk.agent_api.services.mcp_client import (
    RemoteForbiddenError,
    RemoteInternalError,
    ToolCaller,
)
from helpdesk.agent_api.services.model_gateway.client import ModelClient
from helpdesk.shared.core.errors import ToolError
from helpdesk.shared.models import Role, System

SUGGEST = "suggest_roles"
OFFER_PREFIX = "Did you mean one of these? "


class _Rows:
    def __init__(self, rows: Sequence[Any]) -> None:
        self._rows = rows

    def scalars(self) -> "_Rows":
        return self

    def all(self) -> Sequence[Any]:
        return self._rows


class _FakeDb:
    """Answers `Repository.list()` for `System` and `Role` from plain lists."""

    def __init__(self, systems: Sequence[System], roles: Sequence[Role]) -> None:
        self._by_model: dict[Any, Sequence[Any]] = {System: systems, Role: roles}

    async def execute(self, statement: Any) -> _Rows:
        return _Rows(self._by_model[statement.column_descriptions[0]["entity"]])


class _StubTools:
    """A `ToolCaller` that records each call and answers what the case says."""

    def __init__(
        self, *, payload: Mapping[str, Any] | None = None, error: ToolError | None = None
    ) -> None:
        self._payload: Mapping[str, Any] = payload if payload is not None else {"roles": []}
        self._error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, arguments: BaseModel) -> Mapping[str, Any]:
        self.calls.append((name, arguments.model_dump()))
        if self._error is not None:
            raise self._error
        return self._payload


def _system(slug: str, name: str) -> System:
    return System(id=uuid.uuid4(), tenant_id=uuid.uuid4(), slug=slug, name=name)


def _role(system: System, slug: str, name: str) -> Role:
    return Role(
        id=uuid.uuid4(),
        tenant_id=system.tenant_id,
        system_id=system.id,
        slug=slug,
        name=name,
        privilege="write",
        default_expiry_days=90,
    )


def _suggested(
    system_slug: str, system_name: str, role_slug: str, role_name: str
) -> dict[str, Any]:
    return {
        "system_slug": system_slug,
        "system_name": system_name,
        "role_slug": role_slug,
        "role_name": role_name,
        "score": 0.8,
    }


WIKI = _system("wiki", "Internal Wiki")
DIRECTORY_SYSTEMS = [WIKI, _system("crm", "Fake Customer Tool")]
DIRECTORY_ROLES = [_role(WIKI, "editor", "wiki.editor")]


def _resolve(
    tools: _StubTools,
    *,
    system: str | None,
    role: str | None,
    asked_kind: QuestionKind | None = None,
    justification: str | None = None,
    expiry_days: int | None = None,
    systems: Sequence[System] = DIRECTORY_SYSTEMS,
    roles: Sequence[Role] = DIRECTORY_ROLES,
) -> TurnState:
    state = TurnState(
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        tenant_slug="fake-tenant",
        user_message="fake message",
        extraction=IntentExtraction(
            intent=Intent.REQUEST_ACCESS,
            entities=IntentEntities(
                system=system,
                role=role,
                target_user=None,
                department=None,
                expiry_days=expiry_days,
                justification=justification,
            ),
            confidence=0.9,
            clarifying_question=None,
        ),
        started_at=datetime(2026, 6, 2, tzinfo=UTC),
        asked_before=asked_kind is not None,
        asked_kind=asked_kind,
    )
    deps = TurnDeps(
        db=cast(AsyncSession, _FakeDb(systems, roles)),
        model=cast(ModelClient, None),
        tools=cast(ToolCaller, tools),
        now=lambda: datetime(2026, 6, 2, tzinfo=UTC),
    )
    return asyncio.run(resolve_entities(state, deps))


def test_a_miss_offers_the_suggestions() -> None:
    """Catches a resolver that still asks for the exact name when roles were
    found, one that reorders the tool's list (slug order here is the reverse of
    the tool's), one that shows `slug (name)` labels or the stored role name
    instead of the plain name, one that does not group the reply by system, and
    one that sends the wrong words: the call must be the wire name with the
    unmatched terms joined by one space."""
    payload = {
        "roles": [
            _suggested("wiki", "Internal Wiki", "editor", "wiki.editor"),
            _suggested("crm", "Fake Customer Tool", "contributor", "crm.contributor"),
            _suggested("crm", "Fake Customer Tool", "author", "crm.author"),
        ]
    }
    tools = _StubTools(payload=payload)

    state = _resolve(tools, system="docs", role="scribe")

    expected = (
        "Internal Wiki editor",
        "Fake Customer Tool contributor",
        "Fake Customer Tool author",
    )
    assert state.outcome is TurnOutcome.DISAMBIGUATE
    assert state.options == expected
    assert state.reply == (
        f"{OFFER_PREFIX}Internal Wiki: editor; Fake Customer Tool: contributor or author"
    )
    assert tools.calls == [(SUGGEST, {"query": "docs scribe"})]


@pytest.mark.parametrize(
    ("system", "role", "query"),
    [
        pytest.param("docs", "scribe", "docs scribe", id="both-unmatched"),
        pytest.param(None, "scribe", "scribe", id="only-a-role-typed"),
        pytest.param("wiki", "scribe", "scribe", id="system-matched-role-not"),
    ],
)
def test_the_query_is_the_unmatched_terms_only(
    system: str | None, role: str | None, query: str
) -> None:
    """Catches a query built from the matched words too: "wiki" found a real
    system, so sending it along would pull the suggestions toward a system the
    person already got right."""
    tools = _StubTools(payload={"roles": [_suggested("wiki", "Internal Wiki", "e", "wiki.e")]})

    state = _resolve(tools, system=system, role=role)

    assert state.outcome is TurnOutcome.DISAMBIGUATE
    assert tools.calls == [(SUGGEST, {"query": query})]


def test_no_suggestion_keeps_todays_question() -> None:
    """Catches a resolver that turns an empty answer into an empty offer
    ("Did you mean one of these? " and nothing after it): nothing close enough
    must leave today's question, word for word."""
    tools = _StubTools(payload={"roles": []})

    state = _resolve(tools, system="docs", role="scribe")

    assert state.outcome is TurnOutcome.CLARIFY
    assert state.reply == NOTHING_MATCHED_QUESTION
    assert state.options == ()
    assert len(tools.calls) == 1, "the premise: the tool was asked and had nothing"


@pytest.mark.parametrize(
    "error",
    [RemoteInternalError("fake outage"), RemoteForbiddenError("fake refusal")],
    ids=["internal", "forbidden"],
)
def test_a_tool_failure_keeps_todays_question(error: ToolError) -> None:
    """Catches a resolver that lets a failed suggestion end the turn: the
    INTERNAL case must not become `FAILED` (a search outage would then break a
    request that works without it), and no other code may escape either."""
    tools = _StubTools(error=error)

    state = _resolve(tools, system="docs", role="scribe")

    assert len(tools.calls) == 1, "the premise: the tool was asked and raised"
    assert state.outcome is TurnOutcome.CLARIFY
    assert state.reply == NOTHING_MATCHED_QUESTION


def test_a_second_miss_does_not_suggest() -> None:
    """Catches a resolver that suggests again right after a suggestion: the cap
    is now per kind, so after a `suggest` question the turn must call no tool
    and ask for the exact name. The payload is a good one, so only the stored
    kind can stop it."""
    tools = _StubTools(payload={"roles": [_suggested("wiki", "Internal Wiki", "e", "wiki.e")]})

    state = _resolve(tools, system="docs", role="scribe", asked_kind=QuestionKind.SUGGEST)

    assert tools.calls == []
    assert state.outcome is TurnOutcome.CLARIFY
    assert state.reply == NOTHING_MATCHED_QUESTION


def test_a_found_name_never_calls_the_tool() -> None:
    """Catches a resolver that asks for suggestions on every turn, not only on
    a miss: "wiki" / "editor" is in the directory and must resolve unchanged.
    The reason and length are given, so the details question is not asked."""
    tools = _StubTools(payload={"roles": [_suggested("wiki", "Internal Wiki", "e", "wiki.e")]})

    state = _resolve(
        tools, system="wiki", role="editor", justification="fake reason", expiry_days=14
    )

    assert tools.calls == []
    assert state.outcome is TurnOutcome.CONTINUE
    assert state.resolved is not None
    assert state.resolved.role_slug == "editor"
    assert state.resolved.system_slug == "wiki"


def test_two_matches_still_ask_which_one_without_the_tool() -> None:
    """Catches a resolver that suggests for any non-resolving outcome: two
    systems sharing a name are an ambiguity (a different question), not a
    miss, and must keep the "Which one do you mean?" question. System options
    keep their `slug (name)` form, because two systems can share a name and a
    plain name would make the two options identical."""
    twins = [_system("billing-eu", "Fake Billing"), _system("billing-us", "Fake Billing")]
    tools = _StubTools(payload={"roles": [_suggested("wiki", "Internal Wiki", "e", "wiki.e")]})

    state = _resolve(tools, system="Fake Billing", role=None, systems=twins, roles=[])

    assert tools.calls == []
    assert state.outcome is TurnOutcome.DISAMBIGUATE
    assert state.reply is not None
    assert state.reply.startswith(DISAMBIGUATION_PREFIX)
    assert state.options == ("billing-eu (Fake Billing)", "billing-us (Fake Billing)")


def test_the_planner_cannot_propose_suggest_roles() -> None:
    """Catches the tool added to the planner's reach: in an `INTENT_TOOLS` row
    or in `TOOL_INPUTS`, the model could propose it, and only the resolving
    step may call it."""
    assert not [intent for intent, tools in gates.INTENT_TOOLS.items() if SUGGEST in tools]
    assert SUGGEST not in TOOL_INPUTS


STORED_NAMES = ("wiki.editor", "wiki.viewer")


def test_the_offer_never_shows_a_stored_name() -> None:
    """Catches an offer that still prints the stored role names or the old
    `slug (name)` labels: the tool answers with `wiki.editor` and `wiki.viewer`
    as role names and `wiki` as the system slug, so any of those forms, or a
    bracket, in the reply or in an option shows the stored value leaking."""
    payload = {
        "roles": [
            _suggested("wiki", "Internal Wiki", "editor", "wiki.editor"),
            _suggested("wiki", "Internal Wiki", "viewer", "wiki.viewer"),
        ]
    }
    tools = _StubTools(payload=payload)

    state = _resolve(tools, system="docs", role="scribe")

    assert state.outcome is TurnOutcome.DISAMBIGUATE
    assert state.reply == f"{OFFER_PREFIX}Internal Wiki: editor or viewer"
    assert state.options == ("Internal Wiki editor", "Internal Wiki viewer")
    shown = [state.reply, *state.options]
    for text in shown:
        for stored in STORED_NAMES:
            assert stored not in text
        assert "(" not in text
        assert "." not in text.removeprefix(OFFER_PREFIX)


@pytest.mark.parametrize(
    ("system", "role"),
    [
        pytest.param("wiki editor", None, id="short-phrase-in-system-field"),
        pytest.param("Internal Wiki editor", None, id="plain-name-in-system-field"),
        pytest.param(None, "wiki editor", id="short-phrase-in-role-field"),
        pytest.param(None, "Internal Wiki editor", id="plain-name-in-role-field"),
    ],
)
def test_typing_back_a_plain_name_finds_the_role(system: str | None, role: str | None) -> None:
    """Catches a matcher that only knows the stored name or the bare slug: a
    second system also has an `editor` role, so "editor" alone is two roles,
    and only the plain name (or the system words with the role word) picks the
    wiki one. The reason and length are given so the details question is not
    asked, and the suggest tool must stay uncalled."""
    crm = _system("crm", "Fake Customer Tool")
    roles = [_role(WIKI, "editor", "wiki.editor"), _role(crm, "editor", "crm.editor")]
    tools = _StubTools(payload={"roles": [_suggested("wiki", "Internal Wiki", "e", "wiki.e")]})

    state = _resolve(
        tools,
        system=system,
        role=role,
        asked_kind=QuestionKind.SUGGEST,
        justification="fake reason",
        expiry_days=14,
        systems=[WIKI, crm],
        roles=roles,
    )

    assert tools.calls == []
    assert state.outcome is TurnOutcome.CONTINUE
    assert state.resolved is not None
    assert state.resolved.role_id == roles[0].id
    assert state.resolved.role_slug == "editor"
    assert state.resolved.role_name == "Internal Wiki editor"
