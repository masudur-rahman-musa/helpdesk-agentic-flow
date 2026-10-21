"""The demo accounts meet in one request.

Surface under test (written before it exists): in both tenants the demo
`manager` is the demo employee's own department manager, and the demo
`system_owner` owns the production infrastructure system (`prod-k8s` at
Nordlicht, `prod-aws` at Cuan). One request from the demo employee for that
system's auditor role then reaches the demo manager, the demo system owner and
the demo IT admin.

Fixture cost: four cases read `generate_tenant(...)` rows or one doc file with
no service. The one `db` case rides the session-scoped `mcp_env` seam on the
shared seeded database and files one request through the real tool; it builds
no engine and reads the notifications by the request it made.
"""

import uuid
from pathlib import Path

import pytest

from scripts.seed import CUAN, NORDLICHT, TenantSpec, generate_tenant
from tests.support.mcp import (
    SeededMcp,
    SeededUser,
    bearer,
    call_tool,
    sql_rows,
    token_for,
)

DOC = Path(__file__).resolve().parents[3] / "docs" / "demo-accounts.md"

PICKS = [
    pytest.param(NORDLICHT, "Cornelia Zorbach", "Friedhold Tröst", "prod-k8s", id="nordlicht"),
    pytest.param(CUAN, "Freya McPolin", "Marco Prial", "prod-aws", id="cuan"),
]
OLD_PICKS = {
    "nordlicht": ("Adriane Karge", "Zdravko Wagenknecht"),
    "cuan": ("Faith Steed", "Padraigin Fee"),
}


@pytest.mark.parametrize(("spec", "manager_name", "owner_name", "system"), PICKS)
def test_the_demo_manager_manages_the_demo_employee(
    spec: TenantSpec, manager_name: str, owner_name: str, system: str
) -> None:
    """Catches a seed that keeps the demo manager in the first department
    (Engineering) while the demo employee works in the second, so the manager
    never sees what the employee asks for.

    The department is read from the employee row, so a manager picked from any
    other department fails even when the name list is unchanged.
    """
    rows = generate_tenant(spec)
    employee = rows.demo_accounts["employee"]
    manager = rows.demo_accounts["manager"]

    assert manager is rows.users_by_department[employee.department][0]
    assert "manager" in manager.principals
    assert manager.display_name == manager_name


@pytest.mark.parametrize(("spec", "manager_name", "owner_name", "system"), PICKS)
def test_the_demo_system_owner_owns_the_production_infrastructure_system(
    spec: TenantSpec, manager_name: str, owner_name: str, system: str
) -> None:
    """Catches a seed that leaves the demo system owner on the tenant's first
    system (ERP, Core Banking), where the policy adds no IT admin seat, so one
    request cannot reach all three demo accounts.

    The `prod_infra` system is found from the spec's categories, so a wrong
    owner fails even if the slug in this file were also wrong.
    """
    rows = generate_tenant(spec)
    infra = [slug for slug, _name, _sens, category in spec.systems if category == "prod_infra"]

    assert infra == [system]
    owner = rows.demo_accounts["system_owner"]
    assert owner is rows.system_owners[system]
    assert "system_owner" in owner.principals
    assert owner.display_name == owner_name


@pytest.mark.parametrize("spec", [NORDLICHT, CUAN], ids=["nordlicht", "cuan"])
def test_the_seven_demo_accounts_are_seven_people(spec: TenantSpec) -> None:
    """Catches a pick that gives one person two demo passwords (the manager or
    owner collapsing onto another demo seat) and a seed that leaves the old
    picks holding a demo password.
    """
    rows = generate_tenant(spec)
    holders = list(rows.demo_accounts.values())

    assert len(holders) == 7
    assert len({user.id for user in holders}) == 7
    assert sum(1 for user in rows.users if user.demo_password) == 7
    for name in OLD_PICKS[spec.slug]:
        old = [user for user in rows.users if user.display_name == name]
        assert old, f"premise: {name} is still a generated person"
        assert all(user.demo_password is None for user in old), name


def test_one_request_reaches_all_three_demo_approvers(mcp_env: SeededMcp) -> None:
    """Catches a seed whose demo picks do not meet: a manager outside the
    employee's department, an owner of a system with no IT admin seat, or a
    filing that tells only some of the three.

    The Nordlicht demo employee files `prod-k8s.auditor` through the real tool.
    The seats are read from the stored request, and each demo account must hold
    exactly one unread `approval_needed` about it.
    """
    rows = generate_tenant(NORDLICHT)
    employee = rows.demo_accounts["employee"]
    manager = rows.demo_accounts["manager"]
    owner = rows.demo_accounts["system_owner"]
    it_admin = rows.demo_accounts["it_admin"]
    caller = SeededUser(
        tenant_id=rows.tenant_id,
        tenant_slug=NORDLICHT.slug,
        user_id=employee.id,
        email=employee.email,
        display_name=employee.display_name,
        department_id=rows.department_ids[employee.department],
        principals=tuple(sorted(employee.principals)),
        status="active",
    )

    result = call_tool(
        bearer(token_for(caller)),
        "create_access_request",
        {
            "target_user_id": str(employee.id),
            "role_id": str(rows.role_ids[("prod-k8s", "auditor")]),
            "justification": f"fake demo walkthrough {uuid.uuid4().hex}",
            "idempotency_key": f"fake-key-{uuid.uuid4().hex}",
        },
    )
    assert result.is_error is False, result.content
    assert result.structured_content is not None
    payload = result.structured_content
    assert payload["decision"] == "approval", "premise: the request needs approval"
    request_id = uuid.UUID(str(payload["request_id"]))

    seats = sql_rows(
        mcp_env.database_url,
        "SELECT approvals FROM access_requests WHERE id = :id",
        {"id": request_id},
    )[0][0]
    assert {(seat["slot"], seat["user_id"]) for seat in seats} == {
        ("department_manager", str(manager.id)),
        ("system_owner", str(owner.id)),
        ("it_admin", None),
    }

    notes = sql_rows(
        mcp_env.database_url,
        "SELECT user_id, kind, read_at FROM notifications"
        " WHERE subject_type = 'access_request' AND subject_id = :subject",
        {"subject": str(request_id)},
    )
    for person in (manager, owner, it_admin):
        mine = [note for note in notes if uuid.UUID(str(note.user_id)) == person.id]
        assert [(note.kind, note.read_at) for note in mine] == [("approval_needed", None)], (
            person.display_name
        )


def test_the_demo_doc_names_the_new_people() -> None:
    """Catches a doc that still lists the old manager and system owner (so
    visitors sign in as people who see nothing), and a doc with no walkthrough
    of the shared request.

    Each tenant's section is cut out on its own, so a walkthrough or login
    that appears only under the other tenant does not satisfy it.
    """
    text = DOC.read_text(encoding="utf-8")
    head, tail = text.split("## Cuan", 1)
    sections = {"nordlicht": head, "cuan": "## Cuan" + tail}
    logins = {
        "nordlicht": [
            ("cornelia.zorbach@nordlicht.example", "Demo-nordlicht-manager-2026!"),
            ("friedhold.trost@nordlicht.example", "Demo-nordlicht-system_owner-2026!"),
        ],
        "cuan": [
            ("freya.mcpolin@cuan.example", "Demo-cuan-manager-2026!"),
            ("marco.prial@cuan.example", "Demo-cuan-system_owner-2026!"),
        ],
    }
    for tenant, pairs in logins.items():
        for email, password in pairs:
            row = [line for line in sections[tenant].splitlines() if email in line]
            assert len(row) == 1, email
            assert f"`{password}`" in row[0], email
    assert "Production Cluster auditor" in sections["nordlicht"]
    assert "Production AWS auditor" in sections["cuan"]
