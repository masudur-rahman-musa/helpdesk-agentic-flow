"""A named system narrows the roles: "wiki editor" must not offer every editor."""

import asyncio
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.agent_api.agent.matching import Candidate, match_candidates
from helpdesk.agent_api.agent.nodes.resolve_entities import resolve_entities, roles_in_system
from helpdesk.agent_api.agent.state import (
    Intent,
    IntentEntities,
    IntentExtraction,
    ResolvedEntities,
    TurnState,
)
from helpdesk.agent_api.agent.turn_deps import TurnDeps
from helpdesk.agent_api.services.mcp_client import ToolCaller
from helpdesk.agent_api.services.model_gateway.client import ModelClient
from helpdesk.shared.models import Role, System


def _role(system_id: uuid.UUID, slug: str, name: str) -> Role:
    return Role(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        system_id=system_id,
        slug=slug,
        name=name,
        privilege="write",
        default_expiry_days=90,
    )


def _as_candidates(roles: Sequence[Role]) -> tuple[Candidate, ...]:
    return tuple(Candidate(id=r.id, slug=r.slug, name=r.name) for r in roles)


def test_named_system_leaves_one_editor() -> None:
    """Catches roles matched tenant-wide: the term "editor" hits all three
    editor roles even though the user named the wiki, so the chat loops."""
    ledger, crm, wiki = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    roles = [
        _role(ledger, "editor", "ledger.editor"),
        _role(crm, "editor", "crm.editor"),
        _role(wiki, "editor", "wiki.editor"),
    ]
    named = (Candidate(id=wiki, slug="wiki", name="Internal Wiki"),)

    matches = match_candidates("editor", _as_candidates(roles_in_system(roles, named)))

    assert [m.name for m in matches] == ["wiki.editor"]


def test_no_single_system_keeps_every_role() -> None:
    """With no system, or an unsettled several, nothing may be hidden."""
    roles = [_role(uuid.uuid4(), "editor", "a.editor"), _role(uuid.uuid4(), "editor", "b.editor")]
    two = (
        Candidate(id=roles[0].system_id, slug="a", name="A"),
        Candidate(id=roles[1].system_id, slug="b", name="B"),
    )

    assert list(roles_in_system(roles, ())) == roles
    assert list(roles_in_system(roles, two)) == roles


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


def _system(slug: str, name: str) -> System:
    return System(id=uuid.uuid4(), tenant_id=uuid.uuid4(), slug=slug, name=name)


def _resolve(
    systems: Sequence[System], roles: Sequence[Role], *, system: str | None, role: str | None
) -> ResolvedEntities:
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
    )
    deps = TurnDeps(
        db=cast(AsyncSession, _FakeDb(systems, roles)),
        model=cast(ModelClient, None),
        tools=cast(ToolCaller, None),
        now=lambda: datetime(2026, 6, 2, tzinfo=UTC),
    )
    resolved = asyncio.run(resolve_entities(state, deps)).resolved
    assert resolved is not None
    return resolved


def test_other_role_names_leave_out_the_asked_role() -> None:
    """Catches an implementation that includes the asked role in the list,
    includes roles of other systems, or sorts by display name instead of slug.
    The names are chosen so slug order and name order differ."""
    kyc, vpn = _system("kyc", "KYC"), _system("vpn", "VPN")
    roles = [
        _role(kyc.id, "kyc-viewer", "Alpha Viewer"),
        _role(kyc.id, "kyc-admin", "KYC Admin"),
        _role(kyc.id, "kyc-analyst", "Zulu Analyst"),
        _role(vpn.id, "vpn-user", "VPN User"),
    ]

    resolved = _resolve([kyc, vpn], roles, system="kyc", role="kyc-admin")

    assert resolved.role_slug == "kyc-admin"
    assert resolved.other_role_names == ("Zulu Analyst", "Alpha Viewer")


def test_a_one_role_system_has_no_other_role_names() -> None:
    """Catches an implementation that lists the asked role itself (or roles of
    other systems) as "another role" when the system has only one."""
    kyc, vpn = _system("kyc", "KYC"), _system("vpn", "VPN")
    roles = [_role(vpn.id, "vpn-user", "VPN User"), _role(kyc.id, "kyc-admin", "KYC Admin")]

    resolved = _resolve([kyc, vpn], roles, system="vpn", role="vpn-user")

    assert resolved.role_slug == "vpn-user"
    assert resolved.other_role_names == ()


def test_no_resolved_role_gives_no_other_role_names() -> None:
    """Catches an implementation that fills the list from the system alone, so
    a person who only named a system is invited to ask for "another role"
    they never started with. The system has roles, so only the missing role id
    can make the list empty."""
    kyc = _system("kyc", "KYC")
    roles = [_role(kyc.id, "kyc-admin", "KYC Admin"), _role(kyc.id, "kyc-viewer", "Viewer")]

    resolved = _resolve([kyc], roles, system="kyc", role=None)

    assert resolved.system_slug == "kyc"
    assert resolved.role_id is None
    assert resolved.other_role_names == ()


def test_extra_words_around_a_role_in_a_named_system_match_it() -> None:
    """Catches a resolver that cannot read a role out of a longer sentence:
    "please give me approver rights" holds the whole word "approver" of exactly
    one role in the named KYC, so that role, and only it, must resolve."""
    kyc = _system("kyc", "KYC")
    roles = [
        _role(kyc.id, "approver", "kyc.approver"),
        _role(kyc.id, "analyst", "kyc.analyst"),
        _role(kyc.id, "editor", "kyc.editor"),
    ]

    resolved = _resolve([kyc], roles, system="kyc", role="please give me approver rights")

    assert resolved.role_slug == "approver"
    assert resolved.role_name == "kyc.approver"
