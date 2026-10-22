"""Deterministic, repeatable seed for both demo tenants.

Static data becomes rows with uuid5 ids from natural keys, Faker names from a
fixed seed and fixed timestamps, so every run makes the same rows. Rows are
inserted only when absent, so a re-run changes nothing. One exception:
`systems.category` and `systems.about` are set again on existing rows, since
older rows hold the migrations' defaults. The database must already be migrated.

Planted for the demos: stale grants, roles outside a department template,
owners holding their system's top role, and users holding both halves of a
segregation-of-duties pair.
"""

import asyncio
import hashlib
import unicodedata
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from faker import Faker
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from helpdesk.shared.auth.passwords import hash_password
from helpdesk.shared.core.config import load_settings
from helpdesk.shared.core.db import (
    bound_tenant_id,
    create_engine,
    create_session_factory,
    tenant_session,
)
from helpdesk.shared.crud import Repository
from helpdesk.shared.models import (
    Department,
    Grant,
    Role,
    RoleTemplate,
    SodRule,
    System,
    Tenant,
    TenantScoped,
    Ticket,
    User,
)
from helpdesk.shared.models.numbers import NumberKind, ReferenceCounter


class SeedError(Exception):
    """The seed refused to run; the message says why."""


# One namespace for every seeded id. Changing it (or any natural key below)
# changes every id and breaks idempotency against existing data — don't.
_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "seed.helpdesk.example")
_FAKER_SEED = 20260903

# All seed-controlled timestamps are fixed so reruns produce identical rows.
_GRANTED_AT = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
_STALE_EXPIRES_AT = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)  # past: stale-but-active
_HIRING_EPOCH = date(2018, 1, 1)

_PRIVILEGE_EXPIRY_DAYS = {"read": 365, "write": 180, "admin": 90}
_PRIVILEGE_RANK = {"read": 0, "write": 1, "admin": 2}

# Non-demo accounts share one placeholder credential (hashed once — argon2 at
# ~400 users would dominate the runtime) and demo salts are derived from the
# account key: both keep hashes byte-identical across runs.
_FILLER_PASSWORD = "This-Is-Seed-Filler-Not-A-Login-1!"


def _uid(key: str) -> uuid.UUID:
    return uuid.uuid5(_NAMESPACE, key)


def _salt_for(key: str) -> bytes:
    return hashlib.blake2b(key.encode(), digest_size=16).digest()


# --- static spec data ----------------------------------------------------------


@dataclass(frozen=True)
class RoleSpec:
    system: str
    slug: str
    privilege: str  # read | write | admin


@dataclass(frozen=True)
class SodRuleSpec:
    role_a: tuple[str, str]  # (system slug, role slug)
    role_b: tuple[str, str]
    rationale: str
    policy_ref: str


@dataclass(frozen=True)
class PlantedConflict:
    """One user deliberately granted role(s) completing an SoD pair."""

    department: str
    user_index: int  # index within the department's users
    roles: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class TicketSpec:
    department: str
    user_index: int
    kind: str
    title: str
    body: str


@dataclass(frozen=True)
class TenantSpec:
    key: str  # short handle used in natural keys and demo passwords
    slug: str
    name: str
    residency: str
    locale: str
    email_domain: str
    departments: tuple[str, ...]
    users_per_department: tuple[int, ...]
    systems: tuple[tuple[str, str, str, str], ...]  # (slug, name, sensitivity, category)
    roles: tuple[RoleSpec, ...]
    # System slug to comma-separated phrases saying what it is for.
    system_about: dict[str, str] = field(default_factory=dict)
    templates: dict[str, tuple[tuple[str, str], ...]] = field(default_factory=dict)
    sod_rules: tuple[SodRuleSpec, ...] = ()
    planted_conflicts: tuple[PlantedConflict, ...] = ()
    tickets: tuple[TicketSpec, ...] = ()
    it_department: str = "IT"
    hr_department: str = "HR"
    auditor_department: str = "Finance"


_COMMON_STANDARD = (("vpn", "user"), ("m365", "user"))

NORDLICHT = TenantSpec(
    key="nordlicht",
    slug="nordlicht",
    name="Nordlicht Maschinenbau GmbH",
    residency="eu",
    locale="de_DE",
    email_domain="nordlicht.example",
    departments=("Engineering", "Production", "Procurement", "Finance", "Sales", "HR", "IT"),
    users_per_department=(46, 72, 18, 16, 28, 14, 26),
    systems=(
        ("erp", "ERP", "critical", "other"),
        ("plm", "Product Lifecycle / CAD Vault", "high", "other"),
        ("mes", "Manufacturing Execution", "high", "other"),
        ("finance-dash", "Finance Dashboard", "high", "other"),
        ("payments", "Payment Release", "critical", "other"),
        ("vendor-master", "Supplier Master Data", "high", "other"),
        ("crm", "CRM", "medium", "other"),
        ("hris", "HR System", "critical", "hris"),
        ("wiki", "Internal Wiki", "low", "other"),
        ("vpn", "VPN", "medium", "other"),
        ("gitlab", "GitLab", "medium", "other"),
        ("prod-k8s", "Production Cluster", "critical", "prod_infra"),
        ("grafana", "Grafana", "low", "other"),
        ("m365", "Mail and Files", "medium", "other"),
        ("badge", "Physical Access", "high", "other"),
    ),
    roles=(
        RoleSpec("erp", "viewer", "read"),
        RoleSpec("erp", "buyer", "write"),
        RoleSpec("erp", "goods-receipt", "write"),
        RoleSpec("erp", "admin", "admin"),
        RoleSpec("plm", "viewer", "read"),
        RoleSpec("plm", "engineer", "write"),
        RoleSpec("mes", "viewer", "read"),
        RoleSpec("mes", "operator", "write"),
        RoleSpec("finance-dash", "viewer", "read"),
        RoleSpec("payments", "initiator", "write"),
        RoleSpec("payments", "approver", "write"),
        RoleSpec("vendor-master", "viewer", "read"),
        RoleSpec("vendor-master", "editor", "write"),
        RoleSpec("crm", "viewer", "read"),
        RoleSpec("crm", "editor", "write"),
        RoleSpec("hris", "viewer", "read"),
        RoleSpec("hris", "payroll-approver", "write"),
        RoleSpec("hris", "admin", "admin"),
        RoleSpec("wiki", "viewer", "read"),
        RoleSpec("wiki", "editor", "write"),
        RoleSpec("vpn", "user", "read"),
        RoleSpec("gitlab", "developer", "write"),
        RoleSpec("gitlab", "maintainer", "admin"),
        RoleSpec("prod-k8s", "auditor", "read"),
        RoleSpec("prod-k8s", "deployer", "write"),
        RoleSpec("grafana", "viewer", "read"),
        RoleSpec("m365", "user", "write"),
        RoleSpec("badge", "holder", "read"),
    ),
    system_about={
        "erp": "orders, purchasing, invoices, stock, enterprise resource planning",
        "plm": "CAD drawings, product designs, engineering parts, bill of materials",
        "mes": "shop floor, production orders, machine data, manufacturing",
        "finance-dash": "finance reports, budgets, revenue figures, financial dashboard",
        "payments": "pay suppliers, release payments, bank transfers",
        "vendor-master": "suppliers, vendors, supplier records, vendor data",
        "crm": "customers, sales leads, customer contacts, sales pipeline",
        "hris": "employee records, payroll, salaries, human resources",
        "wiki": "documentation, docs, team pages, how-to guides, knowledge base",
        "vpn": "remote access, work from home, network connection",
        "gitlab": "source code, git repositories, merge requests, code review",
        "prod-k8s": "production servers, deployments, kubernetes, containers",
        "grafana": "monitoring, metrics, dashboards, charts, alerts",
        "m365": "email, outlook, file sharing, office documents, calendar",
        "badge": "building access, door badge, office entry, key card",
    },
    templates={
        "Engineering": (
            ("plm", "engineer"),
            ("gitlab", "developer"),
            ("grafana", "viewer"),
            *_COMMON_STANDARD,
            ("wiki", "editor"),
            ("badge", "holder"),
        ),
        "Production": (
            ("mes", "operator"),
            ("erp", "viewer"),
            *_COMMON_STANDARD,
            ("wiki", "viewer"),
            ("badge", "holder"),
        ),
        "Procurement": (
            ("erp", "buyer"),
            ("vendor-master", "editor"),
            ("crm", "viewer"),
            *_COMMON_STANDARD,
            ("wiki", "editor"),
            ("badge", "holder"),
        ),
        "Finance": (
            ("finance-dash", "viewer"),
            ("erp", "viewer"),
            ("payments", "initiator"),
            *_COMMON_STANDARD,
            ("wiki", "viewer"),
            ("badge", "holder"),
        ),
        "Sales": (
            ("crm", "editor"),
            ("erp", "viewer"),
            *_COMMON_STANDARD,
            ("wiki", "viewer"),
            ("badge", "holder"),
        ),
        "HR": (
            ("hris", "viewer"),
            *_COMMON_STANDARD,
            ("wiki", "viewer"),
            ("badge", "holder"),
        ),
        "IT": (
            ("gitlab", "developer"),
            ("grafana", "viewer"),
            *_COMMON_STANDARD,
            ("wiki", "editor"),
            ("badge", "holder"),
        ),
    },
    sod_rules=(
        SodRuleSpec(
            ("payments", "initiator"),
            ("payments", "approver"),
            "Initiating and approving the same payment must be two people.",
            "SoD-STD §2.1",
        ),
        SodRuleSpec(
            ("vendor-master", "editor"),
            ("payments", "approver"),
            "Editing supplier bank data and approving payments enables invoice fraud.",
            "SoD-STD §2.2",
        ),
        SodRuleSpec(
            ("erp", "buyer"),
            ("erp", "goods-receipt"),
            "Ordering and confirming receipt of the same goods hides phantom purchases.",
            "SoD-STD §2.3",
        ),
        SodRuleSpec(
            ("prod-k8s", "deployer"),
            ("prod-k8s", "auditor"),
            "Deploying to production and auditing those deployments must be separated.",
            "SoD-STD §2.4",
        ),
        SodRuleSpec(
            ("hris", "admin"),
            ("hris", "payroll-approver"),
            "Administering HR records and approving payroll enables ghost employees.",
            "SoD-STD §2.5",
        ),
        SodRuleSpec(
            ("erp", "admin"),
            ("payments", "initiator"),
            "ERP administrators can alter controls and must not initiate payments.",
            "SoD-STD §2.6",
        ),
    ),
    planted_conflicts=(
        PlantedConflict("Finance", 3, (("payments", "approver"),)),
        PlantedConflict("Procurement", 4, (("payments", "approver"),)),
        PlantedConflict("Procurement", 5, (("erp", "goods-receipt"),)),
        PlantedConflict("Engineering", 6, (("prod-k8s", "deployer"), ("prod-k8s", "auditor"))),
    ),
    tickets=(
        TicketSpec(
            "Production",
            3,
            "password_reset",
            "Cannot log in to MES",
            "Forgot my MES password after the summer break, please reset it.",
        ),
        TicketSpec(
            "Sales",
            2,
            "unlock",
            "CRM account locked",
            "Three wrong passwords and now my CRM account is locked.",
        ),
        TicketSpec(
            "Engineering",
            5,
            "general",
            "Grafana dashboard missing",
            "The line-3 throughput dashboard disappeared from my Grafana home.",
        ),
    ),
)

CUAN = TenantSpec(
    key="cuan",
    slug="cuan",
    name="Cuan Financial Services Ltd",
    residency="eu",
    locale="en_IE",
    email_domain="cuan.example",
    departments=(
        "Engineering",
        "Operations",
        "Risk & Compliance",
        "Finance",
        "Customer Support",
        "HR",
        "IT",
    ),
    users_per_department=(40, 45, 20, 15, 32, 10, 18),
    systems=(
        ("core-banking", "Core Banking", "critical", "other"),
        ("payments-gateway", "Payments Gateway", "critical", "other"),
        ("kyc", "KYC", "critical", "other"),
        ("ledger", "Ledger", "high", "other"),
        ("crm", "CRM", "medium", "other"),
        ("support-desk", "Support Desk", "medium", "other"),
        ("data-warehouse", "Data Warehouse", "high", "other"),
        ("hris", "HR System", "critical", "hris"),
        ("vpn", "VPN", "medium", "other"),
        ("github", "GitHub", "medium", "other"),
        ("prod-aws", "Production AWS", "critical", "prod_infra"),
        ("datadog", "Datadog", "low", "other"),
        ("m365", "Mail and Files", "medium", "other"),
        ("wiki", "Internal Wiki", "low", "other"),
    ),
    roles=(
        RoleSpec("core-banking", "viewer", "read"),
        RoleSpec("core-banking", "operator", "write"),
        RoleSpec("core-banking", "admin", "admin"),
        RoleSpec("payments-gateway", "initiator", "write"),
        RoleSpec("payments-gateway", "approver", "write"),
        RoleSpec("kyc", "analyst", "write"),
        RoleSpec("kyc", "approver", "write"),
        RoleSpec("ledger", "viewer", "read"),
        RoleSpec("ledger", "editor", "write"),
        RoleSpec("crm", "viewer", "read"),
        RoleSpec("crm", "editor", "write"),
        RoleSpec("support-desk", "agent", "write"),
        RoleSpec("support-desk", "supervisor", "admin"),
        RoleSpec("data-warehouse", "analyst", "read"),
        RoleSpec("data-warehouse", "engineer", "write"),
        RoleSpec("hris", "viewer", "read"),
        RoleSpec("hris", "payroll-approver", "write"),
        RoleSpec("hris", "admin", "admin"),
        RoleSpec("vpn", "user", "read"),
        RoleSpec("github", "developer", "write"),
        RoleSpec("github", "maintainer", "admin"),
        RoleSpec("prod-aws", "auditor", "read"),
        RoleSpec("prod-aws", "deployer", "write"),
        RoleSpec("datadog", "viewer", "read"),
        RoleSpec("m365", "user", "write"),
        RoleSpec("wiki", "viewer", "read"),
        RoleSpec("wiki", "editor", "write"),
    ),
    system_about={
        "core-banking": "bank accounts, customer balances, loans, deposits",
        "payments-gateway": "card payments, money transfers, payment processing",
        "kyc": "know your customer, identity checks, customer verification, compliance",
        "ledger": "general ledger, accounting, bookkeeping, journal entries",
        "crm": "customers, sales leads, customer contacts, sales pipeline",
        "support-desk": "support tickets, customer support, help desk, service requests",
        "data-warehouse": "analytics, reports, data queries, business intelligence, SQL",
        "hris": "employee records, payroll, salaries, human resources",
        "vpn": "remote access, work from home, network connection",
        "github": "source code, git repositories, pull requests, code review",
        "prod-aws": "production servers, cloud infrastructure, deployments, amazon web services",
        "datadog": "monitoring, metrics, dashboards, logs, alerts",
        "m365": "email, outlook, file sharing, office documents, calendar",
        "wiki": "documentation, docs, team pages, how-to guides, knowledge base",
    },
    templates={
        "Engineering": (
            ("github", "developer"),
            ("datadog", "viewer"),
            ("data-warehouse", "engineer"),
            *_COMMON_STANDARD,
            ("wiki", "editor"),
        ),
        "Operations": (
            ("core-banking", "operator"),
            ("ledger", "viewer"),
            *_COMMON_STANDARD,
            ("wiki", "viewer"),
        ),
        "Risk & Compliance": (
            ("kyc", "analyst"),
            ("data-warehouse", "analyst"),
            ("ledger", "viewer"),
            *_COMMON_STANDARD,
            ("wiki", "viewer"),
        ),
        "Finance": (
            ("ledger", "editor"),
            ("data-warehouse", "analyst"),
            *_COMMON_STANDARD,
            ("wiki", "viewer"),
        ),
        "Customer Support": (
            ("support-desk", "agent"),
            ("crm", "viewer"),
            *_COMMON_STANDARD,
            ("wiki", "viewer"),
        ),
        "HR": (
            ("hris", "viewer"),
            *_COMMON_STANDARD,
            ("wiki", "viewer"),
        ),
        "IT": (
            ("github", "developer"),
            ("datadog", "viewer"),
            *_COMMON_STANDARD,
            ("wiki", "editor"),
        ),
    },
    sod_rules=(
        SodRuleSpec(
            ("kyc", "analyst"),
            ("kyc", "approver"),
            "The analyst who prepares a KYC case must not approve it.",
            "SoD-STD §2.1",
        ),
        SodRuleSpec(
            ("payments-gateway", "initiator"),
            ("payments-gateway", "approver"),
            "Initiating and approving the same payment must be two people.",
            "SoD-STD §2.2",
        ),
        SodRuleSpec(
            ("hris", "admin"),
            ("hris", "payroll-approver"),
            "Administering HR records and approving payroll enables ghost employees.",
            "SoD-STD §2.3",
        ),
        SodRuleSpec(
            ("prod-aws", "deployer"),
            ("prod-aws", "auditor"),
            "Deploying to production and auditing those deployments must be separated.",
            "SoD-STD §2.4",
        ),
        SodRuleSpec(
            ("core-banking", "admin"),
            ("payments-gateway", "approver"),
            "Core banking administrators can alter controls and must not approve payments.",
            "SoD-STD §2.5",
        ),
        SodRuleSpec(
            ("ledger", "editor"),
            ("payments-gateway", "initiator"),
            "Editing the ledger and initiating payments hides misdirected funds.",
            "SoD-STD §2.6",
        ),
    ),
    planted_conflicts=(
        PlantedConflict("Risk & Compliance", 3, (("kyc", "approver"),)),
        PlantedConflict(
            "Operations", 4, (("payments-gateway", "initiator"), ("payments-gateway", "approver"))
        ),
        PlantedConflict("Finance", 5, (("payments-gateway", "initiator"),)),
        PlantedConflict("HR", 2, (("hris", "admin"), ("hris", "payroll-approver"))),
    ),
    tickets=(
        TicketSpec(
            "Customer Support",
            3,
            "password_reset",
            "Support desk password expired",
            "My support desk password expired while I was on leave, please reset it.",
        ),
        TicketSpec(
            "Operations",
            2,
            "unlock",
            "Core banking account locked",
            "Locked out of core banking after mistyping my password this morning.",
        ),
        TicketSpec(
            "Risk & Compliance",
            5,
            "general",
            "Data warehouse query timeout",
            "My monthly exposure query in the data warehouse times out since Monday.",
        ),
    ),
    auditor_department="Risk & Compliance",
)

TENANTS = (NORDLICHT, CUAN)

PRINCIPALS = ("employee", "manager", "system_owner", "hr", "it_admin", "auditor", "platform_admin")

_STALE_EVERY = 20  # every Nth user's first standard grant is expired-but-active
_NONSTANDARD_EVERY = 15  # every Nth user holds one role outside their template


# --- generated rows ----------------------------------------------------------


@dataclass
class UserRow:
    id: uuid.UUID
    department: str
    department_index: int  # index within the department
    tenant_index: int  # index across the whole tenant
    email: str
    display_name: str
    principals: set[str] = field(default_factory=lambda: {"employee"})
    demo_password: str | None = None  # set for demo accounts only


@dataclass
class GrantRow:
    id: uuid.UUID
    user_id: uuid.UUID
    role: tuple[str, str]
    expires_at: datetime | None


@dataclass
class TenantRows:
    spec: TenantSpec
    tenant_id: uuid.UUID
    department_ids: dict[str, uuid.UUID]
    users: list[UserRow]
    users_by_department: dict[str, list[UserRow]]
    system_ids: dict[str, uuid.UUID]
    system_owners: dict[str, UserRow]  # system slug -> owner
    role_ids: dict[tuple[str, str], uuid.UUID]
    grants: list[GrantRow]
    demo_accounts: dict[str, UserRow]  # principal -> user


def _email_slug(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return "".join(ch for ch in ascii_name.lower() if ch.isalpha())


def _generate_users(spec: TenantSpec) -> tuple[list[UserRow], dict[str, list[UserRow]]]:
    fake = Faker(spec.locale)
    fake.seed_instance(f"{_FAKER_SEED}:{spec.slug}")
    users: list[UserRow] = []
    by_department: dict[str, list[UserRow]] = {}
    seen_emails: set[str] = set()
    tenant_index = 0
    for department, count in zip(spec.departments, spec.users_per_department, strict=True):
        rows: list[UserRow] = []
        for department_index in range(count):
            first, last = fake.first_name(), fake.last_name()
            base = f"{_email_slug(first)}.{_email_slug(last)}"
            email = f"{base}@{spec.email_domain}"
            suffix = 2
            while email in seen_emails:
                email = f"{base}{suffix}@{spec.email_domain}"
                suffix += 1
            seen_emails.add(email)
            rows.append(
                UserRow(
                    id=_uid(f"{spec.key}:user:{email}"),
                    department=department,
                    department_index=department_index,
                    tenant_index=tenant_index,
                    email=email,
                    display_name=f"{first} {last}",
                )
            )
            tenant_index += 1
        by_department[department] = rows
        users.extend(rows)
    return users, by_department


def _top_role(spec: TenantSpec, system: str) -> tuple[str, str]:
    """The most privileged role of a system (stable on spec order for ties)."""
    candidates = [r for r in spec.roles if r.system == system]
    best = max(candidates, key=lambda r: _PRIVILEGE_RANK[r.privilege])
    return (best.system, best.slug)


def _assign_principals(
    spec: TenantSpec, users_by_department: dict[str, list[UserRow]]
) -> tuple[dict[str, UserRow], dict[str, UserRow]]:
    """Managers, owners and the app-level principals; returns (owners, demo accounts)."""
    for department, rows in users_by_department.items():
        rows[0].principals.add("manager")
        del department

    it_rows = users_by_department[spec.it_department]
    owners: dict[str, UserRow] = {}
    for index, (system_slug, _name, _sensitivity, _category) in enumerate(spec.systems):
        owner = it_rows[1 + index]  # index 0 is the IT manager
        owner.principals.add("system_owner")
        owners[system_slug] = owner
    next_it = 1 + len(spec.systems)
    it_admins = (it_rows[next_it], it_rows[next_it + 1])
    platform_admin = it_rows[next_it + 2]
    for row in it_admins:
        row.principals.add("it_admin")
    platform_admin.principals.add("platform_admin")

    hr_officer = users_by_department[spec.hr_department][1]
    hr_officer.principals.add("hr")
    auditor = users_by_department[spec.auditor_department][1]
    auditor.principals.add("auditor")

    # The demo manager runs the demo employee's own department, and the demo
    # system owner owns the one prod_infra system, the only category that adds
    # the it_admin seat. So one request from the demo employee for that system
    # reaches the demo manager, owner and IT admin.
    employee = users_by_department[spec.departments[1]][5]
    prod_infra = next(slug for slug, _n, _s, category in spec.systems if category == "prod_infra")
    demo_accounts = {
        "employee": employee,
        "manager": users_by_department[employee.department][0],
        "system_owner": owners[prod_infra],
        "hr": hr_officer,
        "it_admin": it_admins[0],
        "auditor": auditor,
        "platform_admin": platform_admin,
    }
    for principal, row in demo_accounts.items():
        row.demo_password = f"Demo-{spec.key}-{principal}-2026!"
    return owners, demo_accounts


def _generate_grants(
    spec: TenantSpec,
    users: list[UserRow],
    users_by_department: dict[str, list[UserRow]],
    owners: dict[str, UserRow],
) -> list[GrantRow]:
    roles_in_spec_order = [(r.system, r.slug) for r in spec.roles]
    grants: dict[uuid.UUID, GrantRow] = {}

    def add(user: UserRow, role: tuple[str, str], expires_at: datetime | None = None) -> None:
        grant_id = _uid(f"{spec.key}:grant:{user.email}:{role[0]}.{role[1]}")
        # A planted/extra grant that duplicates a standard one collapses onto
        # the same deterministic id; first writer wins, like the DB upsert.
        grants.setdefault(
            grant_id, GrantRow(id=grant_id, user_id=user.id, role=role, expires_at=expires_at)
        )

    for user in users:
        template = spec.templates[user.department]
        for position, role in enumerate(template):
            stale = position == 0 and user.tenant_index % _STALE_EVERY == 7
            add(user, role, _STALE_EXPIRES_AT if stale else None)
        if user.tenant_index % _NONSTANDARD_EVERY == 4:
            for offset in range(len(roles_in_spec_order)):
                candidate = roles_in_spec_order[
                    (user.tenant_index * 7 + offset) % len(roles_in_spec_order)
                ]
                if candidate not in template:
                    add(user, candidate)
                    break

    for system_slug, owner in owners.items():
        add(owner, _top_role(spec, system_slug))

    for planted in spec.planted_conflicts:
        victim = users_by_department[planted.department][planted.user_index]
        for role in planted.roles:
            add(victim, role)

    return list(grants.values())


def generate_tenant(spec: TenantSpec) -> TenantRows:
    users, users_by_department = _generate_users(spec)
    owners, demo_accounts = _assign_principals(spec, users_by_department)
    return TenantRows(
        spec=spec,
        tenant_id=_uid(f"tenant:{spec.slug}"),
        department_ids={d: _uid(f"{spec.key}:department:{d}") for d in spec.departments},
        users=users,
        users_by_department=users_by_department,
        system_ids={s: _uid(f"{spec.key}:system:{s}") for s, _, _, _ in spec.systems},
        system_owners=owners,
        role_ids={
            (r.system, r.slug): _uid(f"{spec.key}:role:{r.system}.{r.slug}") for r in spec.roles
        },
        grants=_generate_grants(spec, users, users_by_department, owners),
        demo_accounts=demo_accounts,
    )


# --- writer ------------------------------------------------------------------


class _SeedRepository[M: TenantScoped](Repository[M]):
    """Insert only absent rows, by fixed id, and never update one. The core
    `Repository.create` refuses caller-set ids; the seed is the one writer
    whose ids are fixed on purpose."""

    async def upsert_many(
        self, rows: Mapping[uuid.UUID, Mapping[str, object]], /
    ) -> dict[uuid.UUID, M]:
        """Insert every absent row of one table in one batch: one read, one
        flush. Returns every row by id, existing ones untouched."""
        if not rows:
            return {}
        found = await self.get_many(rows.keys())
        tenant_id = bound_tenant_id(self._session)
        added = {
            id: self._model(id=id, tenant_id=tenant_id, **fields)
            for id, fields in rows.items()
            if id not in found
        }
        if added:
            self._session.add_all(added.values())
            await self._session.flush()
        return found | added


def _password_hashes(rows: TenantRows) -> dict[uuid.UUID, str]:
    filler = hash_password(_FILLER_PASSWORD, salt=_salt_for(f"{rows.spec.key}:filler"))
    hashes = {user.id: filler for user in rows.users}
    for user in rows.demo_accounts.values():
        assert user.demo_password is not None
        hashes[user.id] = hash_password(
            user.demo_password, salt=_salt_for(f"{rows.spec.key}:{user.email}")
        )
    return hashes


async def _write_tenant(session: AsyncSession, rows: TenantRows) -> None:
    spec = rows.spec
    departments = _SeedRepository(session, Department)
    users = _SeedRepository(session, User)
    systems = _SeedRepository(session, System)
    roles = _SeedRepository(session, Role)
    templates = _SeedRepository(session, RoleTemplate)
    sod_rules = _SeedRepository(session, SodRule)
    grants = _SeedRepository(session, Grant)
    tickets = _SeedRepository(session, Ticket)

    await departments.upsert_many(
        {
            rows.department_ids[department]: {
                "name": department,
                "manager_user_id": rows.users_by_department[department][0].id,
            }
            for department in spec.departments
        }
    )

    hashes = _password_hashes(rows)
    await users.upsert_many(
        {
            user.id: {
                "department_id": rows.department_ids[user.department],
                "email": user.email,
                "display_name": user.display_name,
                "password_hash": hashes[user.id],
                "principals": sorted(user.principals),
                "status": "active",
                "start_date": _HIRING_EPOCH + timedelta(days=(user.tenant_index * 97) % 2900),
                "end_date": None,
            }
            for user in rows.users
        }
    )

    written_systems = await systems.upsert_many(
        {
            rows.system_ids[slug]: {
                "slug": slug,
                "name": name,
                "sensitivity": sensitivity,
                "category": category,
                "about": spec.system_about[slug],
                "owner_user_id": rows.system_owners[slug].id,
            }
            for slug, name, sensitivity, category in spec.systems
        }
    )
    for slug, _name, _sensitivity, category in spec.systems:
        # Backfill for rows seeded before the category column existed (plan
        # 11a): the migration server-defaults `other` and must not name
        # system slugs, so the seed is the only place real categories can
        # come from. Idempotent — a second run finds the value equal.
        system = written_systems[rows.system_ids[slug]]
        if system.category != category:
            system.category = category
        # The same for `about`: the migration defaults it empty.
        about = spec.system_about[slug]
        if system.about != about:
            system.about = about

    await roles.upsert_many(
        {
            rows.role_ids[(role.system, role.slug)]: {
                "system_id": rows.system_ids[role.system],
                "slug": role.slug,
                "name": f"{role.system}.{role.slug}",
                "privilege": role.privilege,
                "default_expiry_days": _PRIVILEGE_EXPIRY_DAYS[role.privilege],
            }
            for role in spec.roles
        }
    )

    await templates.upsert_many(
        {
            _uid(f"{spec.key}:template:{department}:{role_key[0]}.{role_key[1]}"): {
                "department_id": rows.department_ids[department],
                "role_id": rows.role_ids[role_key],
            }
            for department, template in spec.templates.items()
            for role_key in template
        }
    )

    await sod_rules.upsert_many(
        {
            _uid(
                f"{spec.key}:sod:{rule.role_a[0]}.{rule.role_a[1]}:{rule.role_b[0]}.{rule.role_b[1]}"
            ): {
                "role_a_id": rows.role_ids[rule.role_a],
                "role_b_id": rows.role_ids[rule.role_b],
                "rationale": rule.rationale,
                "policy_ref": rule.policy_ref,
            }
            for rule in spec.sod_rules
        }
    )

    managers = {d: users_rows[0].id for d, users_rows in rows.users_by_department.items()}
    department_of = {user.id: user.department for user in rows.users}
    await grants.upsert_many(
        {
            grant.id: {
                "user_id": grant.user_id,
                "role_id": rows.role_ids[grant.role],
                "request_id": None,
                "granted_by_user_id": managers[department_of[grant.user_id]],
                "granted_at": _GRANTED_AT,
                "expires_at": grant.expires_at,
                "revoked_at": None,
                "revoke_reason": None,
                "status": "active",
            }
            for grant in rows.grants
        }
    )

    reporters = [
        (ticket, rows.users_by_department[ticket.department][ticket.user_index])
        for ticket in spec.tickets
    ]
    await tickets.upsert_many(
        {
            _uid(f"{spec.key}:ticket:{reporter.email}:{ticket.kind}:{ticket.title}"): {
                "user_id": reporter.id,
                "kind": ticket.kind,
                "title": ticket.title,
                "body": ticket.body,
                "status": "open",
                # Fixed by position, so a rerun gives every ticket the same number.
                "number": number,
            }
            for number, (ticket, reporter) in enumerate(reporters, start=1)
        }
    )
    # The counter starts where the fixed numbers end. A rerun leaves it alone,
    # so tickets filed since keep their numbers. A counter the numbers
    # migration made has a random id, so it is found by kind, not by id.
    counters = _SeedRepository(session, ReferenceCounter)
    has_counter = any(c.kind == NumberKind.TICKET.value for c in await counters.list())
    if reporters and not has_counter:
        await counters.upsert_many(
            {
                _uid(f"{spec.key}:counter:{NumberKind.TICKET.value}"): {
                    "kind": NumberKind.TICKET.value,
                    "last": len(reporters),
                }
            }
        )


async def _check_migrated(database_url: str) -> None:
    engine = create_engine(database_url)
    try:
        async with engine.connect() as conn:
            tables = set(await conn.run_sync(lambda c: inspect(c).get_table_names()))
    finally:
        await engine.dispose()
    required = {"tenants", "departments", "users", "grants"}
    if not required <= tables:
        raise SeedError(
            f"database at {database_url.rsplit('@', 1)[-1]} is missing the helpdesk schema "
            f"(tables absent: {sorted(required - tables)}); run the migrations first: "
            "uv run alembic upgrade head"
        )


async def _write_tenants_table(
    factory: async_sessionmaker[AsyncSession], all_rows: list[TenantRows]
) -> None:
    async with factory() as session:
        for rows in all_rows:
            if await session.get(Tenant, rows.tenant_id) is None:
                session.add(
                    Tenant(
                        id=rows.tenant_id,
                        slug=rows.spec.slug,
                        name=rows.spec.name,
                        residency=rows.spec.residency,
                        settings={},
                    )
                )
        await session.commit()


async def seed_all(database_url: str) -> None:
    """Seed both tenants; deterministic, idempotent, one transaction per tenant."""
    await _check_migrated(database_url)
    all_rows = [generate_tenant(spec) for spec in TENANTS]
    engine = create_engine(database_url)
    try:
        factory = create_session_factory(engine)
        await _write_tenants_table(factory, all_rows)
        for rows in all_rows:
            async with tenant_session(factory, rows.tenant_id) as session:
                await _write_tenant(session, rows)
    finally:
        await engine.dispose()


def main() -> None:
    database_url = load_settings().database_url
    asyncio.run(seed_all(database_url))
    total_users = sum(
        spec.users_per_department[i] for spec in TENANTS for i in range(len(spec.departments))
    )
    print(f"seeded {len(TENANTS)} tenants, {total_users} users (idempotent; safe to re-run)")


if __name__ == "__main__":
    main()
