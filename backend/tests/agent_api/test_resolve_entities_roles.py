"""A named system narrows the roles: "wiki editor" must not offer every editor."""

import uuid
from collections.abc import Sequence

from helpdesk.agent_api.agent.matching import Candidate, match_candidates
from helpdesk.agent_api.agent.nodes.resolve_entities import roles_in_system
from helpdesk.shared.models import Role


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
