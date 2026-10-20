"""A role in the person's department template asks nothing.

Names the implementation adds (fixed by the plan):

* ``TurnState.template_role_ids: frozenset[uuid.UUID]`` (empty by default), read
  by ``load_session`` from the caller's own directory row: the user's department,
  then ``fetch_role_templates_for_department``. Empty when the user or the
  department is missing.
* ``resolve_entities``: a resolved role whose id is in that set, on a
  ``REQUEST_ACCESS``, skips the details question. The returned extraction keeps
  a given reason, else "Part of my department's starting access."; the length is
  left as it was (``None`` stays ``None``). Outcome ``CONTINUE``, reply ``None``.

The first group runs the resolving step on plain fakes. The second group reads
the template through a real tenant-bound session, because only the database
shows the department join and the tenant scope.
"""

import asyncio
import uuid
from datetime import UTC, date, datetime
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.agent_api.agent.clarify_cap import QuestionKind
from helpdesk.agent_api.agent.nodes.load_session import load_session
from helpdesk.agent_api.agent.nodes.resolve_entities import resolve_entities
from helpdesk.agent_api.agent.state import TurnOutcome, TurnState
from helpdesk.agent_api.agent.turn_deps import TurnDeps
from helpdesk.agent_api.services.mcp_client import ToolCaller
from helpdesk.agent_api.services.model_gateway.client import ModelClient
from helpdesk.shared.core.db import tenant_session
from helpdesk.shared.models import Department, Role, RoleTemplate, System, Tenant, User
from tests.agent_api.conftest import AgentDb
from tests.agent_api.test_question_kinds import EDITOR, TURN_NOW, WIKI, _extraction
from tests.agent_api.test_resolve_entities_suggestions import _FakeDb

STARTING_ACCESS_REASON = "Part of my department's starting access."
LOAD_NOW = datetime(2026, 10, 19, tzinfo=UTC)


def _resolve_with_template(
    *,
    template: frozenset[uuid.UUID],
    system: str | None,
    role: str | None,
    justification: str | None = None,
    expiry_days: int | None = None,
) -> TurnState:
    """One resolving step with the given template role ids on the state."""
    state = TurnState(
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        tenant_slug="fake-tenant",
        user_message="fake message",
        extraction=_extraction(
            system=system, role=role, justification=justification, expiry_days=expiry_days
        ),
        started_at=TURN_NOW,
        template_role_ids=template,
    )
    deps = TurnDeps(
        db=cast(AsyncSession, _FakeDb([WIKI], [EDITOR])),
        model=cast(ModelClient, None),
        tools=cast(ToolCaller, None),
        now=lambda: TURN_NOW,
    )
    return asyncio.run(resolve_entities(state, deps))


def test_a_template_role_with_no_reason_goes_on() -> None:
    """Catches a resolver that still asks "how long and why" for a role in the
    department template, or one that leaves the reason empty or invents a length:
    no reason and no length must continue with no reply, the starting-access
    reason filled and the length still empty so the role's default applies."""
    state = _resolve_with_template(template=frozenset({EDITOR.id}), system="wiki", role="editor")

    assert state.outcome is TurnOutcome.CONTINUE
    assert state.reply is None
    assert state.extraction is not None
    assert state.extraction.entities.justification == STARTING_ACCESS_REASON
    assert state.extraction.entities.expiry_days is None


def test_a_template_role_keeps_the_given_reason() -> None:
    """Catches a resolver that overwrites a reason the person gave with the
    starting-access text: the given reason must come back word for word."""
    given = "to write the team pages"

    state = _resolve_with_template(
        template=frozenset({EDITOR.id}), system="wiki", role="editor", justification=given
    )

    assert state.outcome is TurnOutcome.CONTINUE
    assert state.extraction is not None
    assert state.extraction.entities.justification == given


def test_a_template_role_keeps_the_given_length() -> None:
    """Catches a resolver that drops a length the person gave (or replaces it
    with the role default) while filling the reason: 30 days with no reason must
    stay 30 and the reason must still be filled."""
    state = _resolve_with_template(
        template=frozenset({EDITOR.id}), system="wiki", role="editor", expiry_days=30
    )

    assert state.outcome is TurnOutcome.CONTINUE
    assert state.extraction is not None
    assert state.extraction.entities.expiry_days == 30
    assert state.extraction.entities.justification == STARTING_ACCESS_REASON


def test_a_role_outside_the_template_still_asks() -> None:
    """Catches a resolver that skips the details question for every role once the
    template is non-empty: a template holding some OTHER role must leave this
    role asking the one question, kind `details`, with the reason still empty."""
    state = _resolve_with_template(template=frozenset({uuid.uuid4()}), system="wiki", role="editor")

    assert state.outcome is TurnOutcome.CLARIFY
    assert state.reply == (
        "That's Internal Wiki editor. "
        "Before I check this, tell me how long you need it for and the business reason."
    )
    assert state.question_kind is QuestionKind.DETAILS
    assert state.extraction is not None
    assert state.extraction.entities.justification is None


def test_a_system_with_no_role_still_asks() -> None:
    """Catches a resolver that treats a found system as a template hit: the
    system is found, no role is named, and the system's only role is in the
    template, so only the missing resolved role can keep the question asked."""
    state = _resolve_with_template(template=frozenset({EDITOR.id}), system="wiki", role=None)

    assert state.outcome is TurnOutcome.CLARIFY
    assert state.question_kind is QuestionKind.DETAILS
    assert state.extraction is not None
    assert state.extraction.entities.justification is None


# --- the template read through a real session -----------------------------------


async def _add_tenant(db: AgentDb) -> uuid.UUID:
    async with db.factory() as session:
        tenant = Tenant(
            slug=f"template-{uuid.uuid4().hex[:12]}",
            name="Fake Template Tenant",
            residency="eu",
            settings={},
        )
        session.add(tenant)
        await session.commit()
        return tenant.id


async def _add_department(
    db: AgentDb, tenant_id: uuid.UUID, name: str, template_slug: str
) -> tuple[uuid.UUID, uuid.UUID]:
    """One department with one person and two roles; the template holds only the
    role named `template_slug`. Returns the person's id and that role's id."""
    async with tenant_session(db.factory, tenant_id) as session:
        department = Department(tenant_id=tenant_id, name=name, manager_user_id=None)
        session.add(department)
        await session.flush()
        person = User(
            tenant_id=tenant_id,
            department_id=department.id,
            email=f"fake.{name}@{uuid.uuid4().hex[:8]}.example.test",
            display_name=f"Fake {name}",
            password_hash="argon2id$obviously-fake-test-hash",
            principals=["employee"],
            status="active",
            start_date=date(2026, 1, 5),
            end_date=None,
        )
        session.add(person)
        await session.flush()
        system = System(
            tenant_id=tenant_id,
            slug=f"sys-{name}",
            name=f"Fake System {name}",
            sensitivity="low",
            owner_user_id=person.id,
        )
        session.add(system)
        await session.flush()
        role_ids: dict[str, uuid.UUID] = {}
        for slug in (template_slug, "untemplated"):
            role = Role(
                tenant_id=tenant_id,
                system_id=system.id,
                slug=f"{slug}-{name}",
                name=f"Fake {slug}",
                privilege="read",
                default_expiry_days=90,
            )
            session.add(role)
            await session.flush()
            role_ids[slug] = role.id
        session.add(
            RoleTemplate(
                tenant_id=tenant_id,
                department_id=department.id,
                role_id=role_ids[template_slug],
            )
        )
        await session.flush()
        return person.id, role_ids[template_slug]


async def _add_person_without_department(db: AgentDb, tenant_id: uuid.UUID) -> uuid.UUID:
    """A person with no department. The chat row needs a real user row, so a
    user with no row at all never reaches the template read."""
    async with tenant_session(db.factory, tenant_id) as session:
        person = User(
            tenant_id=tenant_id,
            department_id=None,
            email=f"fake.nodept@{uuid.uuid4().hex[:8]}.example.test",
            display_name="Fake No Department",
            password_hash="argon2id$obviously-fake-test-hash",
            principals=["employee"],
            status="active",
            start_date=date(2026, 1, 5),
            end_date=None,
        )
        session.add(person)
        await session.flush()
        return person.id


async def _load(db: AgentDb, tenant_id: uuid.UUID, user_id: uuid.UUID) -> frozenset[uuid.UUID]:
    """Run `load_session` for the user and return the template ids it read."""
    state = TurnState(
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=uuid.uuid4(),
        tenant_slug="fake-tenant",
        user_message="i want doc access",
        started_at=LOAD_NOW,
    )
    async with tenant_session(db.factory, tenant_id) as session:
        deps = TurnDeps(
            db=session,
            model=cast(ModelClient, None),
            tools=cast(ToolCaller, None),
            now=lambda: LOAD_NOW,
        )
        loaded = await load_session(state, deps)
    return loaded.template_role_ids


@pytest.mark.db
def test_load_session_reads_the_template(agent_db: AgentDb) -> None:
    """Catches a loader that reads the wrong template: every template row of the
    tenant instead of the caller's department (a second department in the same
    tenant holds its own template role), another tenant's rows, or a user with no
    department getting anything but an empty set."""

    async def scenario() -> tuple[frozenset[uuid.UUID], frozenset[uuid.UUID], uuid.UUID]:
        tenant_id = await _add_tenant(agent_db)
        person_id, mine_role = await _add_department(agent_db, tenant_id, "mine", "starter")
        await _add_department(agent_db, tenant_id, "sibling", "other")
        other_tenant = await _add_tenant(agent_db)
        await _add_department(agent_db, other_tenant, "foreign", "alien")
        mine = await _load(agent_db, tenant_id, person_id)
        stranger = await _load(
            agent_db, tenant_id, await _add_person_without_department(agent_db, tenant_id)
        )
        return mine, stranger, mine_role

    mine, stranger, mine_role = asyncio.run(scenario())

    assert mine == frozenset({mine_role})
    assert stranger == frozenset()
