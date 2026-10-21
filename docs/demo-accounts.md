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
| manager | cornelia.zorbach@nordlicht.example | `Demo-nordlicht-manager-2026!` |
| system_owner | friedhold.trost@nordlicht.example | `Demo-nordlicht-system_owner-2026!` |
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
| Grafana viewer, Wiki editor, CRM viewer, GitLab developer | Waits for the Production manager (Cornelia) |
| PLM viewer, Finance Dashboard viewer, Supplier Master Data viewer, MES viewer | Waits for the Production manager, then the system owner |
| ERP buyer, Payment Release initiator | Critical system: manager, then system owner |
| HR System viewer | Critical HR system: manager, system owner, then HR |
| Production Cluster auditor or deployer | Critical prod system: manager, system owner, then IT admin |
| GitLab maintainer for yourself | Admin on yourself is pushed up one level: manager, then system owner |

The Production manager is the manager login above, so every one of these
requests shows up in Cornelia's approvals.

You can also reset your password, unlock your account, ask policy questions
("how long does VPN access last?") and open a ticket.

**manager** (Cornelia Zorbach, Production manager). Vincenzo's own manager.
Sees and approves requests from Production staff, asks for access on their
behalf, runs access reviews and reads the audit trail for Production.

**system_owner** (Friedhold Tröst, IT, owns Production Cluster). Approves,
refuses and revokes Production Cluster access, and runs its access reviews.
Holds `prod-k8s.deployer`, so asking for Production Cluster auditor for
himself is blocked by a separation of duties rule.

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

### One request, three approvers at Nordlicht

Production Cluster is a critical production system, so a request for it needs
the manager, the system owner and an IT admin. All three are demo logins.

1. Sign in as the employee (Vincenzo). Ask for Production Cluster auditor.
   The agent shows what it will file. Say yes.
2. Sign in as the manager (Cornelia). The bell shows one new approval. Open
   Approvals and approve it.
3. Sign in as the system owner (Friedhold). The bell shows the same request.
   Approve it.
4. Sign in as the IT admin (Bernt). The bell shows it too. Approve it.
5. Sign in as Vincenzo again. The bell says the access was granted, and
   `prod-k8s.auditor` is in My access.

The three approvals can happen in any order. Each approver's reply says who
is still left.

## Cuan Financial Services Ltd (`cuan`)

| Principal | Email | Password |
|---|---|---|
| employee | elisha.devereauxdeaueroux@cuan.example | `Demo-cuan-employee-2026!` |
| manager | freya.mcpolin@cuan.example | `Demo-cuan-manager-2026!` |
| system_owner | marco.prial@cuan.example | `Demo-cuan-system_owner-2026!` |
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
| Datadog viewer, Wiki editor, CRM viewer, Support Desk agent, GitHub developer | Waits for the Operations manager (Freya) |
| Data Warehouse analyst, Ledger editor | Waits for the Operations manager, then the system owner |
| Core Banking viewer, KYC analyst, Payments Gateway initiator | Critical system: manager, then system owner |
| HR System viewer | Critical HR system: manager, system owner, then HR |
| Production AWS auditor or deployer | Critical prod system: manager, system owner, then IT admin |
| Core Banking admin for yourself | Critical, and admin on yourself: manager, then system owner |

The Operations manager is the manager login above, so every one of these
requests shows up in Freya's approvals.

You can also reset your password, unlock your account, ask policy questions
and open a ticket.

**manager** (Freya McPolin, Operations manager). Elisha's own manager.
Sees and approves requests from Operations staff, asks for access on their
behalf, runs access reviews and reads the audit trail for Operations.

**system_owner** (Marco Prial, IT, owns Production AWS). Approves, refuses
and revokes Production AWS access, and runs its access reviews. Holds
`prod-aws.deployer`, so asking for Production AWS auditor for himself is
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

### One request, three approvers at Cuan

Production AWS is a critical production system, so a request for it needs
the manager, the system owner and an IT admin. All three are demo logins.

1. Sign in as the employee (Elisha). Ask for Production AWS auditor. The
   agent shows what it will file. Say yes.
2. Sign in as the manager (Freya). The bell shows one new approval. Open
   Approvals and approve it.
3. Sign in as the system owner (Marco). The bell shows the same request.
   Approve it.
4. Sign in as the IT admin (Amanda). The bell shows it too. Approve it.
5. Sign in as Elisha again. The bell says the access was granted, and
   `prod-aws.auditor` is in My access.

The three approvals can happen in any order. Each approver's reply says who
is still left.
