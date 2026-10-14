"""The nine directory-core tables (the design, an earlier change scope decision A).

Column types stay plain (String for enum-valued fields); allowed values are
enforced at the Pydantic boundaries, not by the database. Soft references
whose target table lands in a later plan row (`grants.request_id`,
`tickets.session_id`) are plain nullable UUIDs, no FK yet.
`departments.manager_user_id` is also FK-less: users.department_id already
points the other way and a circular FK pair isn't worth it for a display
field.
"""

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import Date, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from helpdesk.shared.models.base import Base, TenantScoped


class Tenant(Base):
    __tablename__ = "tenants"

    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(256))
    residency: Mapped[str] = mapped_column(String(16))  # eu | any
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class Department(TenantScoped):
    __tablename__ = "departments"
    __table_args__ = (Index("ix_departments_tenant_id_name", "tenant_id", "name", unique=True),)

    name: Mapped[str] = mapped_column(String(256))
    manager_user_id: Mapped[uuid.UUID | None] = mapped_column()


class User(TenantScoped):
    __tablename__ = "users"
    __table_args__ = (Index("ix_users_tenant_id_email", "tenant_id", "email", unique=True),)

    department_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("departments.id"))
    email: Mapped[str] = mapped_column(String(320))
    display_name: Mapped[str] = mapped_column(String(256))
    password_hash: Mapped[str] = mapped_column(String(256))
    principals: Mapped[list[str]] = mapped_column(ARRAY(String(64)))
    status: Mapped[str] = mapped_column(String(16))  # active | leaver
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    # the design's simulated self-service "flags": the
    # reset/unlock tools stamp these; no real directory action happens.
    password_reset_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    account_unlocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class System(TenantScoped):
    __tablename__ = "systems"
    __table_args__ = (Index("ix_systems_tenant_id_slug", "tenant_id", "slug", unique=True),)

    slug: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(256))
    sensitivity: Mapped[str] = mapped_column(String(16))  # low | medium | high | critical
    # prod_infra | hris | other (policy SystemCategory, the design). The
    # server_default means a migrated-but-not-reseeded DB reads `other`,
    # pinned by `test_access_snapshot.py::
    # test_a_system_created_without_a_category_reads_other`; the seed
    # backfills real values on re-run.
    category: Mapped[str] = mapped_column(String(16), server_default="other")
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))


class Role(TenantScoped):
    __tablename__ = "roles"
    __table_args__ = (
        Index("ix_roles_tenant_id_system_id_slug", "tenant_id", "system_id", "slug", unique=True),
    )

    system_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("systems.id"))
    slug: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(256))
    privilege: Mapped[str] = mapped_column(String(16))  # read | write | admin
    default_expiry_days: Mapped[int] = mapped_column()


class RoleTemplate(TenantScoped):
    __tablename__ = "role_templates"
    __table_args__ = (
        Index(
            "ix_role_templates_tenant_id_department_id_role_id",
            "tenant_id",
            "department_id",
            "role_id",
            unique=True,
        ),
    )

    department_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("departments.id"))
    role_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("roles.id"))


class SodRule(TenantScoped):
    __tablename__ = "sod_rules"
    __table_args__ = (
        Index(
            "ix_sod_rules_tenant_id_role_a_id_role_b_id",
            "tenant_id",
            "role_a_id",
            "role_b_id",
            unique=True,
        ),
    )

    role_a_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("roles.id"))
    role_b_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("roles.id"))
    rationale: Mapped[str] = mapped_column(Text)
    policy_ref: Mapped[str] = mapped_column(String(128))


class Grant(TenantScoped):
    __tablename__ = "grants"
    __table_args__ = (Index("ix_grants_tenant_id_user_id", "tenant_id", "user_id"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    role_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("roles.id"))
    request_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("access_requests.id"))
    granted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoke_reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16))  # active | expired | revoked


class Ticket(TenantScoped):
    __tablename__ = "tickets"
    __table_args__ = (Index("ix_tickets_tenant_id_user_id", "tenant_id", "user_id"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    kind: Mapped[str] = mapped_column(String(32))  # password_reset | unlock | general | escalation
    title: Mapped[str] = mapped_column(String(256))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16))
    # FK target (sessions) lands in a later plan row.
    session_id: Mapped[uuid.UUID | None] = mapped_column()
