# Access control policy

Document id: NL-POL-001
Version: 2.0
Owner: Head of IT Security
Effective date: 2026-03-01
Review cycle: annual
Revision history: 2.0 restructures the document, adds a worked classification example, a section on how a request travels through the process, and an exceptions and dispensations process. No decision rule, approver chain, or expiry number changed from version 1.0.

## 1 Purpose and scope

Nordlicht Maschinenbau GmbH grants employees access to its IT systems on the principle of least privilege: a person holds the access their current duties require and nothing beyond that. This policy states how that principle is applied in practice. It binds all employees, all departments, and every system the company operates or has operated on its behalf. External parties fall under the Third-party and Contractor Access Policy in addition to this one.

Three properties hold for every grant without exception. It has a named owner. It rests on a documented approval. It carries an expiry date. Requests reach the process only through the IT helpdesk portal or the access request assistant; a request placed by email, in chat, or in conversation has no standing and is not acted on.

## 2 How a request travels

A request names one person, one role on one system, a business justification, and a wanted duration. From submission to grant it passes four stations.

First, the request is checked against the Segregation of Duties Standard. A conflict ends the request immediately; see section 8.

Second, the request is classified into a risk class using the rules in section 5. The class is recorded on the request and shown to every later approver.

Third, the approval chain for that class runs, one approver after another, as section 6 describes.

Fourth, the grant is executed with the expiry computed under section 7, after a second segregation-of-duties check at execution time.

Every station writes to the request record. The record is what the quarterly review under the Access Review Procedure examines.

## 3 System sensitivity tiers

Each system carries exactly one of four sensitivity tiers, assigned and maintained by the Head of IT Security. No access to a system is granted before it has a tier.

Critical tier: systems where misuse can produce direct financial loss, endanger employees, or stop production. These are erp (ERP), payments (Payment Release), hris (HR System), and prod-k8s (Production Cluster).

High tier: systems holding valuable engineering, manufacturing, or supplier data, or controlling entry to the premises. These are plm (CAD vault), mes (Manufacturing Execution), finance-dash, vendor-master, and badge (Physical Access).

Medium tier: systems holding internal business data whose exposure has limited damage potential. These are crm, vpn, gitlab, and m365.

Low tier: systems holding broadly shared internal content. These are wiki and grafana.

## 4 Department templates and non-standard access

Every department has a role template listing what a member receives automatically on joining, under the Joiner, Mover, Leaver Procedure. The templates are:

Engineering: plm.engineer, gitlab.developer, grafana.viewer, vpn.user, m365.user, wiki.editor, badge.holder.
Production: mes.operator, erp.viewer, vpn.user, m365.user, wiki.viewer, badge.holder.
Procurement: erp.buyer, vendor-master.editor, crm.viewer, vpn.user, m365.user, wiki.editor, badge.holder.
Finance: finance-dash.viewer, erp.viewer, payments.initiator, vpn.user, m365.user, wiki.viewer, badge.holder.
Sales: crm.editor, erp.viewer, vpn.user, m365.user, wiki.viewer, badge.holder.
HR: hris.viewer, vpn.user, m365.user, wiki.viewer, badge.holder.
IT: gitlab.developer, grafana.viewer, vpn.user, m365.user, wiki.editor, badge.holder.

A role inside the requester's own department template is standard access. Everything else is non-standard access. Non-standard requests are a normal part of the business, for example when a Finance employee needs to read the CAD vault for a costing project, but they always receive closer scrutiny than template roles.

## 5 Risk classification

Every request receives one of four risk classes: low, elevated, privileged, or critical. The rules below are applied in the order given, and the first matching rule that sets a floor is combined with the tenure rule at the end.

Rule one: any role on a critical system (erp, payments, hris, prod-k8s) makes the request critical. Template membership and privilege level do not soften this.

Rule two: any role carrying admin privilege makes the request privileged at minimum. If the system is also critical, rule one already applies and the class is critical.

Rule three: a role outside the requester's department template is elevated when the system is low or medium tier, and privileged when the system is high tier.

Rule four: a template role is low on a low or medium tier system. On a high tier system, a template role with read privilege is low and one with write privilege is elevated.

Tenure rule: when the person the access is for has been employed for fewer than 30 days, a request that would otherwise be low becomes elevated. New joiners are the most common vehicle for social engineering and for mistakes in role assignment; this rule puts a manager between both risks and the grant.

### 5.1 A worked example

A Procurement employee, employed for two years, requests plm.viewer to check a supplier's drawing package. The system plm is high tier. The role is not in the Procurement template, so rule three applies: outside template on a high tier system means privileged. The department manager and the plm system owner must both approve, and the expiry is capped at 90 days under section 7. Had the same employee requested crm.viewer, a template role on a medium tier system, rule four would classify it low and it would be granted automatically.

## 6 Approvals and escalation paths

The risk class decides the chain.

Low: granted automatically, with the classification recorded. No human is involved.

Elevated: the department manager of the person receiving the access approves.

Privileged: the department manager approves first, then the owner of the target system.

Critical: department manager, then system owner, then a third approver. The third approver is IT administration for production and infrastructure systems, and HR for the HR system.

Approvers act one after another, each seeing the justification, duration, and classification. A rejection by any approver closes the request, and the requester is told the reason. All decisions are visible in the quarterly review.

Two situations reroute the chain. A manager never approves a request they submitted for themselves; the decision moves to the next role in the chain, first the system owner, then IT administration. And a request in which a person asks for admin access for themselves never completes automatically and always escalates one approval level beyond what its class would normally require, as the Privileged Access Standard details.

When an approver does not act within the deadline shown in the IT helpdesk portal, the portal reminds them and copies their department head. A request cannot be granted by timeout; silence never approves anything.

## 7 Expiry

No grant is permanent. Three numbers determine when a grant ends.

The role default depends on privilege: read roles default to 365 days, write roles to 180 days, admin roles to 90 days.

The class cap depends on risk class: low 365 days, elevated 180 days, privileged 90 days, critical 30 days.

The admin cap: any role with admin privilege is capped at 30 days, whatever the risk class.

The effective expiry is the minimum of the requested duration, the role default, and the applicable caps. Asking for more than the cap does not fail the request; it proceeds at the cap and the over-ask is noted on the record. Renewal is always a fresh request, classified again from the beginning.

## 8 Blocked requests

Two conditions block a request outright, and no approver can lift the block through the normal process.

Segregation of duties: a request that would create a conflict defined in the Segregation of Duties Standard is blocked, with the specific rule cited in the notice, for example SoD-STD 2.1. A conflict exists when the target person already holds the paired role or has an approved but not yet executed request for it.

Offboarding: a request for a person who is in offboarding or has left the company is blocked. The Joiner, Mover, Leaver Procedure defines when offboarding begins.

## 9 Exceptions and dispensations

A situation this policy does not fit is handled as a written exception, never as an informal workaround. The requesting manager describes the need, the deviation, and the end date, and sends it to it-security@nordlicht.example. The Head of IT Security decides in writing; an approved exception is time-limited, names the accountable manager, and appears on the agenda of every quarterly access review until it lapses.

An exception can relax process steps, for example a modified verification path during an audit engagement. An exception cannot create a grant that section 8 blocks, cannot exceed the expiry caps in section 7, and cannot remove an approver from a chain. Requests for those things are refused.

## 10 Responsibilities and enforcement

Employees request only what they need and report access they no longer need. Department managers decide within the portal deadlines and certify their team's grants quarterly under the Access Review Procedure. System owners decide on privileged and critical requests for their systems and keep role definitions current. The IT security office runs the process, maintains the tier list, and audits grants.

Going around the request process, sharing credentials, or using a grant for a purpose other than the one approved violates this policy and is handled under the disciplinary provisions of the employment contract. Suspected violations go to it-security@nordlicht.example.
