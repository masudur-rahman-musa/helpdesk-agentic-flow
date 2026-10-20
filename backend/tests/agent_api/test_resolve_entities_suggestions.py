"""Did you mean one of these? `resolve_entities` offers roles after a first miss.

Surface under test (written before it exists; the implementer must provide
exactly this):

When the typed system or role matches nothing (today's CLARIFY outcome) and
``state.asked_before`` is false, ``resolve_entities`` calls
``deps.tools.call_tool("suggest_roles", SuggestRolesArgs(query=<the unmatched
terms joined by one space>))`` and validates the payload as
``SuggestRolesResult(roles=[...])``.

* Roles came back: outcome ``DISAMBIGUATE``; ``options`` is one label per role,
  in the tool's order, ``"<system_slug>.<role_slug> (<system_name> / <role_name>)"``;
  ``reply`` is ``"Did you mean one of these? "`` and the options joined by ``"; "``.
* No roles, or the tool raised a ``ToolError``: today's ``CLARIFY`` with
  ``NOTHING_MATCHED_QUESTION``. Never ``FAILED``.
* ``asked_before`` true: the tool is not called; the outcome stays ``ANSWER``
  with ``best_reading`` true.
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
    asked_before: bool = False,
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
                expiry_days=None,
                justification=None,
            ),
            confidence=0.9,
            clarifying_question=None,
        ),
        started_at=datetime(2026, 6, 2, tzinfo=UTC),
        asked_before=asked_before,
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
    the tool's), one that drops the system name from a label, and one that
    sends the wrong words: the call must be the wire name with the unmatched
    terms joined by one space."""
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
        "wiki.editor (Internal Wiki / wiki.editor)",
        "crm.contributor (Fake Customer Tool / crm.contributor)",
        "crm.author (Fake Customer Tool / crm.author)",
    )
    assert state.outcome is TurnOutcome.DISAMBIGUATE
    assert state.options == expected
    assert state.reply == f"{OFFER_PREFIX}{'; '.join(expected)}"
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
    """Catches a resolver that suggests on every miss: after one question the
    turn must answer from its best reading with NO tools, so the one-question
    cap still holds. The payload is a good one, so only the flag can stop it."""
    tools = _StubTools(payload={"roles": [_suggested("wiki", "Internal Wiki", "e", "wiki.e")]})

    state = _resolve(tools, system="docs", role="scribe", asked_before=True)

    assert tools.calls == []
    assert state.outcome is TurnOutcome.ANSWER
    assert state.best_reading is True


def test_a_found_name_never_calls_the_tool() -> None:
    """Catches a resolver that asks for suggestions on every turn, not only on
    a miss: "wiki" / "editor" is in the directory and must resolve unchanged."""
    tools = _StubTools(payload={"roles": [_suggested("wiki", "Internal Wiki", "e", "wiki.e")]})

    state = _resolve(tools, system="wiki", role="editor")

    assert tools.calls == []
    assert state.outcome is TurnOutcome.CONTINUE
    assert state.resolved is not None
    assert state.resolved.role_slug == "editor"
    assert state.resolved.system_slug == "wiki"


def test_two_matches_still_ask_which_one_without_the_tool() -> None:
    """Catches a resolver that suggests for any non-resolving outcome: two
    systems sharing a name are an ambiguity (a different question), not a
    miss, and must keep the "Which one do you mean?" question."""
    twins = [_system("billing-eu", "Fake Billing"), _system("billing-us", "Fake Billing")]
    tools = _StubTools(payload={"roles": [_suggested("wiki", "Internal Wiki", "e", "wiki.e")]})

    state = _resolve(tools, system="Fake Billing", role=None, systems=twins, roles=[])

    assert tools.calls == []
    assert state.outcome is TurnOutcome.DISAMBIGUATE
    assert state.reply is not None
    assert state.reply.startswith(DISAMBIGUATION_PREFIX)


def test_the_planner_cannot_propose_suggest_roles() -> None:
    """Catches the tool added to the planner's reach: in an `INTENT_TOOLS` row
    or in `TOOL_INPUTS`, the model could propose it, and only the resolving
    step may call it."""
    assert not [intent for intent, tools in gates.INTENT_TOOLS.items() if SUGGEST in tools]
    assert SUGGEST not in TOOL_INPUTS
