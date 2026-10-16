# Demo accounts

Seeded by `uv run python scripts/seed.py` (deterministic; safe to re-run).
One login per app principal per tenant. All data is invented; the email
domains are reserved `.example` addresses on purpose.

These credentials are for local demos only. Before any public deploy they
are rotated and moved to secrets; they never guard real data.

Every other seeded user shares one filler password that is not documented
anywhere and is not a login path for demos.

## Nordlicht Maschinenbau GmbH (`nordlicht`)

| Principal | Email | Password |
|---|---|---|
| employee | vincenzo.gude@nordlicht.example | `Demo-nordlicht-employee-2026!` |
| manager | adriane.karge@nordlicht.example | `Demo-nordlicht-manager-2026!` |
| system_owner | zdravko.wagenknecht@nordlicht.example | `Demo-nordlicht-system_owner-2026!` |
| hr | konrad.kraushaar@nordlicht.example | `Demo-nordlicht-hr-2026!` |
| it_admin | bernt.dietz@nordlicht.example | `Demo-nordlicht-it_admin-2026!` |
| auditor | gaetano.losekann@nordlicht.example | `Demo-nordlicht-auditor-2026!` |
| platform_admin | sylvio.lindner@nordlicht.example | `Demo-nordlicht-platform_admin-2026!` |

### What to try at Nordlicht

**employee** (Vincenzo Gude, Production). Already holds everything the
Production department gets by default: `mes.operator`, `erp.viewer`,
`vpn.user`, `m365.user`, `wiki.viewer`, `badge.holder`. So every new ask
needs an approval.

| Ask for | What happens |
|---|---|
| Grafana viewer, Wiki editor, CRM viewer, GitLab developer | Waits for the Production manager |
| PLM viewer, Finance Dashboard viewer, Supplier Master Data viewer, MES viewer | Waits for the Production manager, then the system owner |
| ERP buyer, Payment Release initiator | Critical system: manager, then system owner |
| HR System viewer | Critical HR system: manager, system owner, then HR |
| Production Cluster deployer | Critical prod system: manager, system owner, then IT admin |
| GitLab maintainer for yourself | Admin on yourself is pushed up one level: manager, then system owner |

The Production manager is not one of the logins above, so these requests
stay pending in this demo. To see a request approved from start to end,
use the manager login below.

You can also reset your password, unlock your account, ask policy questions
("how long does VPN access last?") and open a ticket.

**manager** (Adriane Karge, Engineering manager). Sees and approves requests
from Engineering staff, asks for access on their behalf, runs access reviews
and reads the audit trail for Engineering. Try this full approval flow: ask
for `erp.viewer` for Anastasia Geisel. ERP is critical, and Adriane cannot
approve her own request, so it goes to Zdravko (ERP owner) and then Bernt
(IT admin). Log in as each to approve it.

**system_owner** (Zdravko Wagenknecht, owns ERP). Approves, refuses and
revokes ERP access, and runs ERP access reviews. Holds `erp.admin`, so asking
for Payment Release initiator for himself is blocked by a separation of
duties rule.

**hr** (Konrad Kraushaar). Onboards, moves and offboards people, asks for
access for anyone, and fills the HR seat on critical HR System requests.

**it_admin** (Bernt Dietz). Onboards and moves people, approves the IT admin
seat on critical requests, rolls back a granted request, and reads the
separation of duties rules and policy documents.

**auditor** (Gaetano Losekann, Finance). Reads the full audit trail. As a
Finance employee he holds `payments.initiator`, so asking for Payment
Release approver for himself is blocked by a separation of duties rule.

**platform_admin** (Sylvio Lindner). Everything the IT admin can do, plus
offboarding and the kill switches that stop the agent from granting.

## Cuan Financial Services Ltd (`cuan`)

| Principal | Email | Password |
|---|---|---|
| employee | elisha.devereauxdeaueroux@cuan.example | `Demo-cuan-employee-2026!` |
| manager | faith.steed@cuan.example | `Demo-cuan-manager-2026!` |
| system_owner | padraigin.fee@cuan.example | `Demo-cuan-system_owner-2026!` |
| hr | pierce.tolan@cuan.example | `Demo-cuan-hr-2026!` |
| it_admin | amanda.oconnor@cuan.example | `Demo-cuan-it_admin-2026!` |
| auditor | jodie.robinson@cuan.example | `Demo-cuan-auditor-2026!` |
| platform_admin | cal.colligan@cuan.example | `Demo-cuan-platform_admin-2026!` |

### What to try at Cuan

**employee** (Elisha Devereaux Deaueroux, Operations). Already holds
everything the Operations department gets by default: `core-banking.operator`,
`ledger.viewer`, `vpn.user`, `m365.user`, `wiki.viewer`. So every new ask
needs an approval.

| Ask for | What happens |
|---|---|
| Datadog viewer, Wiki editor, CRM viewer, Support Desk agent, GitHub developer | Waits for the Operations manager |
| Data Warehouse analyst, Ledger editor | Waits for the Operations manager, then the system owner |
| Core Banking viewer, KYC analyst, Payments Gateway initiator | Critical system: manager, then system owner |
| HR System viewer | Critical HR system: manager, system owner, then HR |
| Production AWS deployer | Critical prod system: manager, system owner, then IT admin |
| Core Banking admin for yourself | Critical, and admin on yourself: manager, then system owner |

The Operations manager is not one of the logins above, so these requests
stay pending in this demo. To see a request approved from start to end,
use the manager login below.

You can also reset your password, unlock your account, ask policy questions
and open a ticket.

**manager** (Faith Steed, Engineering manager). Sees and approves requests
from Engineering staff, asks for access on their behalf, runs access reviews
and reads the audit trail for Engineering. Try this full approval flow: ask
for `core-banking.viewer` for Alex Ford. Core Banking is critical, and Faith
cannot approve her own request, so it goes to Padraigin (Core Banking owner)
and then Amanda (IT admin). Log in as each to approve it.

**system_owner** (Padraigin Fee, owns Core Banking). Approves, refuses and
revokes Core Banking access, and runs Core Banking access reviews. Holds
`core-banking.admin`, so asking for Payments Gateway approver for herself is
blocked by a separation of duties rule.

**hr** (Pierce Tolan). Onboards, moves and offboards people, asks for access
for anyone, and fills the HR seat on critical HR System requests.

**it_admin** (Amanda O'Connor). Onboards and moves people, approves the IT
admin seat on critical requests, rolls back a granted request, and reads the
separation of duties rules and policy documents.

**auditor** (Jodie Robinson, Risk & Compliance). Reads the full audit trail.
As a Risk employee she holds `kyc.analyst`, so asking for KYC approver for
herself is blocked by a separation of duties rule.

**platform_admin** (Cal Colligan). Everything the IT admin can do, plus
offboarding and the kill switches that stop the agent from granting.
